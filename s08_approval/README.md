# s08: Approval and escalation — run first, ask only when refused

[English](README.md) · [中文](README.zh.md)

[s07](../s07_sandbox/) → `s08` → [s09](../s09_exec_policy/) → ... → [s15](../s15_harness/)

> *"Run it in the sandbox first. Ask only when the sandbox says no."*
>
> **Harness layer**: human in the loop — when it is right to interrupt the user.

---

## The problem

Your agent tried something and was stopped: installing a dependency, writing `~/.npmrc`,
running a build that needs the network.

**That block might have been right, and it might have been wrong.** Only you know which.

So there is a decision to make, and both extremes are wrong:

- **Ask before every command** → the user gets worn down and starts clicking "allow" without
  reading. At that point your approval mechanism **is no longer a security mechanism**.
- **Never ask** → the agent hits a wall and gives up; half the tasks do not complete.

And asking is itself hard: **if the code running the turn is an ordinary function, it cannot ask
at all.** A function returns once; it cannot "ask, wait for your answer, then carry on" — which
is what [s02](../s02_protocol/) exists to fix.

---

## the order is the design

Most people's instinct is "ask, then run". Codex does the reverse:

```
1. assess     -- auto-approvable, needs a human, or refused outright?
2. run        -- inside the sandbox
3. denied?    -- s07's heuristic fires
4. ask        -- emit ExecApprovalRequest, and await an Op that has not arrived yet
5. retry      -- on approval, run again with the sandbox off
6. remember   -- so the same command is never asked about twice
```

**Why is "run first" safe?** Because of [s07](../s07_sandbox/). The sandbox reduces the worst
case of "just try it" to "the command failed".

**What does the order buy?** The only questions that reach the user are the ones **the kernel
actually raised**. `ls`, `pytest`, `git status` — hundreds of commands, zero prompts.

---

## The solution

Three approval policies. They are **three situations**, not three severity levels:

| Policy | Behavior | When |
|---|---|---|
| `untrusted` | Ask before anything not on the trusted list | An unfamiliar repo, a first run |
| `on-request` | Run sandboxed; ask only when the sandbox blocks it | The default |
| `never` | Never ask; a blocked command fails and the model is told why | **CI — there is nobody to ask** |

`never` is not "the less safe mode". It is **the correct behavior when no human is present.**

---

## How it works

**Step 1**: assess before running.

```python
def assess_command_safety(cmd, *, approval_policy, sandbox_available, approved) -> SafetyCheck:
    if cmd in approved:
        # Already blessed this session: run it unsandboxed without asking again.
        return AutoApprove(sandboxed=False)

    if approval_policy == UNLESS_TRUSTED and not is_trusted(cmd):
        return AskUser("approval policy is `untrusted`")

    if sandbox_available:
        return AutoApprove(sandboxed=True)          # <-- the common path

    # No sandbox on this platform: running is a real risk, so the policy has
    # to decide instead of the kernel.
    if approval_policy == NEVER:
        return Reject("no sandbox available and approval policy is `never`")
    return AskUser("no sandbox available on this platform")
```

Three outcomes: `AutoApprove` (run it), `AskUser` (ask), `Reject` (do not run, do not ask).

Note the last block: **when the sandbox and the human are both missing, refuse.** Failing closed
is the only defensible choice there.

**Step 2**: run it in the sandbox ([s07](../s07_sandbox/)'s code).

```python
self.emit(sub_id, ExecCommandBegin(call_id, cmd, check.sandboxed))
result = await asyncio.to_thread(run_command, cmd, cwd, sandboxed=check.sandboxed)
```

**Step 3**: was it denied?

```python
if is_likely_sandbox_denied(result):
    ...
```

**Step 4 — this is why [s02](../s02_protocol/) exists.** Ask, when the answer does not exist yet.

```python
async def _ask(self, sub_id, call_id, cmd, cwd, reason, justification) -> str:
    """Park the turn on a future. The answer arrives as another Op."""
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    self.pending_approvals[call_id] = future
    self.emit(sub_id, ExecApprovalRequest(call_id, cmd, cwd, reason, justification))
    try:
        return await future                 # <-- parked here, blocking nothing
    finally:
        self.pending_approvals.pop(call_id, None)
```

`await future` means "this function stops here until someone fills in the result". **It holds
no thread and blocks no event loop** — other ops keep being handled, `/interrupt` keeps working.

The answer arrives later as **another submission on the same queue**:

```python
elif isinstance(op, ExecApproval):
    # This is the whole trick: an answer to a question the turn is
    # still waiting on, delivered through the same queue as everything else.
    if not sess.resolve_approval(op.call_id, op.decision):
        sess.emit(sub.id, ErrorEvent(f"no pending approval for {op.call_id}"))
```

```python
def resolve_approval(self, call_id: str, decision: str) -> bool:
    future = self.pending_approvals.get(call_id)
    if future is None or future.done():
        return False
    future.set_result(decision)      # <-- the await above resumes right here
    return True
```

The frontend can render the question however it likes — a TUI dialog, a JSON event, an HTTP
response — because it is just **an event and a submission**.

**Step 5**: on approval, retry with the sandbox off.

```python
if decision in (DENIED, ABORT):
    return "command not run: the user declined the escalation"
if decision == APPROVED_FOR_SESSION:
    self.approved.add(cmd)
retried = await asyncio.to_thread(run_command, cmd, cwd, sandboxed=False)
```

**Step 6**: remember. `approved_for_session` is what makes this usable at all.

A build that needs network access is not one question — it is **one question per attempt**. Ask
the same question six times and the user stops reading it, which puts us back where we started.

---

## Under `never`, the failure message is the product

```python
if self.approval_policy == NEVER:
    return (
        f"Process exited with code {result.exit_code}\n"
        "The sandbox blocked this command and approvals are disabled.\n"
        f"Output:\n{result.output}"
    )
```

**Telling the model *why* it failed is the entire point of these lines.**

- Just `Permission denied` → the model retries, rephrases, goes in circles.
- "The sandbox blocked this and approvals are disabled" → the model knows this road is **closed**
  and goes to find another one, or reports honestly that it cannot proceed.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `assess_command_safety` | `AutoApprove` / `AskUser` / `Reject` |
| `Session._exec_with_approval` | The six steps |
| `_ask` / `resolve_approval` | A future parked in the turn, resolved by an Op |
| `approved` | The session-scoped approval cache |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| When the user is asked | never, or every time | only when the kernel actually refused |
| Asking mid-turn | impossible in a function | a future the turn parks on |
| The answer | n/a | another `Op` on the same queue |
| Asking twice for the same command | every attempt | `approved_for_session` remembers |
| Nobody at the keyboard | hangs, or runs unprotected | `never`: fails, and tells the model why |

---

## Try it

```bash
python s08_approval/code.py "write a file into my home directory"
```

It tries in the sandbox, gets refused, and **asks you**:

```
! the sandbox blocked this command
  command: echo test > ~/scratch.txt
  allow? [y/N/always]
```

Try all three answers and watch what the model receives change.

Then try the mode with nobody to ask:

```bash
python s08_approval/code.py --policy never "write a file into my home directory"
```

**What to watch**: under `never` there is no prompt, and the model receives
`The sandbox blocked this command and approvals are disabled.` — then see what it does next. A
good model writes somewhere else, or tells you plainly that it cannot.

---

## Real source

- `codex-rs/core/src/safety.rs` — `SafetyCheck`
- `codex-rs/core/src/tools/approvals.rs`, `tools/sandboxing.rs`
- `codex-rs/protocol/src/protocol.rs` — `AskForApproval`, `ReviewDecision`

---

## Next

It can ask now. But asking has a problem of its own:

Was `git status` blocked by the sandbox? No — it only reads. `ls`? Also no. Yet under
`untrusted`, every one of them raises a prompt, because the harness **has no idea which commands
are safe**.

[s09](../s09_exec_policy/) gives it a rule file so it can **decide without asking** — and lets an
organization say "`git push --force`, never" in a way the model cannot argue with.
