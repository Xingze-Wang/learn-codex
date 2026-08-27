# s07: The sandbox

[English](README.md) · [中文](README.zh.md)

[s06](../s06_unified_exec/) → `s07` → [s08](../s08_approval/)

> *"Ask the kernel, not the command string."*

---

Every chapter so far ran commands with the user's full privileges. Codex does not. By default a
command can read the machine, can write only inside the workspace, and cannot reach the network
at all — enforced by the operating system, not by inspecting what the command looks like.

```
macOS    /usr/bin/sandbox-exec -p <SBPL policy> -DWRITABLE_ROOT_0=... -- cmd
Linux    Landlock (filesystem) + seccomp (network syscalls)
other    no sandbox available -> the harness must ask the user instead
```

Three policies, the same three words the user sees in the TUI:

| Policy | Read | Write | Network |
|---|---|---|---|
| `read-only` | anywhere | nowhere | no |
| `workspace-write` | anywhere | cwd, `$TMPDIR`, `/tmp` | no |
| `danger-full-access` | anywhere | anywhere | yes |

Reading is unrestricted even under `read-only`, which surprises people. It is deliberate: an
agent that cannot read the machine cannot do its job, and the thing that actually protects
secrets is the network deny — data the agent can read but cannot send is data that stays put.

Running `--demo` on macOS produces this, and it is real enforcement, not a simulation:

```
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

## Two details decide whether this holds

**Real paths.** `/var/folders/...` is a symlink to `/private/var/folders/...`, and seatbelt
matches the resolved path.

```python
# Symlinked paths must be resolved or the kernel check never matches.
real = os.path.realpath(root)
```

Skip that and the sandbox *looks* like it works — it starts, it runs the command — but the
writable root grants nothing and every write fails. That failure mode is worse than no sandbox,
because it produces a confident-looking policy that does the opposite of what it says.

**Paths are parameters, never interpolated.**

```python
key = f"WRITABLE_ROOT_{index}"
clauses.append(f'(subpath (param "{key}"))')
params.append(f"-D{key}={root}")
```

A directory literally named `foo") (allow file-write* (subpath "/` would otherwise rewrite the
policy. This is SQL injection wearing a different hat, and the fix is the same one: pass data as
data.

## Denial is a guess

The kernel returns `EPERM`. It does not say "the sandbox did this".

```python
DENIAL_MARKERS = ("operation not permitted", "permission denied", "read-only file system", ...)

def is_likely_sandbox_denied(output) -> bool:
    """False negatives cost a retry. False positives re-run a command outside
    the sandbox that never needed to be, so this stays narrow on purpose."""
```

The asymmetry sets the threshold. A missed denial means the model sees a confusing error and
tries something else — annoying. A false positive means the harness asks the user to run
something unsandboxed that had an ordinary bug — a security prompt for no reason, which trains
the user to click through prompts. Narrow wins.

Note what the demo does *not* flag: `curl -s` fails silently under `read-only`, so no marker
appears in stderr and the heuristic stays quiet. That is the honest limit of the approach, and
it is why s08 never treats "not denied" as "definitely fine".

## In `code.py`

| Piece | Job |
|---|---|
| `SandboxPolicy` | Mode, writable roots, network |
| `effective_writable_roots` | cwd + tmp carve-outs, resolved |
| `build_seatbelt_policy` | SBPL text plus `-D` parameters |
| `run_sandboxed` | Spawn under `sandbox-exec` |
| `is_likely_sandbox_denied` | The heuristic that triggers s08 |

## Run it

```bash
python s07_sandbox/code.py --demo         # the same probes under all four policies
python s07_sandbox/code.py --policy       # print the generated SBPL
python s07_sandbox/code.py --run "ls"     # one command under workspace-write
```

The demo enforces policies only on macOS. On Linux, Codex uses Landlock plus seccomp; the policy
shapes still print correctly.

## Real source

- `codex-rs/sandboxing/src/seatbelt.rs`, `seatbelt_base_policy.sbpl`
- `codex-rs/sandboxing/src/landlock.rs`
- `codex-rs/protocol/src/protocol.rs` — `SandboxPolicy`

## Next

The sandbox says no. [s08](../s08_approval/) is what happens next.
