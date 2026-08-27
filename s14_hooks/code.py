#!/usr/bin/env python3
"""
s14: Hooks

The harness so far is fixed: its policies live in Python and change only when
someone edits this file. Hooks are the extension point that lets a user or an
organization put their own code on the agent's path without forking it.

A hook is a program. Codex writes a JSON payload to its stdin, reads JSON from
its stdout, and acts on the answer:

    {"session_id": "...", "cwd": "/repo", "hook_event_name": "PreToolUse",
     "tool_name": "exec_command", "tool_input": {"cmd": "git push"}}

    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "pushes go through CI"}}

Eleven events, and the useful ones split into two kinds:

    SessionStart, UserPromptSubmit,
    SubagentStart                    -> may return additionalContext
    PreToolUse                       -> may return allow / deny / ask
    PostToolUse, Stop, SessionEnd,
    PreCompact, Interrupt,
    SubagentStop, PermissionRequest  -> observe, or add context

The first kind writes into the conversation. The second decides whether
something happens at all. `deny` is the only hook result that can stop the
agent -- everything else is advisory, which is deliberate: a hook that crashes,
hangs, or prints garbage must not take the session down with it. Every failure
mode here degrades to "run without this hook and say so".

Two rules follow from that, and both are enforced below:

  * **Timeouts are mandatory.** A hook is a subprocess someone else wrote.
  * **Injected context is bounded.** `additionalContextLimit` caps what a hook
    can push into every request; without it, one chatty hook quietly eats the
    context window that s11 is trying to protect.

Run:
  python s14_hooks/code.py --demo        # build a hooks.json and fire every event
  python s14_hooks/code.py --show        # read the real ~/.codex/hooks.json

Real source: codex-rs/hooks/ (engine/dispatcher.rs, engine/command_runner.rs,
engine/output_parser.rs, schema.rs), codex-rs/core/src/hook_runtime.rs
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"
PRE_COMPACT = "PreCompact"
INTERRUPT = "Interrupt"
PERMISSION_REQUEST = "PermissionRequest"
SUBAGENT_START = "SubagentStart"
SUBAGENT_STOP = "SubagentStop"

EVENTS = (
    SESSION_START, SESSION_END, USER_PROMPT_SUBMIT, PRE_TOOL_USE,
    POST_TOOL_USE, STOP, PRE_COMPACT, INTERRUPT, PERMISSION_REQUEST,
    SUBAGENT_START, SUBAGENT_STOP,
)

ALLOW = "allow"
DENY = "deny"
ASK = "ask"

DEFAULT_TIMEOUT = 5.0
DEFAULT_CONTEXT_LIMIT = 2_000


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HookCommand:
    command: str
    timeout: float = DEFAULT_TIMEOUT
    additional_context_limit: int = DEFAULT_CONTEXT_LIMIT
    status_message: str = ""


@dataclass(frozen=True)
class MatcherGroup:
    matcher: str | None
    hooks: tuple[HookCommand, ...]

    def matches(self, subject: str) -> bool:
        """`matcher` is a regex over the tool name (or the session source).

        No matcher means "every subject"; an invalid regex means "no subject",
        because a typo must not silently widen a rule that was meant to narrow one.
        """
        if not self.matcher:
            return True
        try:
            return re.search(self.matcher, subject) is not None
        except re.error:
            return False


@dataclass
class HookConfig:
    groups: dict[str, list[MatcherGroup]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> HookConfig:
        config = cls()
        path = Path(path)
        if not path.is_file():
            return config
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            config.errors.append(f"{path}: {exc}")
            return config

        for event, groups in (raw.get("hooks") or {}).items():
            if event not in EVENTS:
                config.errors.append(f"{path}: unknown event {event!r}")
                continue
            parsed = []
            for group in groups:
                commands = tuple(
                    HookCommand(
                        command=entry["command"],
                        timeout=float(entry.get("timeout", DEFAULT_TIMEOUT)),
                        additional_context_limit=int(
                            entry.get("additionalContextLimit", DEFAULT_CONTEXT_LIMIT)
                        ),
                        status_message=entry.get("statusMessage", ""),
                    )
                    for entry in group.get("hooks", [])
                    if entry.get("type") == "command" and entry.get("command")
                )
                if commands:
                    parsed.append(MatcherGroup(group.get("matcher"), commands))
            if parsed:
                config.groups[event] = parsed
        return config

    def for_event(self, event: str, subject: str = "") -> list[HookCommand]:
        return [
            command
            for group in self.groups.get(event, [])
            if group.matches(subject)
            for command in group.hooks
        ]


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class HookOutcome:
    decision: str = ALLOW
    reason: str = ""
    additional_context: list[str] = field(default_factory=list)
    system_messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.decision == DENY

    def context_text(self) -> str:
        return "\n\n".join(self.additional_context)


def parse_hook_output(stdout: str, limit: int) -> tuple[str | None, str, str, str]:
    """Returns (decision, reason, additional_context, system_message).

    A hook that prints nothing, or prints something that is not the documented
    shape, is treated as "no opinion" -- not as an error and not as a denial.
    """
    stdout = stdout.strip()
    if not stdout:
        return None, "", "", ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "", "", ""
    if not isinstance(payload, dict):
        return None, "", "", ""

    specific = payload.get("hookSpecificOutput") or {}
    decision = specific.get("permissionDecision")
    if decision not in (ALLOW, DENY, ASK, None):
        decision = None
    context = str(specific.get("additionalContext") or "")[:limit]
    return (
        decision,
        str(specific.get("permissionDecisionReason") or ""),
        context,
        str(payload.get("systemMessage") or ""),
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class HookRunner:
    def __init__(self, config: HookConfig, *, cwd: str | None = None, session_id: str | None = None) -> None:
        self.config = config
        self.cwd = cwd or os.getcwd()
        self.session_id = session_id or str(uuid.uuid4())

    def run(self, event: str, *, subject: str = "", **payload: Any) -> HookOutcome:
        outcome = HookOutcome()
        for warning in self.config.errors:
            outcome.warnings.append(warning)

        body = {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "hook_event_name": event,
            **payload,
        }

        for hook in self.config.for_event(event, subject):
            decision, reason, context, system_message = self._run_one(hook, body, outcome)
            if context:
                outcome.additional_context.append(context)
            if system_message:
                outcome.system_messages.append(system_message)
            if decision == DENY:
                # First deny wins and the rest are skipped: the tool is not
                # going to run, so asking the remaining hooks about it is noise.
                outcome.decision = DENY
                outcome.reason = reason or f"blocked by hook: {hook.command}"
                return outcome
            if decision == ASK and outcome.decision == ALLOW:
                outcome.decision = ASK
                outcome.reason = reason
        return outcome

    def _run_one(
        self, hook: HookCommand, body: dict[str, Any], outcome: HookOutcome
    ) -> tuple[str | None, str, str, str]:
        command = os.path.expanduser(hook.command)
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", command],
                input=json.dumps(body),
                capture_output=True,
                text=True,
                timeout=hook.timeout,
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired:
            outcome.warnings.append(f"hook timed out after {hook.timeout}s: {hook.command}")
            return None, "", "", ""
        except OSError as exc:
            outcome.warnings.append(f"hook could not run: {hook.command}: {exc}")
            return None, "", "", ""

        if proc.returncode != 0:
            # Exit code 2 is the documented "block" shortcut for hooks that do
            # not want to emit JSON.
            if proc.returncode == 2:
                return DENY, proc.stderr.strip() or "blocked by hook", "", ""
            outcome.warnings.append(
                f"hook exited {proc.returncode}: {hook.command}: {proc.stderr.strip()[:200]}"
            )
            return None, "", "", ""

        return parse_hook_output(proc.stdout, hook.additional_context_limit)


# --------------------------------------------------------------------------
# Where hooks sit in a turn
# --------------------------------------------------------------------------


def turn_with_hooks(runner: HookRunner, prompt: str, planned_calls: list[dict[str, Any]]) -> list[str]:
    """The same sequence the real loop runs, with the model stubbed out."""
    transcript: list[str] = []

    start = runner.run(SESSION_START, subject="startup", source="startup")
    for text in start.additional_context:
        transcript.append(f"[context injected at session start] {text}")

    submitted = runner.run(USER_PROMPT_SUBMIT, subject=prompt, prompt=prompt)
    if submitted.blocked:
        transcript.append(f"[prompt refused] {submitted.reason}")
        return transcript
    for text in submitted.additional_context:
        transcript.append(f"[context injected with the prompt] {text}")

    for call in planned_calls:
        name, arguments = call["name"], call["arguments"]
        pre = runner.run(PRE_TOOL_USE, subject=name, tool_name=name, tool_input=arguments)
        for text in pre.additional_context:
            transcript.append(f"[context injected before {name}] {text}")
        if pre.blocked:
            # The model is told, in the tool result, that a hook stopped it.
            transcript.append(f"[{name} denied by hook] {pre.reason}")
            continue
        if pre.decision == ASK:
            transcript.append(f"[{name} escalated to the user by hook] {pre.reason}")
            continue

        transcript.append(f"$ {arguments.get('cmd', name)}")
        runner.run(POST_TOOL_USE, subject=name, tool_name=name, tool_output="(ok)")

    stop = runner.run(STOP, subject="")
    for message in stop.system_messages:
        transcript.append(f"[system] {message}")
    runner.run(SESSION_END, subject="")
    return transcript


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------

GUARD_HOOK = '''\
import json, sys
payload = json.load(sys.stdin)
cmd = (payload.get("tool_input") or {}).get("cmd", "")
if "git push" in cmd:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "pushes go through CI, not the agent",
    }}))
elif "rm -rf" in cmd:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": "recursive delete",
    }}))
'''

CONTEXT_HOOK = '''\
import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "House rule: never edit files under vendor/.",
}}))
'''

SLOW_HOOK = "sleep 5"
BROKEN_HOOK = "echo 'not json at all'"


def demo() -> int:
    home = Path(tempfile.mkdtemp(prefix="learn-codex-s14-"))
    (home / "guard.py").write_text(GUARD_HOOK)
    (home / "context.py").write_text(CONTEXT_HOOK)

    config_path = home / "hooks.json"
    config_path.write_text(
        json.dumps(
            {
                "hooks": {
                    SESSION_START: [
                        {
                            "matcher": "startup|resume",
                            "hooks": [
                                {"type": "command", "command": f"{sys.executable} {home}/context.py",
                                 "timeout": 3, "additionalContextLimit": 200},
                                {"type": "command", "command": BROKEN_HOOK, "timeout": 3},
                            ],
                        }
                    ],
                    PRE_TOOL_USE: [
                        {
                            "matcher": "exec_command",
                            "hooks": [
                                {"type": "command", "command": f"{sys.executable} {home}/guard.py",
                                 "timeout": 3},
                                {"type": "command", "command": SLOW_HOOK, "timeout": 0.3},
                            ],
                        },
                        {
                            "matcher": "apply_patch",
                            "hooks": [{"type": "command", "command": "exit 7", "timeout": 3}],
                        },
                    ],
                }
            },
            indent=2,
        )
    )

    print(f"hooks.json: {config_path}\n")
    config = HookConfig.load(config_path)
    runner = HookRunner(config, cwd=str(home))

    calls = [
        {"name": "exec_command", "arguments": {"cmd": "pytest -q"}},
        {"name": "exec_command", "arguments": {"cmd": "git push origin main"}},
        {"name": "exec_command", "arguments": {"cmd": "rm -rf build"}},
        {"name": "apply_patch", "arguments": {"cmd": "*** Begin Patch ..."}},
    ]
    for line in turn_with_hooks(runner, "clean up and ship it", calls):
        print(line)

    print("\nwarnings collected along the way (nothing aborted the session):")
    for event, subject in ((SESSION_START, "startup"), (PRE_TOOL_USE, "exec_command"),
                           (PRE_TOOL_USE, "apply_patch")):
        outcome = runner.run(event, subject=subject, tool_name=subject, tool_input={"cmd": "ls"})
        for warning in outcome.warnings:
            print(f"  {event}: {warning}")
    return 0


def show() -> int:
    path = Path(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))) / "hooks.json"
    config = HookConfig.load(path)
    print(f"{path}: {'found' if config.groups or config.errors else 'not present'}")
    for error in config.errors:
        print(f"  error: {error}")
    for event, groups in sorted(config.groups.items()):
        print(f"\n{event}")
        for group in groups:
            print(f"  matcher: {group.matcher or '(any)'}")
            for hook in group.hooks:
                print(f"    {hook.command}  (timeout {hook.timeout}s, "
                      f"context limit {hook.additional_context_limit})")
    return 0


def main(argv: list[str]) -> int:
    if "--show" in argv:
        return show()
    return demo()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
