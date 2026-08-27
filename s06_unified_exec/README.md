# s06: Unified exec — the shell that outlives the tool call

[English](README.md) · [中文](README.zh.md)

[s05](../s05_apply_patch/) → `s06` → [s07](../s07_sandbox/)

> *"Return early with a session id, not late with a timeout."*

---

s01 ran `/bin/bash -lc CMD` and waited for it to exit. That model breaks on everything
interesting:

```
cd build && make     -> the cd is gone by the next call
python3              -> a REPL that never exits, so the call never returns
npm run dev          -> a server that must keep running while work continues
ssh host             -> a prompt that wants an answer
```

Codex's `exec_command` opens a PTY-backed **session**. If the command finishes inside
`yield_time_ms`, the tool returns its output and the session is retired. If it does not, the tool
returns *early* with a session id, and the model keeps talking to the live process with
`write_stdin`.

## The response header is the protocol

```
Chunk ID: 8f21ac
Wall time: 0.0031 seconds
Process exited with code 0            <- finished
Output:
...
```

```
Chunk ID: 44b0e1
Wall time: 1.0007 seconds
Process running with session ID 3     <- still alive, talk to it
Output:
>>>
```

The model does not have to guess whether a command finished. One line says which world it is in,
and `Process running with session ID 3` is also the handle it needs to continue.

## Why a PTY and not pipes

Two reasons, both practical. Interactive programs check whether stdout is a terminal and change
behavior — `python3` prints no prompt to a pipe, `git` pages differently, progress bars
disappear. And a process wanting input from a pipe that never closes just hangs, invisibly.

The cost is that a PTY echoes: what you write comes back in the output. That is why the demo's
`print(6 * 7)` shows up next to `42`.

## Reading without hanging

```python
while time.monotonic() < deadline:
    if not selector.select(timeout=0.05):
        if self.process.poll() is not None:
            break
        if time.monotonic() - last_output > IDLE_QUIET_MS / 1000:
            break
        continue
```

Three exits: the process died, the output went quiet, or the yield window expired. The
quiet-output exit is what makes an interactive session feel responsive — a REPL that printed its
prompt and is now waiting has nothing more to say, and waiting the full 10 seconds to discover
that would make every interaction unusable.

```python
except OSError as exc:
    if exc.errno in (errno.EIO, errno.EBADF):
        break  # the child closed the pty: it is gone
```

On a PTY, the child exiting surfaces as `EIO` on read, not as EOF. Treating that as an error
would report a crash for every command that completed normally.

## Output is capped, and the model is told

```python
class HeadTailBuffer:
    """Keep the beginning and the end, drop the middle."""
```

```
Chunk ID: 9fb9b5
Wall time: 0.0285 seconds
Process exited with code 0
Original token count: 50000          <- what it would have been
Output:
xxxxxxxxxx...
[... 199900 characters truncated ...]
```

`Original token count` matters more than the truncation itself: the model can see that it asked
for something enormous, and narrow the next command instead of assuming it saw everything.

## The process group

```python
start_new_session=True,  # its own process group, so we can kill the tree
```

Killing the shell without this leaves its children — the `make`, the dev server, the `ssh` —
running after the session ends. `os.killpg` on a group the harness created cleans up everything
the command started.

## In `code.py`

| Piece | Job |
|---|---|
| `ProcessManager.exec_command` | Spawn under a PTY, read until yield, return or hand back a session |
| `ProcessManager.write_stdin` | Talk to a live session |
| `ExecSession.read_available` | Deadline + quiet + exit |
| `HeadTailBuffer` | Bounded output with a dropped-count |
| `ExecResult.render` | The header above |

## Run it

```bash
python s06_unified_exec/code.py --demo
python s06_unified_exec/code.py --repl     # drive a live session by hand
```

## Real source

- `codex-rs/core/src/unified_exec/` — `mod.rs`, `process.rs`, `process_manager.rs`
- `codex-rs/core/src/tools/context.rs` — `response_header`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` — `yield_time_ms`, `max_output_tokens`

## Next

Everything so far runs with the user's full privileges. [s07](../s07_sandbox/) takes them away.
