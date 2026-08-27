# s10: Rollout — the session as an append-only file

[English](README.md) · [中文](README.zh.md)

[s09](../s09_exec_policy/) → `s10` → [s11](../s11_compaction/)

> *"Append, never rewrite. Resume and fork fall out for free."*

---

Nothing so far survives the process exiting. Codex writes every session to JSONL as it happens:

```
~/.codex/sessions/2026/05/23/rollout-2026-05-23T18-18-36-<thread-id>.jsonl
```

The date path is not decoration: `codex resume` lists yesterday's sessions by opening one
directory, not by reading every file on disk.

## Four line types, and one important split

```
session_meta    once, first line: id, cwd, cli version, instructions
turn_context    once per turn: cwd, approval policy, sandbox policy, model
response_item   what goes back to the model on resume
event_msg       what the user saw
```

```json
{"timestamp":"2026-05-23T10:18:47.419Z","type":"session_meta","payload":{"id":"019e5458-...","cwd":"/Users/you","cli_version":"0.128.0"}}
{"timestamp":"2026-05-23T10:18:57.334Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"sed -n '1,220p' ...\"}","call_id":"call_ZX8V..."}}
{"timestamp":"2026-05-23T10:18:57.391Z","type":"event_msg","payload":{"type":"exec_command_end","call_id":"call_ZX8V...","exit_code":0}}
```

`response_item` lines rebuild the *model's* view. `event_msg` lines rebuild the *human's* view.
Rendering a resumed session needs both; replaying it to the model needs only the first. That is
why they are separate types rather than one stream with a flag — the split is load-bearing at
read time, so it has to exist at write time.

(`code.py --show` reads real `~/.codex` rollouts. The format above is what it parses.)

## Not everything is persisted

```python
PERSISTED_EVENTS = {"task_started", "task_complete", "user_message", "agent_message",
                    "exec_command_begin", "exec_command_end", "token_count", ...}
```

Deltas are not in that set. A turn emits thousands of `agent_message_delta` events and one
`agent_message`; persisting the fragments would multiply the file size for information that is
already there. The same filter drops response items the model does not need on replay.

## Append and flush, per line

```python
# Append and flush per line: a crash mid-turn must not lose the turn.
with self.path.open("a", encoding="utf-8") as handle:
    handle.write(line + "\n")
    handle.flush()
```

And the reader is built to expect the crash that happens anyway:

```python
except json.JSONDecodeError:
    # A crash can leave a half-written last line. Everything before
    # it is still a valid session; refuse to throw it away.
    continue
```

A truncated last line is the normal end state of a killed process. Refusing to load the file
because of it would throw away the session it was supposed to protect.

## Resume and fork

```python
def resume(path):
    """Rebuild (history, session meta). The model sees what it saw before."""
    rollout = read_rollout(path)
    return rollout.response_items(), rollout.meta
```

```python
def fork(path, codex_home, *, keep_turns):
    """Copy the first `keep_turns` turns into a new thread.

    The original file is never edited. Rewriting history in place would mean a
    crash during the rewrite loses both futures."""
```

Fork is what "go back three turns and try something else" actually is: two files sharing a
prefix. Because the log is append-only, the old one is still there and still complete.

## Listing without reading

```python
def head_summary(path, max_lines=40):
    """Enough to render one row of a session picker, without reading the file."""
```

A long session is megabytes. A picker showing fifty of them must not read fifty megabytes to
draw a list, so the summary comes from the head: meta, then the first user message, then stop.

## In `code.py`

| Piece | Job |
|---|---|
| `should_persist` | The write-time filter |
| `RolloutRecorder` | Create the dated path, append lines |
| `read_rollout` / `Rollout` | Read back, tolerate truncation |
| `resume` / `fork` | Replay, and branch |
| `head_summary` / `list_rollouts` | The session picker |

## Run it

```bash
python s10_rollout/code.py --demo
python s10_rollout/code.py --list ~/.codex        # your real sessions
python s10_rollout/code.py --show <file.jsonl>
```

## Real source

- `codex-rs/rollout/src/recorder.rs`, `policy.rs`, `list.rs`
- `codex-rs/core/src/session/rollout_reconstruction.rs`

## Next

The history is durable, and it grows forever. [s11](../s11_compaction/) makes room.
