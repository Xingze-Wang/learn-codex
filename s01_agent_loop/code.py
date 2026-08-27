#!/usr/bin/env python3
"""
s01: Agent Loop

Codex is a loop around one model call and one shell. The model reads the
conversation, emits `function_call` items, the harness runs them, appends
`function_call_output` items, and calls the model again. When a response
carries no function call, the turn is over.

Two things make this loop Codex-shaped rather than generic:

  1. `store=false` -- the server keeps nothing. Every request carries the whole
     history, including the model's own reasoning items, echoed back verbatim.
  2. The only hand it gets is a shell. `exec_command` runs a command; reading,
     editing and searching are things the model does *with* the shell.

Run:
  python s01_agent_loop/code.py "count the python files under ."
  python s01_agent_loop/code.py            # interactive

Env:
  OPENAI_API_KEY  required for the live path
  CODEX_MODEL     default gpt-5.5

Real source: codex-rs/core/src/client.rs (request construction),
codex-rs/core/src/session/turn.rs (run_turn), codex-rs/core/src/tools/handlers/shell_spec.rs

    +-----------+   input items    +-------+   function_call   +-------+
    | history[] | ---------------> | model | ----------------> | shell |
    +-----------+                  +-------+                   +---+---+
         ^                             |                           |
         |      function_call_output   | no function_call          |
         +-----------------------------+------<--------------------+
                                       v
                                  turn complete
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
DEFAULT_TIMEOUT_MS = 120_000
MAX_OUTPUT_BYTES = 32 * 1024

# Codex ships one base instruction blob per model family. This is a stand-in
# with the same job: say who the agent is and how the shell behaves.
BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in a terminal harness.

You have exactly one tool: `exec_command`, which runs a shell command in the
user's workspace. Read files, search, and edit through that shell.

Work in small steps. Run a command, look at the output, decide the next one.
When the task is done, reply with a short plain-text summary of what you did.
"""


# --------------------------------------------------------------------------
# Tool spec -- shape copied from codex-rs/core/src/tools/handlers/shell_spec.rs
# --------------------------------------------------------------------------

EXEC_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "strict": False,
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
            "workdir": {
                "type": "string",
                "description": "Working directory for the command. Defaults to the turn cwd.",
            },
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
}


# --------------------------------------------------------------------------
# Normalized stream events -- codex-rs calls this enum ResponseEvent
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
    """Anything that can turn a prompt into a stream of ResponseEvents."""

    def stream(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Iterator[ResponseEvent]: ...


class ResponsesClient:
    """The live path: OpenAI Responses API, streaming, stateless."""

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
        # This dict is the whole contract with the model. Codex builds the same
        # one in ResponsesApiRequest: no server-side state, streaming on, and
        # `include` asks for encrypted reasoning so it can be replayed later.
        request = {
            "model": self.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        with self._client.responses.create(**request) as stream:  # type: ignore[arg-type]
            for event in stream:
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    yield OutputTextDelta(event.delta)
                elif kind == "response.output_item.done":
                    yield OutputItemDone(_as_item(event.item))
                elif kind == "response.completed":
                    usage = getattr(event.response, "usage", None)
                    yield Completed(
                        input_tokens=getattr(usage, "input_tokens", 0) or 0,
                        output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    )


def _as_item(item: Any) -> dict[str, Any]:
    raw = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)
    # With store=false the server has no memory of these ids, so Codex drops
    # them before echoing items back (client.rs::prepare_response_items_for_request).
    raw.pop("id", None)
    raw.pop("status", None)
    return raw


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


def exec_command(cmd: str, workdir: str | None = None) -> str:
    """Run one shell command. s07 puts this inside a sandbox."""
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", cmd],
            cwd=workdir or os.getcwd(),
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_MS / 1000,
        )
    except subprocess.TimeoutExpired:
        return f"command timed out after {DEFAULT_TIMEOUT_MS} ms"
    except OSError as exc:
        return f"failed to spawn command: {exc}"

    output = (proc.stdout + proc.stderr)[:MAX_OUTPUT_BYTES]
    return f"Process exited with code {proc.returncode}\nOutput:\n{output}"


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


class Session:
    def __init__(self, client: ModelClient, cwd: str | None = None) -> None:
        self.client = client
        self.cwd = cwd or os.getcwd()
        self.history: list[dict[str, Any]] = []
        self.tokens = 0

    def run_turn(self, user_text: str, *, echo: bool = True) -> str:
        self.history.append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": user_text}],
            }
        )

        last_message = ""
        while True:
            calls: list[dict[str, Any]] = []
            for event in self.client.stream(
                instructions=BASE_INSTRUCTIONS,
                input_items=list(self.history),  # snapshot: codex clones history per request
                tools=[EXEC_COMMAND_TOOL],
            ):
                if isinstance(event, OutputTextDelta):
                    if echo:
                        print(event.delta, end="", flush=True)
                elif isinstance(event, OutputItemDone):
                    # Every item the model produced goes back into history --
                    # messages, reasoning, function calls alike.
                    self.history.append(event.item)
                    if event.item.get("type") == "function_call":
                        calls.append(event.item)
                    elif event.item.get("type") == "message":
                        last_message = _message_text(event.item)
                elif isinstance(event, Completed):
                    self.tokens += event.input_tokens + event.output_tokens

            if not calls:
                # No function call: the model is done with this turn.
                if echo:
                    print()
                return last_message

            for call in calls:
                output = self._dispatch(call, echo=echo)
                self.history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": output,
                    }
                )

    def _dispatch(self, call: dict[str, Any], *, echo: bool) -> str:
        if call.get("name") != "exec_command":
            return f"unsupported tool: {call.get('name')}"
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            return f"invalid arguments: {exc}"

        cmd = args.get("cmd", "")
        if echo:
            print(f"\n$ {cmd}")
        return exec_command(cmd, args.get("workdir") or self.cwd)


def _message_text(item: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for part in item.get("content", [])
        if part.get("type") in ("output_text", "text")
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    session = Session(ResponsesClient())

    if len(argv) > 1:
        session.run_turn(" ".join(argv[1:]))
        return 0

    print("codex (s01) -- ctrl-d to exit")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if line:
            session.run_turn(line)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
