from __future__ import annotations

import json

from helpers import ScriptedClient, call, load, say

mod = load("s04_tool_registry")


def registry(**kw):
    return mod.build_registry(mod.ToolsConfig(**kw))


def test_model_shell_type_picks_the_shell_tool():
    names = [s.get("name") for s in registry(shell_type="shell").specs()]
    assert "shell" in names and "exec_command" not in names

    names = [s.get("name") for s in registry().specs()]
    assert "exec_command" in names and "shell" not in names


def test_disabled_features_are_absent():
    names = [s.get("name") for s in registry(apply_patch=False, plan_tool=False).specs()]
    assert names == ["exec_command"]


def test_apply_patch_is_a_freeform_grammar_tool():
    spec = next(s for s in registry().specs() if s.get("name") == "apply_patch")
    assert spec["type"] == "custom"
    assert spec["format"]["syntax"] == "lark"
    assert "*** Begin Patch" in spec["format"]["definition"]
    assert json.dumps(spec)


def test_custom_tool_call_payload_is_raw_text():
    item = {
        "type": "custom_tool_call",
        "name": "apply_patch",
        "call_id": "c1",
        "input": "*** Begin Patch\n*** Delete File: x.py\n*** End Patch",
    }
    tool_call = mod.build_tool_call(item)
    assert isinstance(tool_call.payload, str)
    assert mod.dispatch(registry(), tool_call, mod.ToolContext(cwd=".")).startswith("would patch: x.py")


def test_unknown_tool_is_reported_to_the_model():
    tool_call = mod.ToolCall("does_not_exist", "c1", {})
    assert "unsupported tool" in mod.dispatch(registry(), tool_call, mod.ToolContext(cwd="."))


def test_handler_errors_become_text_not_exceptions():
    tool_call = mod.ToolCall("update_plan", "c1", {"plan": []})
    out = mod.dispatch(registry(), tool_call, mod.ToolContext(cwd="."))
    assert out.startswith("error: ")


def test_plan_tool_rejects_two_in_progress_steps():
    ctx = mod.ToolContext(cwd=".")
    plan = [
        {"step": "a", "status": "in_progress"},
        {"step": "b", "status": "in_progress"},
    ]
    out = mod.dispatch(registry(), mod.ToolCall("update_plan", "c1", {"plan": plan}), ctx)
    assert "at most one step" in out
    assert ctx.plan == []


def test_output_is_truncated_head_and_tail():
    text = "\n".join(f"line {i}" for i in range(50_000))
    out = mod.truncate_output(text, max_tokens=100)
    assert out.startswith("line 0")
    assert out.rstrip().endswith("line 49999")
    assert "characters truncated" in out
    assert len(out) < len(text)


def test_parallel_support_is_per_tool():
    reg = registry()
    assert reg.supports_parallel("update_plan") is True
    assert reg.supports_parallel("exec_command") is False
    assert reg.supports_parallel("apply_patch") is False


def test_loop_survives_invalid_json_arguments(tmp_path):
    broken = [
        mod.OutputItemDone(
            {"type": "function_call", "name": "exec_command", "arguments": "{oops", "call_id": "c1"}
        ),
        mod.Completed(1, 1),
    ]
    client = ScriptedClient(mod, [broken, say(mod, "recovered")])
    session = mod.Session(client, registry(), cwd=str(tmp_path))
    assert session.run_turn("go", echo=False) == "recovered"
    assert "invalid JSON arguments" in session.history[2]["output"]
