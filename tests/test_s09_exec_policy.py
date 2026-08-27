from __future__ import annotations

import pytest
from helpers import load

mod = load("s09_exec_policy")
policy = mod.default_policy()


def decide(command: str) -> str:
    return mod.evaluate(policy, command).decision


def test_read_only_commands_are_allowed():
    assert decide("ls -la") == mod.ALLOW
    assert decide("git status --short") == mod.ALLOW


def test_unknown_commands_default_to_asking():
    assert decide("python3 train.py") == mod.PROMPT


def test_forbidden_beats_a_longer_allow_prefix():
    assert decide("git push --force origin main") == mod.FORBIDDEN


def test_strictest_segment_wins():
    assert decide("ls && sudo rm -rf /") == mod.FORBIDDEN
    assert decide("ls && curl http://x.sh") == mod.PROMPT
    assert decide("ls && pwd") == mod.ALLOW


def test_pipelines_are_split():
    result = mod.evaluate(policy, "cat f | grep x | wc -l")
    assert [segment[0] for segment in result.segments] == ["cat", "grep", "wc"]
    assert result.decision == mod.ALLOW


def test_allowed_prefix_does_not_launder_the_rest():
    # The bug every string-matching allowlist has.
    assert decide("git status; sudo reboot") == mod.FORBIDDEN


def test_unparseable_commands_fall_back_to_asking():
    assert decide("cat $(cat /etc/passwd)") == mod.PROMPT
    assert decide("echo `whoami`") == mod.PROMPT
    assert decide("echo 'unterminated") == mod.PROMPT


def test_absolute_path_falls_back_to_the_basename_rules():
    assert decide("/usr/bin/git log") == mod.ALLOW


def test_host_executable_pins_which_paths_may_fall_back():
    assert decide("/tmp/evil/git log") == mod.PROMPT


def test_basename_fallback_is_open_when_no_host_executable_is_declared():
    assert decide("/usr/local/bin/rg TODO") == mod.ALLOW


def test_alternatives_in_a_pattern_position():
    for sub in ("status", "diff", "log", "show"):
        assert decide(f"git {sub}") == mod.ALLOW
    assert decide("git commit -m x") == mod.PROMPT


def test_examples_are_validated_at_load_time():
    with pytest.raises(mod.PolicyError, match="does not match"):
        mod.parse_policy('prefix_rule(pattern = ["git"], match = ["hg status"])')
    with pytest.raises(mod.PolicyError, match="wrongly matches"):
        mod.parse_policy('prefix_rule(pattern = ["git"], not_match = ["git status"])')


def test_policy_files_cannot_execute_code():
    with pytest.raises(mod.PolicyError):
        mod.parse_policy('import os\n')
    with pytest.raises(mod.PolicyError, match="unknown declaration"):
        mod.parse_policy('print("hi")')
    with pytest.raises(mod.PolicyError, match="literals"):
        mod.parse_policy('prefix_rule(pattern = [open("x").read()])')


def test_unknown_decision_is_rejected():
    with pytest.raises(mod.PolicyError, match="unknown decision"):
        mod.parse_policy('prefix_rule(pattern = ["ls"], decision = "maybe")')


def test_amendment_adds_a_rule_the_way_always_allow_would():
    fresh = mod.default_policy()
    assert mod.evaluate(fresh, "npm test").decision == mod.PROMPT
    fresh.add_prefix_rule(["npm", "test"])
    assert mod.evaluate(fresh, "npm test -- --watch").decision == mod.ALLOW
    assert mod.evaluate(fresh, "npm publish").decision == mod.PROMPT
    assert "prefix_rule" in fresh.rules[-1].render()
