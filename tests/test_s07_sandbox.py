from __future__ import annotations

import os
import platform

import pytest
from helpers import load

mod = load("s07_sandbox")

on_macos = pytest.mark.skipif(
    platform.system() != "Darwin" or not os.path.exists(mod.SANDBOX_EXEC),
    reason="seatbelt is macOS-only",
)


def test_read_only_has_no_writable_roots():
    policy = mod.SandboxPolicy(mode=mod.READ_ONLY)
    assert policy.effective_writable_roots("/repo") == []


def test_workspace_write_always_includes_cwd(tmp_path):
    policy = mod.SandboxPolicy(mode=mod.WORKSPACE_WRITE)
    roots = policy.effective_writable_roots(str(tmp_path))
    assert os.path.realpath(str(tmp_path)) in roots


def test_writable_roots_are_resolved(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    roots = mod.SandboxPolicy(mode=mod.WORKSPACE_WRITE).effective_writable_roots(str(link))
    # A symlinked root that is not resolved matches nothing in the kernel check.
    assert str(real.resolve()) in roots


def test_paths_are_passed_as_parameters_not_interpolated(tmp_path):
    hostile = tmp_path / 'evil") (allow file-write* (subpath "/'
    hostile.mkdir()
    text, params = mod.build_seatbelt_policy(mod.SandboxPolicy(), str(hostile))
    assert 'allow file-write* (subpath "/")' not in text
    assert any(str(hostile.resolve()) in p for p in params)


def test_network_clause_only_when_enabled():
    off, _ = mod.build_seatbelt_policy(mod.SandboxPolicy(), "/repo")
    on, _ = mod.build_seatbelt_policy(mod.SandboxPolicy(network_access=True), "/repo")
    assert "network-outbound" not in off
    assert "network-outbound" in on


def test_full_access_skips_the_sandbox_entirely():
    argv = mod.build_command("ls", mod.SandboxPolicy(mode=mod.DANGER_FULL_ACCESS), "/repo")
    assert argv[0] != mod.SANDBOX_EXEC


@on_macos
def test_read_only_blocks_writes(tmp_path):
    result = mod.run_sandboxed("echo x > f.txt", mod.SandboxPolicy(mode=mod.READ_ONLY), str(tmp_path))
    assert result.exit_code != 0
    assert mod.is_likely_sandbox_denied(result)
    assert not (tmp_path / "f.txt").exists()


@on_macos
def test_workspace_write_allows_writes_inside_and_blocks_outside(tmp_path):
    # tmp_path lives under $TMPDIR, which is writable by default, so the
    # "outside" half of this test has to turn those carve-outs off.
    policy = mod.SandboxPolicy(exclude_tmpdir=True, exclude_slash_tmp=True)

    inside = mod.run_sandboxed("echo x > f.txt", policy, str(tmp_path))
    assert inside.exit_code == 0
    assert (tmp_path / "f.txt").read_text() == "x\n"

    outside = tmp_path.parent / "escaped.txt"
    blocked = mod.run_sandboxed(f"echo x > {outside}", policy, str(tmp_path))
    assert blocked.exit_code != 0
    assert mod.is_likely_sandbox_denied(blocked)
    assert not outside.exists()


@on_macos
def test_tmpdir_is_writable_by_default(tmp_path):
    target = tmp_path.parent / "tmp-carveout.txt"
    result = mod.run_sandboxed(f"echo x > {target}", mod.SandboxPolicy(), str(tmp_path))
    assert result.exit_code == 0
    target.unlink()


@on_macos
def test_reads_are_allowed_under_every_policy(tmp_path):
    for mode in (mod.READ_ONLY, mod.WORKSPACE_WRITE):
        result = mod.run_sandboxed("head -1 /etc/hosts", mod.SandboxPolicy(mode=mode), str(tmp_path))
        assert result.exit_code == 0


def test_denial_heuristic_ignores_success_and_unsandboxed_runs():
    assert not mod.is_likely_sandbox_denied(mod.ExecOutput(0, "", "operation not permitted", True))
    assert not mod.is_likely_sandbox_denied(mod.ExecOutput(1, "", "operation not permitted", False))
    assert mod.is_likely_sandbox_denied(mod.ExecOutput(1, "", "Operation not permitted", True))
