# s14: Hooks

[English](README.md) · [中文](README.zh.md)

[s13](../s13_mcp/) → `s14` → [s15](../s15_harness/)

> *"Everything a hook returns is advisory, except `deny`."*

---

The harness so far is fixed: its policies live in code and change only when someone edits it.
Hooks let a user or an organization put their own program on the agent's path without forking
anything.

A hook is a program. Codex writes JSON to its stdin and reads JSON from its stdout:

```json
{"session_id": "...", "cwd": "/repo", "hook_event_name": "PreToolUse",
 "tool_name": "exec_command", "tool_input": {"cmd": "git push"}}
```

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "pushes go through CI"}}
```

Eleven events, and the useful ones split into two kinds:

| Events | Can do |
|---|---|
| `SessionStart`, `UserPromptSubmit`, `SubagentStart` | return `additionalContext` |
| `PreToolUse` | return `allow` / `deny` / `ask` |
| `PostToolUse`, `Stop`, `SessionEnd`, `PreCompact`, `Interrupt`, `SubagentStop`, `PermissionRequest` | observe, or add context |

The first kind writes into the conversation. The second decides whether something happens at
all.

## Failure modes are the design

`deny` is the only hook result that can stop the agent. Everything else — a crash, a hang,
garbage on stdout — degrades to "run without this hook and say so":

```
[context injected at session start] House rule: never edit files under vendor/.
$ pytest -q
[exec_command denied by hook] pushes go through CI, not the agent
[exec_command escalated to the user by hook] recursive delete
$ *** Begin Patch ...

warnings collected along the way (nothing aborted the session):
  PreToolUse: hook timed out after 0.3s: sleep 5
  PreToolUse: hook exited 7: exit 7
```

Three specific choices:

```python
if not stdout:
    return None, "", "", ""     # no opinion, not an error
```

A hook that prints nothing has no opinion. A hook that prints something malformed also has no
opinion — treating a parse failure as a denial would mean a typo in someone's script silently
disables their agent.

```python
except subprocess.TimeoutExpired:
    outcome.warnings.append(f"hook timed out after {hook.timeout}s: {hook.command}")
```

Timeouts are mandatory: a hook is a subprocess someone else wrote, and it runs before every
single tool call.

```python
context = str(specific.get("additionalContext") or "")[:limit]
```

Injected context is bounded by `additionalContextLimit`. Without it, one chatty hook quietly
eats the context window s11 exists to protect — and it does so on every request, forever.

## Matchers fail closed

```python
try:
    return re.search(self.matcher, subject) is not None
except re.error:
    return False
```

An invalid regex matches nothing. A typo in a matcher meant to *narrow* a rule must not
accidentally *widen* it — `^exec_comand$` should silently do nothing, not fire on everything.

## First deny wins

```python
if decision == DENY:
    # First deny wins and the rest are skipped: the tool is not
    # going to run, so asking the remaining hooks about it is noise.
    return outcome
```

## Where they sit in a turn

```
SessionStart      -> additionalContext into the conversation
UserPromptSubmit  -> may inject, may block the prompt
  PreToolUse      -> allow / deny / ask, per call
  (tool runs)
  PostToolUse     -> observe
Stop              -> systemMessage
SessionEnd
```

`turn_with_hooks` in `code.py` runs exactly that sequence with the model stubbed out, so the
placement is visible without an API key.

## In `code.py`

| Piece | Job |
|---|---|
| `HookConfig.load` | Parse `hooks.json`, report what it cannot parse |
| `MatcherGroup.matches` | Regex over the tool name, failing closed |
| `HookRunner.run` | Spawn, feed JSON, time out, collect |
| `parse_hook_output` | The wire shape, leniently |
| `turn_with_hooks` | Where each event fires |

## Run it

```bash
python s14_hooks/code.py --demo        # builds a hooks.json and fires every event
python s14_hooks/code.py --show        # reads your real ~/.codex/hooks.json
```

## Real source

- `codex-rs/hooks/` — `engine/dispatcher.rs`, `engine/command_runner.rs`, `engine/output_parser.rs`, `schema.rs`
- `codex-rs/core/src/hook_runtime.rs`

## Next

Fourteen mechanisms, fourteen files. [s15](../s15_harness/) runs them in one process.
