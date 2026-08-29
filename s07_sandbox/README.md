# s07: The sandbox — ask the kernel, not the command string

[English](README.md) · [中文](README.zh.md)

[s06](../s06_unified_exec/) → `s07` → [s08](../s08_approval/) → ... → [s15](../s15_harness/)

> *"Ask the kernel, not the command string."*
>
> **Harness layer**: the boundary — enforced by the OS, not judged by code.

---

## The problem

Every command in the first six chapters ran with your full privileges. Whatever the model wrote,
your machine executed.

The first instinct is usually: **"I'll inspect the command string and block the dangerous ones."**

That road does not work — not because you were not careful enough, but because it is **not
possible**:

```bash
rm -rf /                          # you can catch this
rm -rf $HOME/../../              # and this
$(echo cm0gLXJmIC8K | base64 -d) # you find out what it is at runtime
python3 -c "import os; os.remove(...)"   # not an `rm` at all
make                              # who knows what the Makefile does
```

**The problem is not that your blocklist is too short. It is that "what will this command do" is
undecidable before it runs.**

So Codex does not judge the command. It **changes what the command is able to do.**

---

## First: what an OS sandbox is

Modern operating systems all offer this: **before starting a process, wrap it in restrictions
that the kernel enforces, that the process cannot undo, and that its children inherit.**

| Platform | Mechanism | How you use it |
|---|---|---|
| macOS | Seatbelt | `/usr/bin/sandbox-exec -p '<policy>' -- your command` |
| Linux | Landlock (files) + seccomp (syscalls) | the process declares its own restrictions at start |
| other | none | the harness has to ask the user instead ([s08](../s08_approval/)) |

On macOS the policy is written in a small Lisp-ish language called **SBPL**. It reads about how
you would guess:

```lisp
(version 1)

; deny everything by default
(deny default)

; then open specific holes
(allow process-exec)
(allow process-fork)
(allow file-read*)                                   ; reading: anywhere
(allow file-write* (subpath (param "WRITABLE_ROOT_0")))   ; writing: only under this directory
```

`(deny default)` is the crux: **closed first, then carve out.** The reverse — open everything,
then block the dangerous parts — is the "enumerate all danger" road that does not work.

Run `python s07_sandbox/code.py --policy` to see the whole generated policy.

---

## The solution

Three policies, the same three words the user sees in the UI:

| Policy | Read | Write | Network |
|---|---|---|---|
| `read-only` | anywhere | nowhere | no |
| `workspace-write` | anywhere | cwd, `$TMPDIR`, `/tmp` | no |
| `danger-full-access` | anywhere | anywhere | yes |

**Why is reading unrestricted under every policy?** It surprises people, and it is deliberate:

- An agent that cannot read the machine cannot do its job.
- What actually protects secrets is the **network deny**: data the agent can read but **cannot
  send** is data that stays put.

---

## How it works

**Step 1**: work out which directories are actually writable this turn.

```python
def effective_writable_roots(self, cwd: str) -> list[str]:
    if self.mode == DANGER_FULL_ACCESS:
        return ["/"]
    if self.mode == READ_ONLY:
        return []

    roots = [cwd, *self.writable_roots]
    if not self.exclude_tmpdir and os.environ.get("TMPDIR"):
        roots.append(os.environ["TMPDIR"])
    if not self.exclude_slash_tmp:
        roots.append("/tmp")
    # Symlinked paths must be resolved or the kernel check never matches.
    resolved = []
    for root in roots:
        real = os.path.realpath(root)
        if real not in resolved:
            resolved.append(real)
    return resolved
```

**That `realpath` at the end is the easiest line to miss and the most damaging to miss.**

On macOS `/var/folders/...` is a symlink to `/private/var/folders/...`, and seatbelt matches the
**resolved** path.

What happens if you skip it? The sandbox **looks** like it works — it starts, the command runs —
but the writable root grants nothing, so **every write fails**. That failure is worse than having
no sandbox: you end up with a confident-looking policy that does the opposite of what it says.

**Step 2**: build the policy, but pass **paths as parameters, never as text**.

```python
key = f"WRITABLE_ROOT_{index}"
clauses.append(f'(subpath (param "{key}"))')      # the policy text has only a placeholder name
params.append(f"-D{key}={root}")                  # the real path goes via the command line
```

Why not interpolate the path into the policy string? Because a directory literally named

```
foo") (allow file-write* (subpath "/
```

would rewrite your policy into **allow writing the whole disk**.

**This is SQL injection wearing a different hat**, and the fix is the same one: pass data as
data.

**Step 3**: spawn under the sandbox.

```python
def build_command(cmd: str, policy: SandboxPolicy, cwd: str) -> list[str]:
    inner = ["/bin/bash", "-lc", cmd]
    if policy.mode == DANGER_FULL_ACCESS or platform_sandbox() != "seatbelt":
        return inner
    text, params = build_seatbelt_policy(policy, cwd)
    return [SANDBOX_EXEC, "-p", text, *params, "--", *inner]
```

Note the `platform_sandbox() != "seatbelt"` branch: **when this machine has no sandbox, this
code does not pretend it does.** It returns the unsandboxed command and lets
[s08](../s08_approval/) decide whether running without one is acceptable.

**Step 4**: decide whether a failure was the sandbox — and know that this is only a **guess**.

```python
DENIAL_MARKERS = ("operation not permitted", "permission denied",
                  "read-only file system", ...)

def is_likely_sandbox_denied(output) -> bool:
    """False negatives cost a retry. False positives re-run a command outside
    the sandbox that never needed to be, so this stays narrow on purpose."""
    if not output.sandboxed or output.exit_code == 0:
        return False
    haystack = output.aggregated.lower()
    return any(marker in haystack for marker in DENIAL_MARKERS)
```

The kernel returns `EPERM`. It **does not say "the sandbox did this"**. So the only evidence is
the error text.

An asymmetry sets the threshold:

- **A missed denial** → the model sees a confusing error and tries another way. Annoying.
- **A false positive** → the harness asks the user to approve running **outside the sandbox** a
  command that just had an ordinary bug. A security prompt for no reason — which is exactly how
  users are trained to click through prompts ([s09](../s09_exec_policy/) picks this up).

**Narrow wins.**

---

## Run it and see that it is real

```bash
python s07_sandbox/code.py --demo
```

```
platform sandbox: seatbelt

== read-only ==
  read a file                  allowed
  write inside the workspace   blocked (looks like a sandbox denial)
  write outside the workspace  blocked (looks like a sandbox denial)
  reach the network            blocked

== workspace-write ==
  read a file                  allowed
  write inside the workspace   allowed
  write outside the workspace  blocked (looks like a sandbox denial)
  reach the network            blocked
```

Not a simulation — that is the kernel refusing.

**What to watch**: under `read-only`, even "write inside the workspace" is blocked — that is
what read-only means. Under `workspace-write` the same command becomes allowed, while "write
outside the workspace" stays blocked.

And one honest rough edge: the `reach the network` line is **not** flagged
`(looks like a sandbox denial)`. `curl -s` fails silently, so no marker appears in stderr and
the heuristic stays quiet. **That is the real limit of the approach**, and it is why
[s08](../s08_approval/) never treats "not denied" as "definitely fine".

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `SandboxPolicy` | Mode, writable roots, network |
| `effective_writable_roots` | cwd + tmp carve-outs, resolved |
| `build_seatbelt_policy` | SBPL text plus `-D` parameters |
| `run_sandboxed` | Spawn under `sandbox-exec` |
| `is_likely_sandbox_denied` | The heuristic that triggers [s08](../s08_approval/) |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| Privileges | yours, in full | what the kernel allows this process |
| Writing | anywhere on disk | only inside the writable roots |
| Network | open | closed by default |
| How danger is judged | inspect the command string (impossible) | not judged — the command's *ability* is changed |

---

## Try it

**No API key needed:**

```bash
python s07_sandbox/code.py --demo         # the same probes under all four policies
python s07_sandbox/code.py --policy       # print the generated SBPL and read it
python s07_sandbox/code.py --run "echo hi > /etc/hosts"   # hit the wall yourself
```

The demo only enforces on macOS. On Linux, Codex uses Landlock + seccomp
(`codex-rs/sandboxing/src/landlock.rs`); the policy shapes still print correctly.

---

## Real source

- `codex-rs/sandboxing/src/seatbelt.rs`, `seatbelt_base_policy.sbpl`
- `codex-rs/sandboxing/src/landlock.rs`
- `codex-rs/protocol/src/protocol.rs` — `SandboxPolicy`

---

## Next

The sandbox said no. Then what?

The model wants to write `~/.npmrc`, which is a reasonable thing to want. The command failed
with a non-zero exit. **Who decides whether to let it out?**

[s08](../s08_approval/) is the six steps: assess → run sandboxed → denied? → ask the user →
retry with the sandbox off → remember, so it is never asked twice.
