#!/usr/bin/env python3
"""
s03: TurnContext and world state

A Codex session lives longer than one turn, but almost nothing a tool needs is
session-wide. The cwd, the model, the approval policy, the sandbox policy --
each is decided when a turn starts and must not change underneath a running
tool. Codex freezes them into a `TurnContext` and hands that to every tool.

The second half is what the *model* is told about that context. Codex keeps a
"world state": a set of rendered sections (environment, permissions, ...) that
are re-injected into the conversation as ordinary user messages -- but only
when their rendering changes. A turn in the same directory with the same
policy costs zero extra tokens; a turn that moves to a new directory injects
one small `<environment_context>` block.

    turn 1  cwd=/repo  read-only   -> inject <environment_context> + <permissions>
    turn 2  cwd=/repo  read-only   -> inject nothing
    turn 3  cwd=/other read-only   -> inject <environment_context> only

Run:
  python s03_turn_context/code.py "what is in this directory?"
  python s03_turn_context/code.py --cwd /tmp "and here?"
  python s03_turn_context/code.py --render     # print the sections, no API call

Real source: codex-rs/core/src/session/turn_context.rs,
codex-rs/core/src/context/world_state/environment.rs (render + change detection),
codex-rs/core/src/context/world_state/permissions.rs

Builds on the s01 kernel.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
MAX_OUTPUT_BYTES = 32 * 1024

BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in a terminal harness.
Your one tool is `exec_command`. Respect the environment and permission
blocks in the conversation: they describe where you are and what you may do.
Finish with a short plain-text summary.
"""

EXEC_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
            "workdir": {"type": "string", "description": "Working directory."},
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
}

# --------------------------------------------------------------------------
# Kernel (s01) -- streaming Responses client, unchanged from chapter to chapter
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputTextDelta:
    delta: str


@dataclass(frozen=True)
class OutputItemDone:
    item: dict[str, Any]


@dataclass(frozen=True)
class Completed:
    input_tokens: int
    output_tokens: int


ResponseEvent = OutputTextDelta | OutputItemDone | Completed


class ModelClient(Protocol):
    def stream(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Iterator[ResponseEvent]: ...


class ResponsesClient:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        from openai import OpenAI

        self._client = OpenAI()
        self.model = model

    def stream(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Iterator[ResponseEvent]:
        with self._client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_items,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            store=False,
            stream=True,
            include=["reasoning.encrypted_content"],
        ) as stream:
            for event in stream:
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    yield OutputTextDelta(event.delta)
                elif kind == "response.output_item.done":
                    raw = event.item.model_dump(exclude_none=True)
                    raw.pop("id", None)
                    raw.pop("status", None)
                    yield OutputItemDone(raw)
                elif kind == "response.completed":
                    usage = getattr(event.response, "usage", None)
                    yield Completed(
                        getattr(usage, "input_tokens", 0) or 0,
                        getattr(usage, "output_tokens", 0) or 0,
                    )


def _message_text(item: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for part in item.get("content", [])
        if part.get("type") in ("output_text", "text")
    )


def user_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


# --------------------------------------------------------------------------
# TurnContext: everything a tool is allowed to read
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnContext:
    """Frozen for the duration of one turn. Tools get this, never globals."""

    cwd: str
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    approval_policy: str = "on-request"  # untrusted | on-request | never
    sandbox_mode: str = "workspace-write"  # read-only | workspace-write | danger-full-access
    network_access: bool = False
    shell: str = field(default_factory=lambda: os.path.basename(os.environ.get("SHELL", "bash")))

    def with_overrides(self, **kwargs: Any) -> TurnContext:
        return replace(self, **{k: v for k, v in kwargs.items() if v is not None})


# --------------------------------------------------------------------------
# World state: sections that are re-rendered, diffed, and injected on change
# --------------------------------------------------------------------------


def render_environment(ctx: TurnContext, *, today: str | None = None) -> str:
    date = today or dt.date.today().isoformat()
    return (
        "<environment_context>\n"
        f"  <cwd>{ctx.cwd}</cwd>\n"
        f"  <shell>{ctx.shell}</shell>\n"
        f"  <current_date>{date}</current_date>\n"
        f'  <network enabled="{str(ctx.network_access).lower()}" />\n'
        "</environment_context>"
    )


APPROVAL_TEXT = {
    "untrusted": (
        "Commands require approval unless an exec policy rule already allows them."
    ),
    "on-request": (
        "You may request escalated permissions when a command needs to run outside "
        "the sandbox. Explain why in the justification."
    ),
    "never": (
        "The user cannot be asked for approval. A command that fails under the "
        "sandbox simply fails; report it and work around it."
    ),
}

SANDBOX_TEXT = {
    "read-only": "The sandbox permits reading files. Writing anything requires approval.",
    "workspace-write": (
        "The sandbox permits reading files and editing files under cwd. Editing "
        "files elsewhere requires approval."
    ),
    "danger-full-access": "No filesystem sandbox is active. Be careful.",
}


def render_permissions(ctx: TurnContext) -> str:
    return (
        "<permissions>\n"
        f"  <sandbox_mode>{ctx.sandbox_mode}</sandbox_mode>\n"
        f"  {SANDBOX_TEXT[ctx.sandbox_mode]}\n"
        f"  <approval_policy>{ctx.approval_policy}</approval_policy>\n"
        f"  {APPROVAL_TEXT[ctx.approval_policy]}\n"
        f"  <network_access>{str(ctx.network_access).lower()}</network_access>\n"
        "</permissions>"
    )


SECTIONS = {
    "environment": render_environment,
    "permissions": render_permissions,
}


class WorldState:
    """Remembers what the model was last told, so it is only told again on change."""

    def __init__(self) -> None:
        self.last: dict[str, str] = {}

    def updates(self, ctx: TurnContext) -> list[str]:
        changed = []
        for name, render in SECTIONS.items():
            text = render(ctx)
            if self.last.get(name) != text:
                self.last[name] = text
                changed.append(text)
        return changed


# --------------------------------------------------------------------------
# Session: one thread, many turns, one TurnContext per turn
# --------------------------------------------------------------------------


class Session:
    def __init__(self, client: ModelClient, defaults: TurnContext) -> None:
        self.client = client
        self.defaults = defaults
        self.history: list[dict[str, Any]] = []
        self.world = WorldState()
        self.turns: list[TurnContext] = []

    def run_turn(self, text: str, *, echo: bool = True, **overrides: Any) -> str:
        # The turn's settings are resolved once, here. Nothing below this line
        # may change them -- a mid-turn `cd` does not move the turn's cwd.
        ctx = self.defaults.with_overrides(**overrides)
        self.defaults = ctx  # thread settings persist to later turns
        self.turns.append(ctx)

        for section in self.world.updates(ctx):
            self.history.append(user_item(section))
        self.history.append(user_item(text))

        last_message = ""
        while True:
            calls: list[dict[str, Any]] = []
            for event in self.client.stream(
                instructions=BASE_INSTRUCTIONS,
                input_items=list(self.history),
                tools=[EXEC_COMMAND_TOOL],
            ):
                if isinstance(event, OutputTextDelta):
                    if echo:
                        print(event.delta, end="", flush=True)
                elif isinstance(event, OutputItemDone):
                    self.history.append(event.item)
                    if event.item.get("type") == "function_call":
                        calls.append(event.item)
                    elif event.item.get("type") == "message":
                        last_message = _message_text(event.item)
                elif isinstance(event, Completed):
                    pass

            if not calls:
                if echo:
                    print()
                return last_message

            for call in calls:
                self.history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": self._dispatch(call, ctx, echo=echo),
                    }
                )

    def _dispatch(self, call: dict[str, Any], ctx: TurnContext, *, echo: bool) -> str:
        if call.get("name") != "exec_command":
            return f"unsupported tool: {call.get('name')}"
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            return f"invalid arguments: {exc}"

        cmd = args.get("cmd", "")
        # The tool reads the turn's cwd, not the process's.
        cwd = args.get("workdir") or ctx.cwd
        if echo:
            print(f"\n$ {cmd}")
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", cmd],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except OSError as exc:
            return f"failed to spawn command: {exc}"
        output = (proc.stdout + proc.stderr)[:MAX_OUTPUT_BYTES]
        return f"Process exited with code {proc.returncode}\nOutput:\n{output}"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    args = argv[1:]
    cwd = os.getcwd()
    if "--cwd" in args:
        i = args.index("--cwd")
        cwd = args[i + 1]
        del args[i : i + 2]

    ctx = TurnContext(cwd=cwd)
    if "--render" in args:
        for section in WorldState().updates(ctx):
            print(section)
            print()
        return 0

    session = Session(ResponsesClient(), ctx)
    if args:
        session.run_turn(" ".join(args))
        return 0

    print("codex (s03) -- ctrl-d to exit; try `--cwd` between runs")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if line:
            session.run_turn(line)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
