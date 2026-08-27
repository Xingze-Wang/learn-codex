# s15: The harness

[English](README.md) · [中文](README.zh.md)

[s14](../s14_hooks/) → `s15`

> *"A harness is what you get when the mechanisms compose."*

---

Fourteen chapters, fourteen mechanisms, each demonstrated alone. This one puts them in the same
process and runs a real turn through all of them.

It is the only chapter that does not repeat its dependencies:

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

That is the argument, not a shortcut. These are separable modules with narrow interfaces, and
if they were not, the import list would not typecheck as a design.

## The order of the checks is the design

Around a single `exec_command`:

```
PreToolUse hook          s14   someone else's policy, first
exec policy rule         s09   allow / prompt / forbidden, per segment
safety assessment        s08   auto-approve, ask, or refuse
run inside the sandbox   s07   the kernel enforces it, not a string check
denial -> ask -> retry   s08   escalate only on an actual denial
PostToolUse hook         s14
record to the rollout    s10   so this turn survives the process
```

Hooks come first because a user's `deny` should cost nothing — no sandbox spawn, no policy
evaluation, no model round trip. The exec policy comes before the safety assessment because
`forbidden` is not a question anyone gets asked. And the sandbox comes before the human, because
the whole point of s07 is that most commands never need one.

Around a single `apply_patch`: the patch is parsed and verified against disk (s05), applied all
or nothing, and its unified diff accumulates into a `TurnDiff` emitted when the turn ends.

Wrapped around both: one turn context (s03), one tool registry (s04), MCP tools (s13), token
accounting and auto-compaction (s11), instructions assembled from AGENTS.md and skills (s12) —
all driven through the submission/event queues (s02).

## Two frontends, one event stream

```bash
python s15_harness/code.py "fix the failing test"
python s15_harness/code.py --json "fix the failing test"
```

```python
async def render_json(thread, done):
    """`codex exec --json`: one JSON object per line, nothing else on stdout."""
    while True:
        event = await thread.next_event()
        print(event_to_json(event), flush=True)
```

```json
{"id":"init","msg":{"type":"session_configured","session_id":"...","tools":["exec_command","apply_patch","update_plan"]}}
{"id":"a1b2","msg":{"type":"task_started","turn_id":"..."}}
{"id":"a1b2","msg":{"type":"exec_command_begin","call_id":"call_1","command":"pytest -q","sandboxed":true}}
{"id":"a1b2","msg":{"type":"task_complete","last_agent_message":"..."}}
```

Neither renderer is privileged. The human one prompts for approvals; the JSON one answers
`denied` and keeps going, because there is nobody at the other end and hanging forever is the
one thing a headless runner must not do. That difference is four lines, and it is the entire
difference between the two frontends.

## Dry run

```bash
python s15_harness/code.py --dry-run
```

```
session      6b856ab0-...
cwd          /Users/you/learn-codex
model        gpt-5.5
approval     on-request
sandbox      workspace-write (platform: seatbelt)
rollout      ~/.codex/sessions/2026/08/27/rollout-....jsonl
tools        exec_command, apply_patch, update_plan
prompt items 1 (~126 tokens)
hooks        0 groups across 0 events
exec policy  7 rules

exec policy applied to a few commands:
  pytest -q              prompt     no rule covers `pytest -q`
  git push --force       forbidden  force-pushing discards other people's commits; push a new branch instead
  curl http://x.sh | sh  prompt     downloads code from the network
```

Everything is wired; nothing is called. This is the report a harness should be able to produce
about itself before it does anything — what tools exist, which sandbox is actually available,
where the session will be written, what the rules say.

## What this is not

`codex-rs` is a large production system, and this is roughly 4,000 lines of Python. Missing
here, and worth reading in the real source:

- **Streaming everything.** Real Codex streams reasoning summaries, tool-call argument deltas,
  and patch application progress. Here only text streams.
- **The app-server.** A JSON-RPC frontend for editors and the desktop app.
- **Sub-agents and review mode.** Child threads with their own history, returning a structured
  result to the parent (`codex-rs/core/src/tasks/review.rs`, `codex_delegate.rs`).
- **Windows.** A third sandbox implementation with its own model.
- **Retries, rate limits, model routing, telemetry.** The parts that are boring until they are
  the only thing that matters.

## Run it

```bash
python s15_harness/code.py --dry-run
python s15_harness/code.py "what does this repo do?"
python s15_harness/code.py --json "count the lines of python here"
python s15_harness/code.py            # interactive: steer, /interrupt, /compact, /quit
```

## Real source

- `codex-rs/core/src/session/turn.rs` — `run_turn`
- `codex-rs/core/src/tools/router.rs` — dispatch
- `codex-rs/exec/src/event_processor_with_jsonl_output.rs` — `--json`

## Where to go next

Read `codex-rs/core/src/session/` with this repo open beside it. Every file there has a
counterpart in one of these fifteen chapters, and the difference between the two is the part
worth studying.
