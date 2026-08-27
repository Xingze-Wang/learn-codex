from __future__ import annotations

import asyncio

import pytest
from helpers import ScriptedClient, call, load, say

mod = load("s08_approval")


async def drain_until(thread, kinds, timeout=5):
    seen = []
    while True:
        event = await asyncio.wait_for(thread.next_event(), timeout=timeout)
        seen.append(event.msg)
        if isinstance(event.msg, kinds):
            return seen


# --- assessment ------------------------------------------------------------


def test_untrusted_policy_asks_for_anything_not_on_the_list():
    check = mod.assess_command_safety(
        "rm -rf build", approval_policy=mod.UNLESS_TRUSTED, sandbox_available=True, approved=set()
    )
    assert isinstance(check, mod.AskUser)


def test_untrusted_policy_still_auto_approves_read_only_commands():
    check = mod.assess_command_safety(
        "git status", approval_policy=mod.UNLESS_TRUSTED, sandbox_available=True, approved=set()
    )
    assert isinstance(check, mod.AutoApprove)


def test_on_request_runs_first_and_asks_later():
    check = mod.assess_command_safety(
        "rm -rf build", approval_policy=mod.ON_REQUEST, sandbox_available=True, approved=set()
    )
    assert check == mod.AutoApprove(sandboxed=True)


def test_no_sandbox_and_never_means_refuse():
    check = mod.assess_command_safety(
        "ls", approval_policy=mod.NEVER, sandbox_available=False, approved=set()
    )
    assert isinstance(check, mod.Reject)


def test_no_sandbox_and_on_request_means_ask():
    check = mod.assess_command_safety(
        "ls", approval_policy=mod.ON_REQUEST, sandbox_available=False, approved=set()
    )
    assert isinstance(check, mod.AskUser)


def test_session_approval_skips_the_sandbox_next_time():
    check = mod.assess_command_safety(
        "make install", approval_policy=mod.UNLESS_TRUSTED,
        sandbox_available=True, approved={"make install"},
    )
    assert check == mod.AutoApprove(sandboxed=False)


# --- the escalation round trip --------------------------------------------


def denied_run(monkeypatch, sequence):
    """Replace run_command with a scripted sequence of results."""
    calls = []

    def fake(cmd, cwd, *, sandboxed):
        calls.append((cmd, sandboxed))
        return sequence[len(calls) - 1]

    monkeypatch.setattr(mod, "run_command", fake)
    monkeypatch.setattr(mod, "sandbox_available", lambda: True)
    return calls


@pytest.mark.asyncio
async def test_denied_command_asks_then_retries_unsandboxed(monkeypatch, tmp_path):
    runs = denied_run(
        monkeypatch,
        [
            mod.ExecOutput(1, "bash: Operation not permitted", True),
            mod.ExecOutput(0, "written", False),
        ],
    )
    client = ScriptedClient(mod, [call(mod, "echo x > ~/notes"), say(mod, "done")])
    thread = mod.CodexThread(client, cwd=str(tmp_path))
    thread.start()

    await thread.submit(mod.UserTurn("write to my home dir"))
    seen = await drain_until(thread, mod.ExecApprovalRequest)
    request = seen[-1]
    assert request.reason == "the sandbox blocked this command"

    await thread.submit(mod.ExecApproval(request.call_id, mod.APPROVED))
    await drain_until(thread, mod.TaskComplete)

    # ran twice: once sandboxed, once not
    assert [sandboxed for _, sandboxed in runs] == [True, False]
    output = [i for i in thread.session.history if i.get("type") == "function_call_output"][0]
    assert "written" in output["output"]


@pytest.mark.asyncio
async def test_denial_tells_the_model_when_the_user_says_no(monkeypatch, tmp_path):
    denied_run(monkeypatch, [mod.ExecOutput(1, "Operation not permitted", True)])
    client = ScriptedClient(mod, [call(mod, "rm -rf /"), say(mod, "understood")])
    thread = mod.CodexThread(client, cwd=str(tmp_path))
    thread.start()

    await thread.submit(mod.UserTurn("go"))
    request = (await drain_until(thread, mod.ExecApprovalRequest))[-1]
    await thread.submit(mod.ExecApproval(request.call_id, mod.DENIED))
    await drain_until(thread, mod.TaskComplete)

    output = [i for i in thread.session.history if i.get("type") == "function_call_output"][0]
    assert "the user declined" in output["output"]


@pytest.mark.asyncio
async def test_approved_for_session_is_not_asked_twice(monkeypatch, tmp_path):
    runs = denied_run(
        monkeypatch,
        [
            mod.ExecOutput(1, "Operation not permitted", True),
            mod.ExecOutput(0, "ok", False),
            mod.ExecOutput(0, "ok", False),
        ],
    )
    client = ScriptedClient(
        mod,
        [
            call(mod, "make install", call_id="c1"),
            call(mod, "make install", call_id="c2"),
            say(mod, "done"),
        ],
    )
    thread = mod.CodexThread(client, cwd=str(tmp_path))
    thread.start()

    await thread.submit(mod.UserTurn("install it twice"))
    request = (await drain_until(thread, mod.ExecApprovalRequest))[-1]
    await thread.submit(mod.ExecApproval(request.call_id, mod.APPROVED_FOR_SESSION))
    seen = await drain_until(thread, mod.TaskComplete)

    assert sum(1 for m in seen if isinstance(m, mod.ExecApprovalRequest)) == 0
    assert "make install" in thread.session.approved
    assert [sandboxed for _, sandboxed in runs] == [True, False, False]


@pytest.mark.asyncio
async def test_never_policy_reports_the_failure_instead_of_asking(monkeypatch, tmp_path):
    denied_run(monkeypatch, [mod.ExecOutput(1, "Operation not permitted", True)])
    client = ScriptedClient(mod, [call(mod, "echo x > /etc/hosts"), say(mod, "cannot")])
    thread = mod.CodexThread(client, cwd=str(tmp_path), approval_policy=mod.NEVER)
    thread.start()

    await thread.submit(mod.UserTurn("go"))
    seen = await drain_until(thread, mod.TaskComplete)

    assert not any(isinstance(m, mod.ExecApprovalRequest) for m in seen)
    output = [i for i in thread.session.history if i.get("type") == "function_call_output"][0]
    assert "approvals are disabled" in output["output"]


@pytest.mark.asyncio
async def test_stray_approval_is_reported_not_crashed(tmp_path):
    thread = mod.CodexThread(ScriptedClient(mod, []), cwd=str(tmp_path))
    thread.start()
    await thread.submit(mod.ExecApproval("nope", mod.APPROVED))
    seen = await drain_until(thread, mod.ErrorEvent)
    assert "no pending approval" in seen[-1].message
