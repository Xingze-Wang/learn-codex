# s06: Unified exec — the shell that outlives the tool call

[English](README.md) · [中文](README.zh.md)

[s05](../s05_apply_patch/) → `s06` → [s07](../s07_sandbox/) → ... → [s15](../s15_harness/)

> *"Return early with a session id, not late with a timeout."*
>
> **Harness layer**: execution — who decides how long a process lives.

---

## The problem

The obvious way to let an agent run a command is: **start it, wait for it to finish, take the
output.**

Four entirely ordinary cases break that on the spot:

```
cd build && make      -> the cd works, but the next call is a brand new bash, back where it started
python3               -> a REPL that never exits, so the call never returns
npm run dev           -> a server that must keep running while other work continues
ssh host              -> a prompt waiting for a password
```

The first two are the worst: **one silently does the wrong thing, the other hangs the agent.**

---

## what a PTY is, and why not pipes

`subprocess` uses **pipes** by default to collect a child's output. Pipes are fine for `ls`, but
interactive programs misbehave through them.

The reason: **many programs check whether their output is attached to a terminal, and change
behavior.**

- `python3` prints **no `>>>` prompt** to a pipe (it assumes it is being scripted)
- `git` pages differently
- progress bars and colors disappear

The input side is worse: a program reading from a pipe that **never closes** just **hangs,
invisibly**.

A **PTY** (pseudo-terminal) fixes this: the kernel makes a fake terminal device pair, and the
child's end looks exactly like a real terminal.

```python
master_fd, slave_fd = pty.openpty()      # a pair: master for us, slave for the child
process = subprocess.Popen(
    [self.shell, "-lc", cmd],
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    start_new_session=True,
)
os.close(slave_fd)                        # once the child has it, we do not keep our copy
```

The cost is that a PTY **echoes**: what you write comes back in the output. That is why the
demo's `print(6 * 7)` shows up right next to `42` — not a bug, just what a terminal is.

---

## The solution

Instead of "run to completion, then return", do "**run for a while, then return with a
handle**".

```
  exec_command(cmd, yield_time_ms=10000)
                │
                ├── finished inside the window ──► output + exit code, session retired
                │
                └── still running ──────────────► output so far + a session id
                                                     │
                                                     ▼
                                   write_stdin(session_id, "print(6*7)\n")
                                                     │
                                                     ▼
                                          more output (the process is still alive)
```

The model does not have to guess whether a command finished — **one line of the response header
says so.**

---

## The response header is the protocol

Finished:

```
Chunk ID: 8f21ac
Wall time: 0.0031 seconds
Process exited with code 0
Output:
hello
```

Still alive:

```
Chunk ID: 44b0e1
Wall time: 1.0007 seconds
Process running with session ID 3
Output:
>>>
```

`Process running with session ID 3` is also the **handle** the model needs to continue. Its next
move is `write_stdin(3, "...")`.

---

## How it works

**Step 1**: spawn under a PTY, in its own process group.

```python
process = subprocess.Popen(
    [self.shell, "-lc", cmd],
    cwd=cwd or os.getcwd(),
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    start_new_session=True,  # its own process group, so we can kill the tree
    close_fds=True,
)
```

That `start_new_session=True` matters. Without it, killing the shell later leaves its children —
the `make`, the dev server, the `ssh` — **running after the session ends**.

**Step 2**: read, but be able to stop. This is the subtlest part of the chapter.

```python
while time.monotonic() < deadline:
    if not selector.select(timeout=0.05):
        if self.process.poll() is not None:
            break                                        # exit A: the process died
        if time.monotonic() - last_output > IDLE_QUIET_MS / 1000:
            break                                        # exit B: output went quiet
        continue
    ...read a chunk...
```

Three exits: **the process exited**, **output has been quiet for 120ms**, **the yield window
expired**.

Exit B is what makes an interactive session feel responsive. A REPL that has printed `>>>` and
is waiting for you **has nothing more to say**. Waiting the full 10 seconds to discover that
would make every interaction unusable.

**Step 3**: a PTY gotcha — a child exiting is not EOF, it is `EIO`.

```python
except OSError as exc:
    if exc.errno in (errno.EIO, errno.EBADF):
        self.pty_closed = True  # the child closed the pty: it is gone
        break
    raise
```

On a normal pipe, a child exiting gives you EOF (an empty read). On a PTY you get an **`EIO`
error**. Let it propagate and you report a crash for **every command that completed normally**.

**Step 4**: the pty closing is not the same as the process being reaped. They are milliseconds
apart.

```python
exit_code = session.process.poll()
if exit_code is None and session.pty_closed:
    # The pty closed, so the command really did finish; wait briefly for
    # the exit status rather than reporting a live session that is not.
    # Under load this window is wide enough to matter.
    try:
        exit_code = session.process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        exit_code = None
```

> This branch exists because of a **flaky test**: running the whole suite under load,
> `exec_command("true")` would occasionally come back with a session id, as if `true` were still
> running. Reading `poll()` alone is not enough.

**Step 5**: cap the output, and **tell the model the cap applied**.

```python
class HeadTailBuffer:
    """Keep the beginning and the end, drop the middle."""
```

```
Chunk ID: 9fb9b5
Process exited with code 0
Original token count: 50000          <-- what it would have been
Output:
xxxxxxxxxx...
[... 199900 characters truncated ...]
```

`Original token count` matters more than the truncation: **the model can see it asked for
something enormous**, so its next command narrows the range (`| head -50`) instead of assuming
it saw everything.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `ProcessManager.exec_command` | Spawn under a PTY, read until yield, return or hand back a session |
| `ProcessManager.write_stdin` | Talk to a live session |
| `ExecSession.read_available` | Those three exits |
| `HeadTailBuffer` | Bounded output with a dropped-count |
| `ExecResult.render` | The header above |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| Running a command | `subprocess.run`, wait for exit | a PTY session |
| A command that never exits | hangs the agent forever | returns early with a session id |
| Talking to a running program | impossible | `write_stdin` |
| `cd build` | lost on the next call | kept for the life of the session |
| Killing it | children keep running | the whole process group goes |

---

## Try it

This chapter needs **no API key**:

```bash
python s06_unified_exec/code.py --demo
```

It walks through: a command that finishes, one that does not (handing back a session id),
writing to a live session, the session remembering a variable, closing it, and output over
budget.

**What to watch**: sections three and four. Three sends `print(6 * 7)` and gets `42`; four sends
`x = 'kept'` then `print(x)` and gets `kept` — **the same Python process, the variable still
there**. That is what s01's one-shot `subprocess.run` can never do.

Drive one by hand:

```bash
python s06_unified_exec/code.py --repl
> open python3 -i -q -u
> import os; os.getcwd()
> quit
```

---

## Real source

- `codex-rs/core/src/unified_exec/` — `mod.rs`, `process.rs`, `process_manager.rs`
- `codex-rs/core/src/tools/context.rs` — `response_header`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` — `yield_time_ms`, `max_output_tokens`

---

## Next

The agent can now run any command, keep long-lived sessions, and edit files.

**And all of it runs with your full privileges.** It can write to `~/.ssh/`, `curl` anything
anywhere, `rm -rf` any directory — whenever the model thinks that is a good idea.

[s07](../s07_sandbox/) takes those privileges away, **and has the OS kernel do the taking**.
