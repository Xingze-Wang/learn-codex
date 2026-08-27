from __future__ import annotations

import json

from helpers import ScriptedClient, call, load, say

mod = load("s01_agent_loop")


def test_loop_runs_tool_then_answers(tmp_path):
    client = ScriptedClient(mod, [call(mod, "echo hello"), say(mod, "done")])
    session = mod.Session(client, cwd=str(tmp_path))

    answer = session.run_turn("say hello", echo=False)

    assert answer == "done"
    assert len(client.requests) == 2


def test_tool_output_is_appended_as_function_call_output(tmp_path):
    client = ScriptedClient(mod, [call(mod, "echo hi"), say(mod, "ok")])
    session = mod.Session(client, cwd=str(tmp_path))
    session.run_turn("go", echo=False)

    kinds = [item["type"] for item in session.history]
    assert kinds == ["message", "function_call", "function_call_output", "message"]

    output = session.history[2]["output"]
    assert "Process exited with code 0" in output
    assert "hi" in output


def test_second_request_carries_the_whole_history(tmp_path):
    client = ScriptedClient(mod, [call(mod, "true"), say(mod, "ok")])
    session = mod.Session(client, cwd=str(tmp_path))
    session.run_turn("go", echo=False)

    first, second = client.requests
    assert len(first["input_items"]) == 1
    # store=false means nothing is remembered server-side: the second request
    # repeats the user message, the function call, and its output.
    assert [i["type"] for i in second["input_items"]][:3] == [
        "message",
        "function_call",
        "function_call_output",
    ]


def test_exec_command_reports_nonzero_exit(tmp_path):
    out = mod.exec_command("exit 3", str(tmp_path))
    assert "Process exited with code 3" in out


def test_bad_arguments_are_reported_to_the_model(tmp_path):
    bad = [
        mod.OutputItemDone(
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": "{not json",
                "call_id": "c1",
            }
        ),
        mod.Completed(1, 1),
    ]
    client = ScriptedClient(mod, [bad, say(mod, "recovered")])
    session = mod.Session(client, cwd=str(tmp_path))
    session.run_turn("go", echo=False)

    assert "invalid arguments" in session.history[2]["output"]


def test_tool_spec_matches_the_documented_shape():
    spec = mod.EXEC_COMMAND_TOOL
    assert spec["name"] == "exec_command"
    assert spec["parameters"]["required"] == ["cmd"]
    assert json.dumps(spec)  # serializable as-is into the request body
