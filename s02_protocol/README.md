# s02: The Submission / Event protocol — making the loop something you can talk to

[English](README.md) · [中文](README.zh.md)

[s01](../s01_agent_loop/) → `s02` → [s03](../s03_turn_context/) → ... → [s15](../s15_harness/)

> *"The caller does not call the agent. It submits an Op and reads Events."*
>
> **Harness layer**: the protocol seam — one core, many frontends.

---

## The problem

You ask it to refactor a module. Two minutes in, you can see from the output that it is editing
the wrong file.

You want to say: *"wait, not that one."*

**There is nowhere to say it.** s01's `run_turn` is an ordinary function: you call it, it runs,
it returns. During "it runs", the only thing you can do is Ctrl-C — killing the whole process,
along with everything it had already worked out.

The same flaw has two more faces:

- It is about to run `rm -rf build` and really should ask first. But a function returns once. It
  cannot *"ask, wait for your answer, then carry on"*.
- It is stuck on a command that has been running for five minutes. You want to stop just this
  turn and keep the conversation. You cannot.

---

## First: a function is the wrong shape for this

A function call has a fixed shape:

```
call ───────────────────────────────► return
        (nothing can get in during this stretch)
```

"Interject mid-run", "cancel mid-run", "answer a question mid-run" — all three need a door in
the *middle* of that line. A function has no middle.

So Codex does not expose the loop as a function. It exposes **two queues**:

- You **put** things into the first one (what you want the agent to do).
- You **read** things from the second one (what the agent is doing).

Putting and reading are independent; neither waits for the other. Which is how "mid-run"
suddenly has somewhere to live.

Their names in Codex:

- **SQ (Submission Queue)** — things going in. Each one is an **`Op`** (operation).
- **EQ (Event Queue)** — things coming out. Each one is an **`Event`**.

---

## The solution

```
                        ┌──────────────── SQ ────────────────┐
   caller ── Op ──────► │ UserTurn / Interrupt / Shutdown ... │
                        └──────────────┬─────────────────────┘
                                       │  submission_loop takes one at a time
                                       ▼
                               ┌───────────────┐
                               │  a turn (task) │ ◄── cancellable
                               └───────┬───────┘
                        ┌──────────────▼─────── EQ ──────────┐
   caller ◄─ Event ───  │ TaskStarted / ExecCommandBegin ...  │
                        └────────────────────────────────────┘
```

`CodexThread.submit(op)` puts a submission on the SQ and **returns immediately**.
`next_event()` reads from the EQ.

Every frontend Codex ships — the terminal UI, `codex exec --json`, the app-server for editors,
Codex-as-an-MCP-server — is **a different reader of that same event stream**. None of them is
"the" interface, which is exactly why there can be four.

---

## How it works

**Step 1**: define what a caller may submit. Four things.

```python
@dataclass(frozen=True)
class UserTurn:
    text: str          # the user said something

@dataclass(frozen=True)
class Interrupt:
    pass               # stop the current turn

@dataclass(frozen=True)
class Shutdown:
    pass               # pack up

Op = UserTurn | Interrupt | Shutdown
```

Each submission carries an id so the events coming back can be matched to it:

```python
@dataclass(frozen=True)
class Submission:
    id: str
    op: Op
```

**Step 2**: define what can happen. These are the raw material a frontend renders.

```python
TaskStarted          # a turn began
AgentMessageDelta    # a fragment of text from the model
ExecCommandBegin     # about to run this command
ExecCommandEnd       # it finished, here is the exit code
UserMessageQueued    # what you just typed was queued into the running turn
TokenCount           # how many tokens have gone
TaskComplete         # the turn is over
TurnAborted          # the turn was cut short
```

Note that `ExecCommandBegin(call_id, command, cwd)` carries **the command**, not a formatted
line. The terminal UI draws it as a colored prompt, `--json` prints it as an object, the test
asserts on the field. **The moment an event carries pre-rendered text, there can only be one
frontend.**

**Step 3**: `submit` does exactly one thing — enqueue.

```python
async def submit(self, op: Op) -> str:
    sub_id = uuid.uuid4().hex[:8]
    await self.submissions.put(Submission(sub_id, op))
    return sub_id
```

It does not wait for the turn. It does not even care whether a turn is running.

**Step 4**: one consumer, one op at a time.

```python
async def _submission_loop(self) -> None:
    while True:
        sub = await self.submissions.get()
        op = sub.op
        ...
```

A single consumer means two ops never mutate the same state at once. And it **never blocks on a
running turn** — which is what makes the next three things possible.

**Step 5**: a turn runs as a task, not inside this loop.

```python
task = asyncio.create_task(sess.run_turn(sub.id, op.text))
sess.active = ActiveTurn(sub.id, uuid.uuid4().hex[:12], task)
```

`asyncio.create_task` means "run this in the background, I'll carry on". So `_submission_loop`
goes straight back to `await self.submissions.get()` and can take the next op.

**The key property: a task can be cancelled. A function call cannot.**

**Step 6**: interrupting is cancelling that task.

```python
elif isinstance(op, Interrupt):
    active = sess.active
    if active and not active.task.done():
        active.task.cancel()
    else:
        sess.emit(sub.id, TurnAborted("no active turn"))
```

The turn does not have to agree to stop, and does not have to reach a checkpoint.

**Step 7**: steering. A message typed while a turn runs does **not** start a second turn.

```python
if sess.active is not None and not sess.active.task.done():
    # A turn is already running: steer it, do not start a
    # second one. This is the whole reason for the queue.
    sess.pending_input.append(op.text)
    sess.emit(sub.id, UserMessageQueued(op.text))
    continue
```

It sits in `pending_input` and is folded into history at the **next step boundary**:

```python
while True:
    # Step boundary: anything typed while the model was thinking
    # gets into history before the next request.
    for queued in self.drain_pending_input():
        self.record_user_text(queued)

    ... send the request ...
```

A "step boundary" is the point where the current tool has finished and the next model call has
not yet gone out. So the model sees your correction **on its very next request**.

Why not just start a second turn? Because two turns would race on one history, producing
interleaved tool calls against a shared working directory. **One turn plus an extra user message
is just a conversation.**

---

## One detail: the model stream has to be bridged onto the event loop

Step 6 says "cancel the task". But if the turn is parked on a blocking network read, the
cancellation never lands — Python can only switch away at an `await`.

So s01's synchronous stream gets a wrapper:

```python
async def _astream(client: ModelClient, **kwargs: Any) -> AsyncIterator[ResponseEvent]:
    """Bridge the blocking SSE iterator onto the event loop.

    The point is not the thread -- it is that every yield is an `await`, so an
    `Op.Interrupt` can cancel the turn task between chunks.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def produce() -> None:                       # blocking reads on a background thread
        for event in client.stream(**kwargs):
            loop.call_soon_threadsafe(queue.put_nowait, event)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=produce, daemon=True).start()
    while True:
        item = await queue.get()                 # <-- every chunk is an await
        if item is None:
            return
        yield item
```

Real Codex gets the same property from a tokio `CancellationToken`. Different mechanism, same
requirement: **cancellation has to land between two chunks of data, not after the whole
response.**

---

## The interruption has to be written into the conversation

```python
except asyncio.CancelledError:
    self.history.append(user_item("[turn interrupted by user]"))
    self.emit(sub_id, TurnAborted("interrupted"))
    raise
```

What happens without that middle line?

When the cancel lands, history may already contain a `function_call` whose
`function_call_output` never got written. Send that history on the next request and **the API
rejects the conversation as malformed** — a call with no matching result. Even if it did not,
what the model would read is "that command is still running".

So the abort itself has to become a fact in the conversation.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `Op` / `Submission` | What a caller may ask for |
| `Event` / `EventMsg` | What the thread reports |
| `CodexThread` | The two queues, plus `submit()` / `next_event()` |
| `_submission_loop` | One consumer; never blocks on a running turn |
| `Session.pending_input` | The steering queue |
| `_astream` | Blocking SSE → cancellable async iterator |
| `_render` | A frontend. It does one thing: read events, print them |

---

## Try it

```bash
python s02_protocol/code.py            # interactive
```

Then do these three things:

1. Type `find the three largest files in this repo`, and **while it is running** type
   `only .py files`. You will see `[queued for the running turn: ...]` — and **no** second
   `TaskStarted`.
2. Ask for something slow (`count the lines in every file, one at a time`), then type
   `/interrupt`.
3. Type `/quit`.

**What to watch**: in step 1, count how many times `[turn complete]` appears — it should be
once. Your interjection did not start a new turn; it was folded into the same one's history.

---

## One layer up, the public surface is coarser

The `Op` / `Event` above are the **internal** protocol. What other programs actually consume is
coarser: `codex exec --json` and the app-server speak a **Thread / Turn / Item** vocabulary —
`thread.started`, `turn.started`, `item.completed`, `turn.completed`.

[s15](../s15_harness/) implements that translation. The reason it exists: **internal event names
are free to change; a published schema is not.**

---

## Real source

- `codex-rs/protocol/src/protocol.rs` — the `Op`, `Event`, `EventMsg` enums
- `codex-rs/core/src/session/handlers.rs` — `submission_loop`
- `codex-rs/core/src/session/input_queue.rs` — pending input and steering

---

## Next

The queues carry ops. But when a turn starts, a pile of things have to be settled: which
directory does it run in, which model, may it write files?

And — the user can now change those settings mid-turn, because s02 just made that possible.

[s03](../s03_turn_context/) is about what a turn freezes when it starts, and **how the model
finds out where it is.**
