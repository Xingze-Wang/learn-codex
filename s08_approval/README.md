# s08: Approval and escalation

[English](README.md) · [中文](README.zh.md)

[s07](../s07_sandbox/) → `s08` → [s09](../s09_exec_policy/)

> *"Run it in the sandbox first. Ask only when the sandbox says no."*

---

Codex does not ask before running a command. It runs it sandboxed, and asks only when the
sandbox blocks it:

```
1. assess     -- auto-approvable, needs a human, or refused outright?
2. run        -- inside the sandbox
3. denied?    -- the heuristic from s07 fires
4. ask        -- emit ExecApprovalRequest, and await an Op that has not arrived yet
5. retry      -- on approval, run again with the sandbox off
6. remember   -- so the same command is never asked about twice
```

The ordering is the design. Asking first would mean a prompt for every `ls`; the sandbox makes
"just run it" the safe default, so the only questions that reach the user are the ones the
kernel actually raised.

## Step 4 is why s02 exists

```python
async def _ask(self, sub_id, call_id, cmd, cwd, reason, justification) -> str:
    """Park the turn on a future. The answer arrives as another Op."""
    future = asyncio.get_running_loop().create_future()
    self.pending_approvals[call_id] = future
    self.emit(sub_id, ExecApprovalRequest(call_id, cmd, cwd, reason, justification))
    return await future
```

The turn is a coroutine parked on a future. The answer arrives later as another submission on
the same queue:

```python
elif isinstance(op, ExecApproval):
    # This is the whole trick: an answer to a question the turn is
    # still waiting on, delivered through the same queue as everything else.
    if not sess.resolve_approval(op.call_id, op.decision):
        sess.emit(sub.id, ErrorEvent(f"no pending approval for {op.call_id}"))
```

Nothing blocks. `Op::Interrupt` still works while the question is on screen. The frontend can
render the question however it likes — a TUI dialog, a JSON event, an HTTP response — because
it is just an event and a submission.

## The policies are different situations, not severity levels

| Policy | Behavior |
|---|---|
| `untrusted` | Ask before anything not on the trusted list |
| `on-request` | Run sandboxed; ask only when the sandbox blocks it (default) |
| `never` | Never ask; a blocked command fails and the model is told |

`never` is not "less safe". It is the mode for CI, where there is nobody to ask:

```python
if self.approval_policy == NEVER:
    return (
        f"Process exited with code {result.exit_code}\n"
        "The sandbox blocked this command and approvals are disabled.\n"
        f"Output:\n{result.output}"
    )
```

Telling the model *why* it failed is the whole point. "Permission denied" invites a retry loop;
"the sandbox blocked this and approvals are disabled" tells the model to find another route or
report that it cannot proceed.

## Remembering

```python
if cmd in approved:
    # Already blessed this session: run it unsandboxed without asking again.
    return AutoApprove(sandboxed=False)
```

`approved_for_session` is what keeps this usable. A build that needs network access is not one
question, it is one question per attempt, and a user asked the same question six times stops
reading it. Codex also supports persisting the decision as an execpolicy rule — that is s09.

## What "no sandbox available" means

```python
if approval_policy == NEVER:
    return Reject("no sandbox available and approval policy is `never`")
return AskUser("no sandbox available on this platform")
```

On a platform with no sandbox, "run it and see" is not a safe default any more, so the policy has
to decide instead of the kernel. Under `never` there is no one to decide, so the command is
refused rather than run unprotected. Failing closed is the only defensible choice when both the
sandbox and the human are missing.

## In `code.py`

| Piece | Job |
|---|---|
| `assess_command_safety` | `AutoApprove` / `AskUser` / `Reject` |
| `Session._exec_with_approval` | The six steps |
| `_ask` / `resolve_approval` | Future parked in the turn, resolved by an Op |
| `approved` | Session-scoped approval cache |

## Run it

```bash
python s08_approval/code.py "write a file into my home directory"
python s08_approval/code.py --policy never "..."
```

## Real source

- `codex-rs/core/src/safety.rs` — `SafetyCheck`
- `codex-rs/core/src/tools/approvals.rs`, `tools/sandboxing.rs`
- `codex-rs/protocol/src/protocol.rs` — `AskForApproval`, `ReviewDecision`

## Next

Asking works, but asking too often is its own failure. [s09](../s09_exec_policy/) is how Codex
decides without asking.
