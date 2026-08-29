# s14: Hooks — putting someone else's program on the agent's path

[English](README.md) · [中文](README.zh.md)

[s13](../s13_mcp/) → `s14` → [s15](../s15_harness/)

> *"Everything a hook returns is advisory, except `deny`."*
>
> **Harness layer**: the extension point — changing behavior without forking.

---

## The problem

So far every harness policy lives in the code: how the sandbox is configured, when the user is
asked, what context gets injected.

But every team's rules differ:

- "The agent never pushes directly; pushes go through CI."
- "Inject our style guide at the start of every session."
- "Deletions always need a human, even when the sandbox allows them."

[s09](../s09_exec_policy/)'s rule file can express the first, but not the second or third — it
only prefix-matches **commands** and cannot run arbitrary logic.

And asking every team to fork Codex is obviously not the answer.

---

## First: a hook is just a program

Codex does not invent a config language. It does this instead: **run a program you wrote, pass
JSON on stdin/stdout.**

What your program receives (stdin):

```json
{"session_id": "...", "cwd": "/repo", "hook_event_name": "PreToolUse",
 "tool_name": "exec_command", "tool_input": {"cmd": "git push"}}
```

What your program prints (stdout):

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "pushes go through CI"}}
```

That is all. **Any language works** — Python, bash, Go, a compiled binary. It only has to read
stdin and write stdout.

A complete hook can be this short:

```python
import json, sys
payload = json.load(sys.stdin)
cmd = (payload.get("tool_input") or {}).get("cmd", "")
if "git push" in cmd:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "pushes go through CI, not the agent",
    }}))
```

---

## The solution

Eleven events, in two kinds by **what they can do**:

| Events | What they can do |
|---|---|
| `SessionStart`, `UserPromptSubmit`, `SubagentStart` | return `additionalContext` (**write into the conversation**) |
| `PreToolUse` | return `allow` / `deny` / `ask` (**decide whether it happens**) |
| `PostToolUse`, `Stop`, `SessionEnd`, `PreCompact`, `Interrupt`, `SubagentStop`, `PermissionRequest` | observe, or add context |

You declare them in `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^exec_command$",
        "hooks": [
          {"type": "command", "command": "python3 ~/.codex/hooks/guard.py", "timeout": 3}
        ]
      }
    ]
  }
}
```

`matcher` is a regex over the **tool name**. No matcher means "every tool".

---

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

`turn_with_hooks` in `code.py` runs **exactly that sequence** with the model stubbed out, so the
placement is visible without an API key.

---

## How it works: the failure modes are the design

The real content of this chapter is not "how to run a subprocess" — it is **what happens when
someone else's program misbehaves.**

**`deny` is the only result that can stop the agent.** Everything else — a crash, a hang,
garbage on stdout — degrades to "run without this hook and say so".

Four rules below, each matching a real kind of broken hook.

**One: a hook that prints nothing has no opinion.**

```python
stdout = stdout.strip()
if not stdout:
    return None, "", "", ""
try:
    payload = json.loads(stdout)
except json.JSONDecodeError:
    return None, "", "", ""      # printed something malformed = also no opinion
```

**Why can a parse failure not count as a denial?** Because then a typo in someone's script (a
stray `print("debugging")`) would **silently disable their agent**, and they would have a hard
time finding out why.

**Two: timeouts are mandatory.**

```python
except subprocess.TimeoutExpired:
    outcome.warnings.append(f"hook timed out after {hook.timeout}s: {hook.command}")
    return None, "", "", ""
```

A hook is a subprocess someone else wrote, and it runs **before every single tool call**. One
hook that forgot a timeout can wedge the whole agent on a network request.

**Three: injected context must be bounded.**

```python
context = str(specific.get("additionalContext") or "")[:limit]
```

`additionalContextLimit` defaults to 2000 characters. Without it, one chatty hook quietly eats
the context window [s11](../s11_compaction/) is protecting — **on every request, forever.**

**Four: a broken matcher fails closed, not open.**

```python
try:
    return re.search(self.matcher, subject) is not None
except re.error:
    return False
```

An invalid regex matches **nothing**. A typo in a matcher meant to *narrow* a rule
(`^exec_comand$`, one m short) should silently do nothing — not accidentally fire on everything.

---

## First deny wins

```python
if decision == DENY:
    # First deny wins and the rest are skipped: the tool is not
    # going to run, so asking the remaining hooks about it is noise.
    outcome.decision = DENY
    outcome.reason = reason or f"blocked by hook: {hook.command}"
    return outcome
```

There is also a shortcut that needs no JSON: **exit code 2 means deny**, and stderr is the
reason.

```python
if proc.returncode == 2:
    return DENY, proc.stderr.strip() or "blocked by hook", "", ""
```

So the shortest possible hook is a line of shell:
`grep -q "git push" && echo "pushes go through CI" >&2 && exit 2`.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `HookConfig.load` | Parse `hooks.json`, report what it cannot parse |
| `MatcherGroup.matches` | Regex over the tool name, failing closed |
| `HookRunner.run` | Spawn, feed JSON, time out, collect |
| `parse_hook_output` | The wire shape, leniently |
| `turn_with_hooks` | Where each event fires |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| Changing harness policy | fork the project | write a program, list it in `hooks.json` |
| Blocking a tool call | not available to you | `PreToolUse` returning `deny` |
| Injecting your own context | not available to you | `additionalContext`, bounded |
| A hook that crashes or hangs | n/a | a warning; the session continues |

---

## Try it

**No API key needed:**

```bash
python s14_hooks/code.py --demo
```

It builds a temporary `hooks.json` with **three deliberately broken hooks** mixed in: one that
times out, one that exits 7, one that prints non-JSON. Then it runs a whole turn:

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

**What to watch**: those last two warning lines. Not one of the three broken hooks stopped the
session — they were noted, and the agent carried on. That is what "everything is advisory except
`deny`" means in practice.

Then read your **real** config:

```bash
python s14_hooks/code.py --show
```

---

## Real source

- `codex-rs/hooks/` — `engine/dispatcher.rs`, `engine/command_runner.rs`, `engine/output_parser.rs`, `schema.rs`
- `codex-rs/core/src/hook_runtime.rs`

---

## Next

Fourteen mechanisms, fourteen standalone files, each demonstrated alone.

[s15](../s15_harness/) puts them in **the same process** and runs a real turn through all of them
— answering one question: **around a single `exec_command`, in what order do all these checks
go?**
