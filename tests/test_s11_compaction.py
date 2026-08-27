from __future__ import annotations

from helpers import ScriptedClient, call, load, say

mod = load("s11_compaction")


def build_history(tool_outputs: int = 5) -> list[dict]:
    history = [
        mod.user_item("<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>"),
        mod.user_item("port the auth module"),
    ]
    for i in range(tool_outputs):
        history.append({"type": "reasoning", "encrypted_content": "g" * 200})
        history.append({"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": f"c{i}"})
        history.append({"type": "function_call_output", "call_id": f"c{i}", "output": "x" * 4000})
    history.append(mod.user_item("also keep the old endpoint"))
    return history


def test_threshold_is_a_fraction_of_the_window():
    assert mod.TokenStatus(79, 100).needs_compaction(0.8) is False
    assert mod.TokenStatus(80, 100).needs_compaction(0.8) is True


def test_compaction_keeps_prefix_user_turns_and_summary():
    history = build_history()
    rebuilt = mod.build_compacted_history(
        mod.session_prefix(history), mod.collect_user_messages(history), "the summary"
    )
    texts = [mod._text_of(item) for item in rebuilt]
    assert texts[0].startswith("<environment_context>")
    assert "port the auth module" in texts
    assert "also keep the old endpoint" in texts
    assert texts[-1].startswith(mod.SUMMARY_PREFIX)
    assert "the summary" in texts[-1]


def test_tool_outputs_and_reasoning_are_dropped():
    history = build_history()
    rebuilt = mod.build_compacted_history(
        mod.session_prefix(history), mod.collect_user_messages(history), "s"
    )
    assert all(item["type"] == "message" for item in rebuilt)
    assert mod.history_tokens(rebuilt) < mod.history_tokens(history) / 10


def test_injected_blocks_are_not_mistaken_for_user_messages():
    history = build_history()
    assert mod.collect_user_messages(history) == ["port the auth module", "also keep the old endpoint"]


def test_previous_summaries_are_not_re_collected_as_user_turns():
    history = [mod.user_item(f"{mod.SUMMARY_PREFIX}\nearlier work"), mod.user_item("next thing")]
    assert mod.collect_user_messages(history) == ["next thing"]


def test_newest_user_messages_win_the_budget():
    messages = ["a" * 4000, "b" * 4000, "the current request"]
    rebuilt = mod.build_compacted_history([], messages, "s", max_tokens=20)
    kept = [mod._text_of(item) for item in rebuilt]

    # Newest first until the budget runs out: the current request survives
    # whole, the one before it is truncated to whatever was left, and the
    # oldest is gone entirely.
    assert kept[-2] == "the current request"
    assert kept[0].startswith("b") and len(kept[0]) < 4000
    assert not any(text.startswith("a") for text in kept)
    assert kept[-1].startswith(mod.SUMMARY_PREFIX)


def test_empty_summary_is_marked_not_silently_empty():
    rebuilt = mod.build_compacted_history([], ["x"], "")
    assert "(no summary available)" in mod._text_of(rebuilt[-1])


def test_summary_request_carries_no_tools(tmp_path):
    client = ScriptedClient(mod, [say(mod, "a summary")])
    assert mod.request_summary(client, [mod.user_item("hi")]) == "a summary"
    assert client.requests[0]["tools"] == []
    assert mod.SUMMARIZATION_PROMPT in client.requests[0]["input_items"][-1]["content"][0]["text"]


def test_session_compacts_before_the_request_that_would_overflow(tmp_path):
    client = ScriptedClient(
        mod,
        [
            call(mod, "python3 -c \"print('x' * 40000)\""),
            say(mod, "a summary of the work so far"),  # the compaction call
            say(mod, "done"),
        ],
    )
    session = mod.Session(client, cwd=str(tmp_path), context_window=4_000)
    answer = session.run_turn("go", echo=False)

    assert answer == "done"
    assert len(session.compactions) == 1
    record = session.compactions[0]
    assert record.after_tokens < record.before_tokens
    assert mod.is_summary_item(session.history[-3]) or any(
        mod.is_summary_item(item) for item in session.history
    )
