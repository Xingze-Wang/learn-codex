from __future__ import annotations

from helpers import ScriptedClient, call, load, say

mod = load("s03_turn_context")


def ctx(**kw):
    return mod.TurnContext(cwd=kw.pop("cwd", "/repo"), **kw)


def test_environment_is_injected_once_then_only_on_change(tmp_path):
    client = ScriptedClient(mod, [say(mod, "a"), say(mod, "b"), say(mod, "c")])
    session = mod.Session(client, ctx(cwd=str(tmp_path)))

    session.run_turn("one", echo=False)
    session.run_turn("two", echo=False)
    injected_after_two = _injected(session)

    session.run_turn("three", echo=False, cwd="/tmp")
    injected_after_three = _injected(session)

    # turn 1 injects environment + permissions; turn 2 changes nothing;
    # turn 3 moves cwd, so exactly one more environment block appears.
    assert injected_after_two == 2
    assert injected_after_three == 3


def _injected(session) -> int:
    return sum(
        1
        for item in session.history
        if item.get("type") == "message"
        and item.get("role") == "user"
        and item["content"][0]["text"].startswith(("<environment_context>", "<permissions>"))
    )


def test_turn_context_is_frozen():
    context = ctx()
    moved = context.with_overrides(cwd="/other")
    assert context.cwd == "/repo"
    assert moved.cwd == "/other"
    assert moved.approval_policy == context.approval_policy


def test_overrides_ignore_none():
    context = ctx(sandbox_mode="read-only")
    assert context.with_overrides(sandbox_mode=None).sandbox_mode == "read-only"


def test_tool_runs_in_the_turn_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("here")
    client = ScriptedClient(mod, [call(mod, "ls"), say(mod, "done")])
    session = mod.Session(client, ctx(cwd=str(tmp_path)))
    session.run_turn("list", echo=False)

    output = [i for i in session.history if i.get("type") == "function_call_output"][0]
    assert "marker.txt" in output["output"]


def test_permissions_text_follows_the_policy():
    read_only = mod.render_permissions(ctx(sandbox_mode="read-only", approval_policy="never"))
    assert "read-only" in read_only
    assert "cannot be asked for approval" in read_only
