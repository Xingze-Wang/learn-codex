#!/usr/bin/env python3
"""
s07: The sandbox

Every previous chapter ran commands with the user's full privileges. Codex does
not. The default is that a command can read the machine but can only write
inside the workspace, and cannot reach the network at all.

This is enforced by the OS, not by inspecting the command string. Codex asks
the platform for a sandbox and spawns the command inside it:

    macOS    /usr/bin/sandbox-exec -p <SBPL policy> -DWRITABLE_ROOT_0=... -- cmd
    Linux    Landlock (filesystem) + seccomp (network syscalls)
    other    no sandbox available -> the harness must ask the user instead

Three policies, and they are the same three words the user sees in the TUI:

    read-only            read anywhere, write nowhere, no network
    workspace-write      + write under cwd (and $TMPDIR, /tmp)
    danger-full-access   no sandbox at all

Two details decide whether this actually holds:

  * **Real paths.** `/var/folders/...` is a symlink to `/private/var/folders/...`
    and seatbelt matches the resolved path. A writable root that is not
    `realpath`-ed silently grants nothing -- the sandbox looks like it works
    and every write fails.
  * **Denial is a guess.** The kernel returns EPERM; it does not say "this was
    the sandbox". Codex uses a heuristic over the exit code and stderr, and
    that guess is what triggers the escalation flow in s08.

Run:
  python s07_sandbox/code.py --demo          # the same commands under all three policies
  python s07_sandbox/code.py --policy        # print the generated SBPL
  python s07_sandbox/code.py --run "cmd"     # run one command under workspace-write

Real source: codex-rs/sandboxing/src/seatbelt.rs, seatbelt_base_policy.sbpl,
codex-rs/sandboxing/src/landlock.rs, codex-rs/protocol/src/protocol.rs (SandboxPolicy)
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
DANGER_FULL_ACCESS = "danger-full-access"


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxPolicy:
    mode: str = WORKSPACE_WRITE
    writable_roots: tuple[str, ...] = ()
    network_access: bool = False
    exclude_tmpdir: bool = False
    exclude_slash_tmp: bool = False

    def effective_writable_roots(self, cwd: str) -> list[str]:
        """cwd is always writable under workspace-write; tmp usually is too."""
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


def platform_sandbox() -> str | None:
    """What this machine can actually enforce."""
    system = platform.system()
    if system == "Darwin" and os.path.exists(SANDBOX_EXEC):
        return "seatbelt"
    if system == "Linux":
        return "landlock"  # codex-rs/sandboxing/src/landlock.rs; not reimplemented here
    return None


# --------------------------------------------------------------------------
# Seatbelt policy generation
# --------------------------------------------------------------------------

BASE_POLICY = """\
(version 1)

; closed by default -- everything below is an explicit carve-out
(deny default)

; child processes inherit this policy
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)

; a process that cannot write to /dev/null cannot run a shell
(allow file-write-data
  (require-all
    (path "/dev/null")
    (vnode-type CHARACTER-DEVICE)))
(allow file-write-data (path "/dev/dtracehelper"))
(allow file-ioctl (path "/dev/dtracehelper"))
"""

READ_POLICY = "; reading is unrestricted; secrets are protected by the network deny\n(allow file-read*)"

NETWORK_POLICY = """\
; outbound network, only when the policy explicitly enables it
(allow network-outbound)
(allow network-inbound)
(allow system-socket)
(allow mach-lookup)
"""


def build_seatbelt_policy(policy: SandboxPolicy, cwd: str) -> tuple[str, list[str]]:
    """Returns (SBPL text, -D parameter definitions).

    Paths go in as *parameters*, never interpolated into the policy text: a
    directory named `foo") (allow file-write* (subpath "/` would otherwise
    rewrite the policy.
    """
    sections = [BASE_POLICY, READ_POLICY]
    params: list[str] = []

    roots = policy.effective_writable_roots(cwd)
    if policy.mode == DANGER_FULL_ACCESS:
        sections.append('(allow file-write* (regex #"^/"))')
    elif roots:
        clauses = []
        for index, root in enumerate(roots):
            key = f"WRITABLE_ROOT_{index}"
            clauses.append(f'(subpath (param "{key}"))')
            params.append(f"-D{key}={root}")
        sections.append("; writable roots\n(allow file-write*\n  " + "\n  ".join(clauses) + ")")

    if policy.network_access:
        sections.append(NETWORK_POLICY)

    return "\n\n".join(sections) + "\n", params


# --------------------------------------------------------------------------
# Spawning
# --------------------------------------------------------------------------


@dataclass
class ExecOutput:
    exit_code: int
    stdout: str
    stderr: str
    sandboxed: bool

    @property
    def aggregated(self) -> str:
        return self.stdout + self.stderr


def build_command(cmd: str, policy: SandboxPolicy, cwd: str) -> list[str]:
    inner = ["/bin/bash", "-lc", cmd]
    if policy.mode == DANGER_FULL_ACCESS or platform_sandbox() != "seatbelt":
        return inner
    text, params = build_seatbelt_policy(policy, cwd)
    return [SANDBOX_EXEC, "-p", text, *params, "--", *inner]


def run_sandboxed(cmd: str, policy: SandboxPolicy, cwd: str, timeout: float = 120) -> ExecOutput:
    argv = build_command(cmd, policy, cwd)
    sandboxed = argv[0] == SANDBOX_EXEC
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ExecOutput(124, "", "command timed out", sandboxed)
    return ExecOutput(proc.returncode, proc.stdout, proc.stderr, sandboxed)


# --------------------------------------------------------------------------
# Denial detection -- a guess, and it must be a conservative one
# --------------------------------------------------------------------------

DENIAL_MARKERS = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
    "sandbox-exec: execvp",
    "could not resolve host",
    "network is unreachable",
    "connection refused",
)


def is_likely_sandbox_denied(output: ExecOutput) -> bool:
    """False negatives cost a retry. False positives re-run a command outside
    the sandbox that never needed to be, so this stays narrow on purpose."""
    if not output.sandboxed or output.exit_code == 0:
        return False
    haystack = output.aggregated.lower()
    return any(marker in haystack for marker in DENIAL_MARKERS)


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------

PROBES = [
    ("read a file", "head -1 /etc/hosts >/dev/null && echo read-ok"),
    ("write inside the workspace", "echo hi > probe.txt && echo write-ok"),
    ("write outside the workspace", "echo hi > $HOME/.learn-codex-probe && echo escaped"),
    ("reach the network", "curl -s -m 3 -o /dev/null https://example.com && echo net-ok"),
]


def demo() -> int:
    backend = platform_sandbox()
    print(f"platform sandbox: {backend or 'none available'}\n")
    if backend != "seatbelt":
        print("This demo enforces policies only on macOS. On Linux, codex uses")
        print("Landlock + seccomp (codex-rs/sandboxing/src/landlock.rs); the policy")
        print("shapes below still print correctly.\n")

    workdir = os.path.realpath(tempfile.mkdtemp(prefix="learn-codex-s07-"))
    policies = [
        SandboxPolicy(mode=READ_ONLY),
        SandboxPolicy(mode=WORKSPACE_WRITE),
        SandboxPolicy(mode=WORKSPACE_WRITE, network_access=True),
        SandboxPolicy(mode=DANGER_FULL_ACCESS),
    ]

    width = max(len(label) for label, _ in PROBES)
    for policy in policies:
        net = " +network" if policy.network_access else ""
        print(f"== {policy.mode}{net} ==")
        for label, cmd in PROBES:
            result = run_sandboxed(cmd, policy, workdir, timeout=15)
            verdict = "allowed" if result.exit_code == 0 else "blocked"
            flag = " (looks like a sandbox denial)" if is_likely_sandbox_denied(result) else ""
            print(f"  {label:<{width}}  {verdict}{flag}")
        print()

    probe = os.path.expanduser("~/.learn-codex-probe")
    if os.path.exists(probe):
        os.unlink(probe)
    return 0


def main(argv: list[str]) -> int:
    if "--policy" in argv:
        text, params = build_seatbelt_policy(SandboxPolicy(), os.getcwd())
        print(text)
        print("parameters:", " ".join(params))
        return 0
    if "--run" in argv:
        cmd = argv[argv.index("--run") + 1]
        result = run_sandboxed(cmd, SandboxPolicy(), os.getcwd())
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if is_likely_sandbox_denied(result):
            print("[likely blocked by the sandbox]", file=sys.stderr)
        return result.exit_code
    return demo()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
