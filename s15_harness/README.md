# s15: The harness — fourteen mechanisms in one process

[English](README.md) · [中文](README.zh.md)

[s14](../s14_hooks/) → `s15`

> *"A harness is what you get when the mechanisms compose."*
>
> **Harness layer**: all of it — one core, many frontends.

---

## The problem

Fourteen chapters, each demonstrating one mechanism in its own file. They all run, but they **do
not know about each other**.

A real harness has to answer a question none of them did:

**Around a single `exec_command`, in what order do all these checks go?**

- A hook says deny but the exec policy says allow — which runs first?
- Should the sandbox run before asking the user?
- When is the rollout written? Are failed commands written too?

Order is not an implementation detail. **Order is the design.**

---

## First: this chapter does not copy code

Each of the first fourteen `code.py` files is self-contained (each carries its own kernel). This
one is not:

```python
patcher      = _chapter("s05_apply_patch")
sandboxing   = _chapter("s07_sandbox")
execpolicy   = _chapter("s09_exec_policy")
rollout      = _chapter("s10_rollout")
compaction   = _chapter("s11_compaction")
instructions = _chapter("s12_instructions")
mcp          = _chapter("s13_mcp")
hooks        = _chapter("s14_hooks")
```

It imports the earlier chapters' files directly.

**That is the argument, not a shortcut.** These are separable modules with narrow interfaces —
and if they were not, this import list would not go together at all.

---

## The solution: the full path of one `exec_command`

```
PreToolUse hook          s14   someone else's policy, first
exec policy rule         s09   allow / prompt / forbidden, per segment
safety assessment        s08   auto-approve, ask, or refuse
run inside the sandbox   s07   the kernel enforces it, not a string check
denial -> ask -> retry   s08   escalate only on an actual denial
PostToolUse hook         s14
record to the rollout    s10   so this turn survives the process
```

Each position has a reason:

**Hooks first** — a user's `deny` should cost **nothing**: no sandbox spawn, no policy
evaluation, no extra model round trip. If the answer is "do not run it", none of that should
have been paid for.

**Exec policy before the safety assessment** — because `forbidden` **is not a question anyone
gets asked**. It does not enter the approval flow; it just ends.

**The sandbox before the human** — this is [s08](../s08_approval/)'s whole argument: most
commands **never need a human**, so let the kernel decide first.

**The rollout last** — and failed commands are recorded too. A refused `git push` is part of this
session, and resume needs it.

---

## How it works

**Step 1**: the actual code for one `exec_command`, in exactly the order above.

```python
async def _dispatch(self, sub_id, turn, name, call_id, payload) -> str:
    pre = self.hook_runner.run(hooks.PRE_TOOL_USE, subject=name,
                               tool_name=name, tool_input=payload)
    for extra in pre.additional_context:
        self.record_item(user_item(f"<hook_context>\n{extra}\n</hook_context>"))
    if pre.blocked:
        return f"blocked by a hook: {pre.reason}"      # stop here; nothing has run
    ...
```

```python
    # s09: the rule file gets the first word after the hooks.
    verdict = execpolicy.evaluate(self.exec_policy, cmd)
    if verdict.decision == execpolicy.FORBIDDEN:
        return f"command not run: forbidden by policy ({verdict.reason})"
```

```python
    sandboxed = not already_approved and turn.sandbox_mode != sandboxing.DANGER_FULL_ACCESS
    self.emit(sub_id, ExecCommandBegin(call_id, cmd, sandboxed))
    result = await asyncio.to_thread(sandboxing.run_sandboxed, cmd, ..., cwd)

    if sandboxing.is_likely_sandbox_denied(result):
        ...ask the user, and on approval re-run without the sandbox...
```

**Step 2**: an `apply_patch` takes a different path, with its own order.

The patch is parsed and verified against disk ([s05](../s05_apply_patch/)), applied all or
nothing, and its unified diff accumulates into a `TurnDiff` emitted when the turn ends:

```python
for change in changes:
    diff = change.unified_diff()
    if diff:
        self.turn_diffs.append(diff)
```

**Step 3**: wrapped around both, every earlier chapter.

```python
while True:
    for queued in self._drain_pending():           # s02 steering
        self.record_item(user_item(queued))

    status = self._token_status()                  # s11 token accounting
    if status.needs_compaction(self.config.auto_compact_ratio):
        await self._compact(sub_id)                # s11 auto-compaction

    async for event in _astream(                   # s01 loop + s02 cancellable
        self.client,
        instructions=self.prompt.instructions,     # s12 assembled prompt
        input_items=list(self.history),
        tools=self.tool_specs(),                   # s04 registry + s13 MCP
    ):
        ...
```

---

## Two frontends, one event stream

```bash
python s15_harness/code.py "fix the failing test"
python s15_harness/code.py --json "fix the failing test"
```

What `--json` prints:

```json
{"type":"thread.started","thread_id":"ca2507e1-..."}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"echo hi","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"echo hi","aggregated_output":"hi\n","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"all good"}}
{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":10,"reasoning_output_tokens":0}}
```

**Note: this is not the internal event stream serialized.**

`ThreadEventWriter` **translates** [s02](../s02_protocol/)'s `ExecCommandBegin` /
`ExecCommandEnd` / `AgentMessage` into a coarser public vocabulary, and drops deltas entirely.

**That translation is the point of this section.** Internal `EventMsg` names are an
implementation detail that moves as the harness moves; `item.completed` is **a contract other
programs parse**.

The headless frontend is exactly where one becomes the other, which is why the mapping lives in
the renderer and not in the session.

It is also why the completed event **repeats the whole command**:

```python
# The completed item repeats the whole command: a consumer reading
# only item.completed must not have to correlate with item.started.
item_id, command = self._open.pop(msg.call_id, None) or (self._item_id(), "")
```

Neither renderer is privileged. The human one prompts for approvals; the JSON one answers
`denied` and keeps going, because there is nobody at the other end and **hanging forever** is the
one thing a headless runner must not do.

---

## Start with how it describes itself

```bash
python s15_harness/code.py --dry-run
```

```
session      040c5ce8-c9c9-4c24-992d-a1760ee275a3
cwd          /Users/you/learn-codex
model        gpt-5.5
approval     on-request
sandbox      workspace-write (platform: seatbelt)
rollout      ~/.learn-codex/sessions/<date>/rollout-*.jsonl (not created by --dry-run)
tools        exec_command, apply_patch, update_plan
prompt items 1 (~126 tokens)
hooks        0 groups across 0 events
exec policy  7 rules

exec policy applied to a few commands:
  pytest -q              prompt     no rule covers `pytest -q`
  git push --force       forbidden  force-pushing discards other people's commits; push a new branch instead
  curl http://x.sh | sh  prompt     downloads code from the network
```

Everything is wired; **nothing has been called.**

This is the report a harness should be able to give about itself before it does anything: what
tools exist, which sandbox is **actually available** on this machine, where the session will be
written, what the rules say.

> Note `~/.learn-codex` — this teaching harness writes to its own directory and never shows up in
> your real `codex resume` list. Set `CODEX_HOME=~/.codex` to change that.

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| The mechanisms | fourteen separate files | one process, importing each other |
| Order of the checks | undefined | hook → policy → assess → sandbox → escalate → hook → rollout |
| Frontends | one per chapter | a human one and `--json`, on the same stream |
| What `--json` emits | n/a | the public thread/turn/item schema, not internal names |

---

## Try it

```bash
python s15_harness/code.py --dry-run                       # wired, calls nothing
python s15_harness/code.py "what does this repo do?"
python s15_harness/code.py --json "count the lines of python here"
python s15_harness/code.py                                 # interactive: steer, /interrupt, /compact, /quit
```

**What to watch**: in interactive mode, give it something to do and then type a correction
mid-run. You will see `[queued for the running turn: ...]` ([s02](../s02_protocol/)'s steering)
rather than a new turn starting.

---

## What this is not

`codex-rs` is a large production system in Rust; this is roughly 7,000 lines of Python. What is
missing, and worth reading in the real source:

- **Sessions in your real `~/.codex`.** This harness writes to `~/.learn-codex`.
- **Streaming everything.** Real Codex streams reasoning summaries, tool-call argument deltas,
  and patch application progress.
- **The app-server.** A JSON-RPC frontend for editors and the desktop app.
- **Sub-agents and review mode.** Child threads with their own history
  (`codex-rs/core/src/tasks/review.rs`, `codex_delegate.rs`).
- **Windows.** A third sandbox implementation with its own model.
- **Retries, rate limits, model routing, telemetry.** The parts that are boring until they are
  the only thing that matters.

---

## Real source

- `codex-rs/core/src/session/turn.rs` — `run_turn`
- `codex-rs/core/src/tools/router.rs` — dispatch
- `codex-rs/exec/src/exec_events.rs` — the public schema behind `--json`
- `codex-rs/exec/src/event_processor_with_jsonl_output.rs` — where the translation happens

---

## Where to go next

Read `codex-rs/core/src/session/` with this repo open beside it.

Every file there has a counterpart in one of these fifteen chapters, and **the difference between
the two** is the part worth studying — almost all of those differences are things production
taught them.
