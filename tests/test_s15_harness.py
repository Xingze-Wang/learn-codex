from __future__ import annotations

import asyncio
import json

import pytest
from helpers import ScriptedClient, call, load, say

mod = load("s15_harness")


def config(tmp_path, **kw):
    return mod.Config(
        cwd=str(tmp_path),
        codex_home=str(tmp_path / "codex-home"),
        **kw,
    )


async def drain_until(thread, kinds, timeout=10):
    seen = []
    while True:
        event = await asyncio.wait_for(thread.next_event(), timeout=timeout)
        seen.append(event.msg)
        if isinstance(event.msg, kinds):
            return seen


def patch_turn(mod_, patch_text, call_id="p1"):
    return [
        mod_.OutputItemDone(
            {"type": "custom_tool_call", "name": "apply_patch", "call_id": call_id, "input": patch_text}
        ),
        mod_.Completed(5, 5),
    ]


@pytest.mark.asyncio
async def test_session_configured_reports_the_wiring(tmp_path):
    thread = mod.CodexThread(ScriptedClient(mod, []), config(tmp_path))
    thread.start()
    event = await asyncio.wait_for(thread.next_event(), timeout=5)
    assert isinstance(event.msg, mod.SessionConfigured)
    assert set(event.msg.tools) >= {"exec_command", "apply_patch", "update_plan"}
    assert event.msg.rollout_path.endswith(".jsonl")


@pytest.mark.asyncio
async def test_a_full_turn_runs_a_command_and_records_it(tmp_path):
    client = ScriptedClient(mod, [call(mod, "echo hello"), say(mod, "done")])
    thread = mod.CodexThread(client, config(tmp_path))
    thread.start()
    await thread.submit(mod.UserTurn("say hello"))
    seen = await drain_until(thread, mod.TaskComplete)

    kinds = [type(m).__name__ for m in seen]
    assert "ExecCommandBegin" in kinds and "ExecCommandEnd" in kinds
    assert seen[-1].last_agent_message == "done"

    recorded = mod.rollout.read_rollout(thread.session.recorder.path)
    assert recorded.turn_count() == 1
    assert any(item["type"] == "function_call" for item in recorded.response_items())


@pytest.mark.asyncio
async def test_apply_patch_edits_the_workspace_and_reports_a_diff(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    patch = "*** Begin Patch\n*** Update File: app.py\n@@\n-value = 1\n+value = 2\n*** End Patch\n"
    client = ScriptedClient(mod, [patch_turn(mod, patch), say(mod, "patched")])
    thread = mod.CodexThread(client, config(tmp_path))
    thread.start()
    await thread.submit(mod.UserTurn("bump the value"))
    seen = await drain_until(thread, mod.TaskComplete)

    assert (tmp_path / "app.py").read_text() == "value = 2\n"
    applied = [m for m in seen if isinstance(m, mod.PatchApplyEnd)]
    assert applied and applied[0].success and applied[0].files == ["app.py"]
    diffs = [m for m in seen if isinstance(m, mod.TurnDiff)]
    assert diffs and "+value = 2" in diffs[0].unified_diff


@pytest.mark.asyncio
async def test_a_failing_patch_is_reported_to_the_model(tmp_path):
    (tmp_path / "app.py").write_text("actual\n")
    patch = "*** Begin Patch\n*** Update File: app.py\n@@\n-imagined\n+new\n*** End Patch\n"
    client = ScriptedClient(mod, [patch_turn(mod, patch), say(mod, "recovering")])
    thread = mod.CodexThread(client, config(tmp_path))
    thread.start()
    await thread.submit(mod.UserTurn("go"))
    await drain_until(thread, mod.TaskComplete)

    outputs = [i for i in thread.session.history if i.get("type") == "custom_tool_call_output"]
    assert outputs and outputs[0]["output"].startswith("patch failed:")
    assert (tmp_path / "app.py").read_text() == "actual\n"


@pytest.mark.asyncio
async def test_forbidden_commands_never_reach_the_shell(tmp_path):
    client = ScriptedClient(mod, [call(mod, "sudo rm -rf /"), say(mod, "understood")])
    thread = mod.CodexThread(client, config(tmp_path))
    thread.start()
    await thread.submit(mod.UserTurn("clean up"))
    seen = await drain_until(thread, mod.TaskComplete)

    assert not any(isinstance(m, mod.ExecCommandBegin) for m in seen)
    output = [i for i in thread.session.history if i.get("type") == "function_call_output"][0]
    assert "forbidden by policy" in output["output"]


@pytest.mark.asyncio
async def test_update_plan_emits_a_plan_event(tmp_path):
    plan = {"plan": [{"step": "read the code", "status": "in_progress"}]}
    turn = [
        mod.OutputItemDone(
            {"type": "function_call", "name": "update_plan", "arguments": json.dumps(plan), "call_id": "u1"}
        ),
        mod.Completed(1, 1),
    ]
    client = ScriptedClient(mod, [turn, say(mod, "ok")])
    thread = mod.CodexThread(client, config(tmp_path))
    thread.start()
    await thread.submit(mod.UserTurn("plan it"))
    seen = await drain_until(thread, mod.TaskComplete)

    updates = [m for m in seen if isinstance(m, mod.PlanUpdate)]
    assert updates and updates[0].plan[0]["step"] == "read the code"


@pytest.mark.asyncio
async def test_mid_turn_message_steers_the_running_turn(tmp_path):
    gate = asyncio.Event()
    loop = asyncio.get_running_loop()

    class SlowClient(ScriptedClient):
        def stream(self, **kwargs):
            if not self.requests:
                loop.call_soon_threadsafe(gate.set)
            return super().stream(**kwargs)

    client = SlowClient(mod, [call(mod, "sleep 0.2"), say(mod, "ok")])
    thread = mod.CodexThread(client, config(tmp_path))
    thread.start()
    await thread.submit(mod.UserTurn("first"))
    await asyncio.wait_for(gate.wait(), timeout=5)
    await thread.submit(mod.UserTurn("second"))
    seen = await drain_until(thread, mod.TaskComplete)

    assert sum(1 for m in seen if isinstance(m, mod.TaskStarted)) == 1
    texts = [
        i["content"][0]["text"]
        for i in thread.session.history
        if i.get("type") == "message" and i.get("role") == "user"
    ]
    assert texts[-2:] == ["first", "second"]


@pytest.mark.asyncio
async def test_json_frontend_emits_one_object_per_event(tmp_path, capsys):
    client = ScriptedClient(mod, [say(mod, "hi")])
    thread = mod.CodexThread(client, config(tmp_path))
    thread.start()
    done = asyncio.Event()
    renderer = asyncio.create_task(mod.render_json(thread, done))
    await thread.submit(mod.UserTurn("hello"))
    await asyncio.wait_for(done.wait(), timeout=5)
    renderer.cancel()

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["msg"]["type"] == "session_configured"
    assert parsed[-1]["msg"]["type"] == "task_complete"
    assert all("id" in item and "msg" in item for item in parsed)


@pytest.mark.asyncio
async def test_shutdown_closes_cleanly(tmp_path):
    thread = mod.CodexThread(ScriptedClient(mod, []), config(tmp_path))
    thread.start()
    await thread.submit(mod.Shutdown())
    seen = await drain_until(thread, mod.ShutdownComplete)
    assert isinstance(seen[-1], mod.ShutdownComplete)
