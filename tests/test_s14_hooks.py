from __future__ import annotations

import json
import sys

from helpers import load

mod = load("s14_hooks")


def write_config(tmp_path, hooks_by_event) -> object:
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": hooks_by_event}))
    return mod.HookConfig.load(path)


def script(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    return f"{sys.executable} {path}"


DENY = '''import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                         "permissionDecision": "deny",
                                         "permissionDecisionReason": "no"}}))
'''

CONTEXT = '''import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": "%s"}}))
'''

ECHO_PAYLOAD = '''import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"systemMessage": payload["hook_event_name"] + ":" + payload.get("tool_name", "")}))
'''


def test_missing_config_is_not_an_error(tmp_path):
    config = mod.HookConfig.load(tmp_path / "nope.json")
    assert config.groups == {} and config.errors == []


def test_malformed_config_is_reported_not_raised(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("{not json")
    config = mod.HookConfig.load(path)
    assert config.errors and config.groups == {}


def test_matcher_filters_by_tool_name(tmp_path):
    config = write_config(
        tmp_path,
        {mod.PRE_TOOL_USE: [{"matcher": "^exec_command$", "hooks": [{"type": "command", "command": "true"}]}]},
    )
    assert len(config.for_event(mod.PRE_TOOL_USE, "exec_command")) == 1
    assert config.for_event(mod.PRE_TOOL_USE, "apply_patch") == []


def test_invalid_matcher_matches_nothing(tmp_path):
    config = write_config(
        tmp_path,
        {mod.PRE_TOOL_USE: [{"matcher": "[unclosed", "hooks": [{"type": "command", "command": "true"}]}]},
    )
    assert config.for_event(mod.PRE_TOOL_USE, "anything") == []


def test_deny_blocks_the_tool(tmp_path):
    config = write_config(
        tmp_path,
        {mod.PRE_TOOL_USE: [{"hooks": [{"type": "command", "command": script(tmp_path, "d.py", DENY)}]}]},
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(
        mod.PRE_TOOL_USE, subject="exec_command", tool_name="exec_command", tool_input={}
    )
    assert outcome.blocked and outcome.reason == "no"


def test_exit_code_2_is_a_deny_without_json(tmp_path):
    config = write_config(
        tmp_path,
        {mod.PRE_TOOL_USE: [{"hooks": [{"type": "command", "command": "echo nope >&2; exit 2"}]}]},
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(mod.PRE_TOOL_USE, subject="x")
    assert outcome.blocked and "nope" in outcome.reason


def test_a_crashing_hook_is_a_warning_not_a_denial(tmp_path):
    config = write_config(
        tmp_path,
        {mod.PRE_TOOL_USE: [{"hooks": [{"type": "command", "command": "exit 9"}]}]},
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(mod.PRE_TOOL_USE, subject="x")
    assert not outcome.blocked
    assert outcome.warnings and "exited 9" in outcome.warnings[0]


def test_a_hanging_hook_times_out(tmp_path):
    config = write_config(
        tmp_path,
        {mod.PRE_TOOL_USE: [{"hooks": [{"type": "command", "command": "sleep 5", "timeout": 0.3}]}]},
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(mod.PRE_TOOL_USE, subject="x")
    assert not outcome.blocked and "timed out" in outcome.warnings[0]


def test_garbage_output_is_treated_as_no_opinion(tmp_path):
    config = write_config(
        tmp_path,
        {mod.PRE_TOOL_USE: [{"hooks": [{"type": "command", "command": "echo hello world"}]}]},
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(mod.PRE_TOOL_USE, subject="x")
    assert outcome.decision == mod.ALLOW and not outcome.warnings


def test_additional_context_is_capped(tmp_path):
    long_text = "x" * 5000
    config = write_config(
        tmp_path,
        {
            mod.SESSION_START: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": script(tmp_path, "c.py", CONTEXT % long_text),
                            "additionalContextLimit": 50,
                        }
                    ]
                }
            ]
        },
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(mod.SESSION_START, subject="startup")
    assert len(outcome.context_text()) == 50


def test_the_payload_carries_the_event_and_tool(tmp_path):
    config = write_config(
        tmp_path,
        {mod.POST_TOOL_USE: [{"hooks": [{"type": "command", "command": script(tmp_path, "e.py", ECHO_PAYLOAD)}]}]},
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(
        mod.POST_TOOL_USE, subject="apply_patch", tool_name="apply_patch"
    )
    assert outcome.system_messages == ["PostToolUse:apply_patch"]


def test_first_deny_wins_and_stops_the_rest(tmp_path):
    config = write_config(
        tmp_path,
        {
            mod.PRE_TOOL_USE: [
                {
                    "hooks": [
                        {"type": "command", "command": script(tmp_path, "d.py", DENY)},
                        {"type": "command", "command": "sleep 5", "timeout": 0.3},
                    ]
                }
            ]
        },
    )
    outcome = mod.HookRunner(config, cwd=str(tmp_path)).run(mod.PRE_TOOL_USE, subject="x")
    assert outcome.blocked
    assert outcome.warnings == []  # the slow hook was never run
