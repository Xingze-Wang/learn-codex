# s10: Rollout — the session as an append-only file

[English](README.md) · [中文](README.zh.md)

[s09](../s09_exec_policy/) → `s10` → [s11](../s11_compaction/) → ... → [s15](../s15_harness/)

> *"Append, never rewrite. Resume and fork fall out for free."*
>
> **Harness layer**: persistence — making a session outlive a process.

---

## The problem

Everything so far lives in `self.history`, a list in memory. Close the terminal and it is gone.

What you lose is not just a chat log:

- which files it read, which approaches it tried, which one was a dead end
- the constraints you gave it ("don't touch vendor/")
- the refactor it was halfway through

To continue, you have to explain everything again.

So you say: fine, save it — `json.dump(history)` and be done.

**The problem is *when* to save.** At the end of each turn? Then a crash mid-turn loses that
whole turn — and it is exactly the long, slow, crash-prone turns that were most worth keeping.

---

## First: JSONL and "append-only"

**JSONL** is a text file with one JSON object per line.

```
{"type": "session_meta", "payload": {...}}
{"type": "response_item", "payload": {...}}
{"type": "event_msg", "payload": {...}}
```

Why is that better than one big JSON array?

1. **You can append**, without reading the whole file and writing it back.
2. **You can read just the first few lines** (a session may be megabytes; a picker only needs the
   head).
3. **A crash mid-write leaves every earlier line intact.**

"Append-only" means: **the file only grows; nothing already written is ever modified.**

Codex puts it here:

```
~/.codex/sessions/2026/05/23/rollout-2026-05-23T18-18-36-<thread-id>.jsonl
```

The date path is not decoration: `codex resume` lists yesterday's sessions by **opening one
directory**, not by reading every file on disk.

---

## The solution

Four line types, with one split that carries real weight:

```
session_meta    once, first line: id, cwd, cli version, instructions
turn_context    once per turn: cwd, approval policy, sandbox policy, model
response_item   what gets REPLAYED TO THE MODEL on resume
event_msg       what the USER SAW
```

A real file looks like this (these lines are from an actual `~/.codex` rollout):

```json
{"timestamp":"2026-05-23T10:18:47.419Z","type":"session_meta","payload":{"id":"019e5458-...","cwd":"/Users/you","cli_version":"0.128.0"}}
{"timestamp":"2026-05-23T10:18:57.334Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"sed -n '1,220p' ...\"}","call_id":"call_ZX8V..."}}
{"timestamp":"2026-05-23T10:18:57.391Z","type":"event_msg","payload":{"type":"exec_command_end","call_id":"call_ZX8V...","exit_code":0}}
```

**Why separate `response_item` from `event_msg`?**

- Restoring a session for a **human** → you need both (to redraw the commands, output, timings).
- Replaying it to the **model** → only the first (the model does not need to know what color the
  terminal was).

The split is load-bearing at read time, so it has to exist at write time.

> `python s10_rollout/code.py --show ~/.codex/sessions/.../rollout-xxx.jsonl`
> reads your own real Codex session files — the format above is what it parses.

---

## How it works

**Step 1**: not everything is persisted.

```python
PERSISTED_EVENTS = {
    "task_started", "task_complete", "user_message", "agent_message",
    "exec_command_begin", "exec_command_end", "token_count", ...
}
```

`agent_message_delta` is **not** in that set. A turn emits thousands of deltas and one complete
`agent_message`; persisting the fragments would multiply the file size for information that is
**already there**.

**Step 2**: append and flush, per line.

```python
# Append and flush per line: a crash mid-turn must not lose the turn.
with self.path.open("a", encoding="utf-8") as handle:
    handle.write(line + "\n")
    handle.flush()
```

That is the direct answer to "when to save": **every time something happens.**

**Step 3 — the reader must expect the crash that eventually happens.**

```python
try:
    line = json.loads(raw)
except json.JSONDecodeError:
    # A crash can leave a half-written last line. Everything before
    # it is still a valid session; refuse to throw it away.
    continue
```

A truncated last line is the **normal end state of a killed process**. Refusing to load the file
because of it would throw away the session it was supposed to protect.

**Step 4**: resume — pull out the `response_item`s and you have the model's view.

```python
def resume(path):
    """Rebuild (history, session meta). The model sees what it saw before."""
    rollout = read_rollout(path)
    return rollout.response_items(), rollout.meta
```

Two lines. Because [s01](../s01_agent_loop/)'s `store: false` made the harness own the history,
"restoring" is just putting a list back.

**Step 5**: fork — "go back three turns and try something else".

```python
def fork(path, codex_home, *, keep_turns):
    """Copy the first `keep_turns` turns into a new thread.

    The original file is never edited. Rewriting history in place would mean a
    crash during the rewrite loses both futures."""
```

The implementation is: make a new file, copy lines across, stop at the `keep_turns`-th
`task_started`.

**Because the log is append-only, the old one is still there and still complete.** You get two
parallel timelines.

**Step 6**: the session picker — without reading the whole file.

```python
def head_summary(path, max_lines=40):
    """Enough to render one row of a session picker, without reading the file."""
```

A long session is megabytes. A picker showing fifty of them must not read fifty megabytes to
draw a list. So the summary comes from the head: meta, then the first user message, then stop.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `should_persist` | The write-time filter |
| `RolloutRecorder` | Create the dated path, append lines |
| `read_rollout` / `Rollout` | Read back, tolerate truncation |
| `resume` / `fork` | Replay, and branch |
| `head_summary` / `list_rollouts` | The session picker |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| Where the session lives | memory only | a JSONL file on disk |
| When it is written | n/a | every line, flushed immediately |
| A crash | loses everything | loses at most the half-written last line |
| Continuing yesterday's work | impossible | `resume` |
| Trying a different approach from turn 3 | impossible | `fork`, leaving the original intact |

---

## Try it

**No API key needed:**

```bash
python s10_rollout/code.py --demo
```

It records two turns, prints the whole file, then resumes once and forks once.

**What to watch**: these three lines of the demo output:

```
turns: 2
replayable items: 8
renderable events: 14  (no deltas: dropped by policy)
```

14 events versus 8 replayable items — that is the split. Then the fork:

```
fork(keep_turns=1) -> rollout-....jsonl
  forked turns: 1
  original untouched: 2 turns
```

**The original file was not modified by a single byte.**

Then read your own real sessions:

```bash
python s10_rollout/code.py --list ~/.codex
python s10_rollout/code.py --show ~/.codex/sessions/2026/.../rollout-xxx.jsonl
```

---

## Real source

- `codex-rs/rollout/src/recorder.rs`, `policy.rs`, `list.rs`
- `codex-rs/core/src/session/rollout_reconstruction.rs`

---

## Next

The history is durable. And it **grows forever**.

After two hours, history holds dozens of `pytest` outputs and hundreds of file reads. Eventually
one request hits the model's context limit and **the whole thing stops there**.

[s11](../s11_compaction/) makes room — and the trick is doing it **before** the request that
would fail, not after.
