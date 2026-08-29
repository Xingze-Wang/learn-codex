# s02: The Submission / Event protocol

[English](README.md) · [中文](README.zh.md)

[s01](../s01_agent_loop/) → `s02` → [s03](../s03_turn_context/)

> *"The caller does not call the agent. It submits an Op and reads Events."*

---

s01's loop returns a string. That is enough for a script and nothing else: while it runs, the
user cannot correct it, cannot stop it, and cannot answer a question it needs answered.

Codex does not expose the loop. It exposes two queues:

```
caller --Op--> [SQ] --> submission_loop --> turn task --Event--> [EQ] --> caller
```

`CodexThread.submit(op)` puts a submission on the first queue and returns immediately.
`next_event()` reads from the second. Every frontend Codex ships — the TUI, `codex exec --json`,
the app-server, the MCP server — is a different reader of that same event stream. None of them
is "the" interface, which is exactly why there can be four of them.

## Three things this shape makes possible

**Interrupt.** The turn runs as a task; `Op::Interrupt` cancels it.

```python
elif isinstance(op, Interrupt):
    active = sess.active
    if active and not active.task.done():
        active.task.cancel()
```

The turn does not have to agree to stop, and it does not have to reach a checkpoint. This is
why the model stream is bridged onto the event loop: every chunk is an `await`, so cancellation
lands between chunks rather than after the response completes.

**Steering.** A message typed while a turn is running does not start a second turn:

```python
if sess.active is not None and not sess.active.task.done():
    # A turn is already running: steer it, do not start a
    # second one. This is the whole reason for the queue.
    sess.pending_input.append(op.text)
```

It is drained into history at the next step boundary — before the next model call, after the
current tool finishes. The model sees the correction on its next request. Two turns racing on
one history would produce interleaved tool calls against a shared working directory; one turn
with an extra user message is just a conversation.

**Approval.** A tool can stop and wait for an answer that has not been submitted yet, because
the turn is a coroutine, not a stack frame. That is s08, and it needs no new machinery.

## The turn records its own interruption

```python
except asyncio.CancelledError:
    self.history.append(user_item("[turn interrupted by user]"))
    self.emit(sub_id, TurnAborted("interrupted"))
    raise
```

Without that line, the next turn's history shows a `function_call` with no output — which is a
protocol error on the next request, and which reads to the model as "that command is still
running". The abort has to be a fact in the conversation.

## Events describe what happened, not what to draw

```python
TaskStarted, AgentMessageDelta, AgentMessage, ExecCommandBegin, ExecCommandEnd,
UserMessageQueued, TokenCount, TaskComplete, TurnAborted, ErrorEvent, ShutdownComplete
```

`ExecCommandBegin(call_id, command, cwd)` carries the command, not a formatted line. The TUI
renders it as a colored prompt; `--json` prints it as an object; the test asserts on the field.
The moment an event carries pre-rendered text, there is only one frontend.

Every `Event` carries the `id` of the submission that caused it, so a caller with several
outstanding submissions can tell the answers apart.

One layer up, the surface other programs actually consume is coarser: `codex exec --json` and
the app-server speak a **Thread / Turn / Item** vocabulary (`thread.started`, `turn.started`,
`item.completed`, `turn.completed`) rather than these internal names. [s15](../s15_harness/)
implements that translation, and the reason it exists is that internal event names are free to
change while a published schema is not.

## In `code.py`

| Piece | Job |
|---|---|
| `Op` / `Submission` | What a caller may ask for |
| `Event` / `EventMsg` | What the thread reports |
| `CodexThread` | The two queues plus `submit()` / `next_event()` |
| `_submission_loop` | One consumer; never blocks on a running turn |
| `Session.pending_input` | The steering queue |
| `_astream` | Blocking SSE iterator → cancellable async iterator |

## Run it

```bash
python s02_protocol/code.py "list the 3 largest files here"
python s02_protocol/code.py            # then type while it works, or /interrupt
```

## Real source

- `codex-rs/protocol/src/protocol.rs` — `Op`, `Event`, `EventMsg`
- `codex-rs/core/src/session/handlers.rs` — `submission_loop`
- `codex-rs/core/src/session/input_queue.rs` — pending input and steering

## Next

The queues carry ops; the turn needs settings. [s03](../s03_turn_context/) is what a turn
freezes when it starts, and what the model is told about it.
