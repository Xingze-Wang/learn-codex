from __future__ import annotations

import json

from helpers import load

mod = load("s10_rollout")


def record_two_turns(home):
    recorder = mod.RolloutRecorder.create(home, cwd="/repo", model="gpt-5.5")
    for turn in (1, 2):
        recorder.record_turn_context(turn=turn, cwd="/repo")
        recorder.record_event("task_started", turn_id=f"t{turn}")
        recorder.record_event("user_message", message=f"question {turn}")
        recorder.record_response_item(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"q{turn}"}]}
        )
        recorder.record_response_item(
            {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": f"c{turn}"}
        )
        recorder.record_response_item({"type": "function_call_output", "call_id": f"c{turn}", "output": "ok"})
        recorder.record_event("task_complete", turn_id=f"t{turn}")
    return recorder


def test_file_name_and_layout_match_codex(tmp_path):
    recorder = mod.RolloutRecorder.create(tmp_path, cwd="/repo", model="m", thread_id="abc-123")
    parts = recorder.path.relative_to(tmp_path).parts
    assert parts[0] == "sessions"
    assert len(parts) == 5  # sessions/YYYY/MM/DD/rollout-....jsonl
    assert parts[-1].startswith("rollout-") and parts[-1].endswith("-abc-123.jsonl")


def test_first_line_is_session_meta(tmp_path):
    recorder = mod.RolloutRecorder.create(tmp_path, cwd="/repo", model="gpt-5.5")
    first = json.loads(recorder.path.read_text().splitlines()[0])
    assert first["type"] == "session_meta"
    assert first["payload"]["cwd"] == "/repo"


def test_deltas_are_not_persisted(tmp_path):
    recorder = mod.RolloutRecorder.create(tmp_path, cwd="/repo", model="m")
    recorder.record_event("agent_message_delta", delta="a")
    recorder.record_response_item({"type": "additional_tools", "tools": []})
    assert len(recorder.path.read_text().splitlines()) == 1  # meta only


def test_resume_returns_only_replayable_items(tmp_path):
    recorder = record_two_turns(tmp_path)
    history, meta = mod.resume(recorder.path)
    assert meta["cwd"] == "/repo"
    assert [item["type"] for item in history] == [
        "message", "function_call", "function_call_output",
        "message", "function_call", "function_call_output",
    ]


def test_fork_keeps_a_prefix_and_leaves_the_original_alone(tmp_path):
    recorder = record_two_turns(tmp_path)
    forked = mod.fork(recorder.path, tmp_path, keep_turns=1)

    assert mod.read_rollout(forked).turn_count() == 1
    assert mod.read_rollout(recorder.path).turn_count() == 2
    assert mod.read_rollout(forked).thread_id != mod.read_rollout(recorder.path).thread_id
    assert len(mod.resume(forked)[0]) == 3


def test_truncated_last_line_does_not_lose_the_session(tmp_path):
    recorder = record_two_turns(tmp_path)
    with recorder.path.open("a") as handle:
        handle.write('{"timestamp": "2026-01-01T00:00:00Z", "type": "response_i')

    rollout = mod.read_rollout(recorder.path)
    assert rollout.turn_count() == 2
    assert len(rollout.response_items()) == 6


def test_head_summary_reads_only_the_head(tmp_path):
    recorder = record_two_turns(tmp_path)
    summary = mod.head_summary(recorder.path, max_lines=10)
    assert summary["cwd"] == "/repo"
    assert summary["preview"] == "question 1"


def test_listing_is_newest_first(tmp_path):
    import datetime as dt

    old = mod.RolloutRecorder.create(
        tmp_path, cwd="/a", model="m", timestamp=dt.datetime(2025, 1, 1, 10, 0, 0)
    )
    new = mod.RolloutRecorder.create(
        tmp_path, cwd="/b", model="m", timestamp=dt.datetime(2026, 6, 1, 10, 0, 0)
    )
    listed = mod.list_rollouts(tmp_path)
    assert listed[0] == new.path and listed[-1] == old.path


def test_reads_a_real_codex_rollout_shape(tmp_path):
    # The exact line shapes codex writes today.
    path = tmp_path / "rollout-real.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(line)
            for line in [
                {"timestamp": "t", "type": "session_meta", "payload": {"id": "x", "cwd": "/w", "cli_version": "0.128.0"}},
                {"timestamp": "t", "type": "turn_context", "payload": {"cwd": "/w", "approval_policy": "on-request"}},
                {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "a"}},
                {"timestamp": "t", "type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "gAAA"}},
                {"timestamp": "t", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c"}},
                {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count", "info": None}},
            ]
        )
        + "\n"
    )
    rollout = mod.read_rollout(path)
    assert rollout.thread_id == "x"
    assert rollout.turn_count() == 1
    assert [i["type"] for i in rollout.response_items()] == ["reasoning", "function_call"]
