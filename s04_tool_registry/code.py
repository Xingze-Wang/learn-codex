#!/usr/bin/env python3
"""
s04: The tool registry

s01 hard-coded one tool and one `if name == ...`. Codex assembles the tool list
per turn and dispatches by name through a registry, because three things vary:

  * **The model.** Each model record carries a `shell_type`; a model that
    speaks `exec_command` never sees a `shell` tool, and vice versa.
  * **The config.** `web_search`, `view_image`, `update_plan`, MCP servers --
    each is present only when enabled.
  * **The call shape.** Not every tool takes JSON. `apply_patch` is a
    *freeform* tool: the model emits raw patch text constrained by a Lark
    grammar, and it arrives as `custom_tool_call.input`, not as `arguments`.

Two rules keep the loop from breaking:

  1. A tool failure is a *message to the model*, never an exception. Unknown
     name, unparseable arguments, non-zero exit -- all come back as
     `function_call_output` text so the model can correct itself.
  2. Output is truncated on a token budget, head and tail, with a marker in
     the middle. A 200k-line log must not evict the conversation.

Run:
  python s04_tool_registry/code.py --list          # print the assembled tool JSON
  python s04_tool_registry/code.py "how many lines of python are here?"

Real source: codex-rs/core/src/tools/registry.rs, codex-rs/core/src/tools/router.rs,
codex-rs/tools/src/tool_spec.rs, codex-rs/core/src/tools/handlers/apply_patch_spec.rs

Builds on the s01 kernel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")

BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in a terminal harness.
Use the tools you are given. Work in small steps and finish with a short
plain-text summary.
"""

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
# Tool specs: three shapes the Responses API understands
# --------------------------------------------------------------------------

APPLY_PATCH_LARK = """\
start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?

filename: /(.+)/
add_line: "+" /(.*)/ LF -> line

change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF

%import common.LF
"""


def function_tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def freeform_tool(name: str, description: str, grammar: str) -> dict[str, Any]:
    """A `custom` tool: the model emits raw text, parsed by a Lark grammar."""
    return {
        "type": "custom",
        "name": name,
        "description": description,
        "format": {"type": "grammar", "syntax": "lark", "definition": grammar},
    }


EXEC_COMMAND = function_tool(
    "exec_command",
    "Runs a command in the workspace shell and returns its output.",
    {
        "cmd": {"type": "string", "description": "Shell command to execute."},
        "workdir": {"type": "string", "description": "Working directory."},
    },
    ["cmd"],
)

SHELL = function_tool(
    "shell",
    "Runs a command as an argv array.",
    {
        "command": {"type": "array", "items": {"type": "string"}},
        "workdir": {"type": "string"},
        "timeout_ms": {"type": "number"},
    },
    ["command"],
)

UPDATE_PLAN = function_tool(
    "update_plan",
    "Replaces the current plan. Exactly one step may be in_progress.",
    {
        "explanation": {"type": "string"},
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                },
                "required": ["step", "status"],
                "additionalProperties": False,
            },
        },
    },
    ["plan"],
)

APPLY_PATCH = freeform_tool(
    "apply_patch",
    "The `apply_patch` tool can be used to edit files. This is a FREEFORM tool, "
    "so do not wrap the patch in JSON.",
    APPLY_PATCH_LARK,
)

WEB_SEARCH = {"type": "web_search"}


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


class ToolError(Exception):
    """A failure the model should see and can act on."""


Handler = Callable[[dict[str, Any] | str, "ToolContext"], str]


@dataclass
class ToolContext:
    cwd: str
    plan: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class RegisteredTool:
    spec: dict[str, Any]
    handler: Handler
    supports_parallel: bool = True
    freeform: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        name = tool.spec.get("name") or tool.spec["type"]
        self._tools[name] = tool

    def specs(self) -> list[dict[str, Any]]:
        """Exactly what goes into the request body's `tools` array."""
        return [tool.spec for tool in self._tools.values()]

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def supports_parallel(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.supports_parallel)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

MAX_OUTPUT_TOKENS = 10_000
CHARS_PER_TOKEN = 4  # the same crude estimate codex uses before it has real counts


def truncate_output(text: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """Keep the head and the tail; the middle is where logs repeat themselves."""
    budget = max_tokens * CHARS_PER_TOKEN
    if len(text) <= budget:
        return text
    head = text[: budget // 2]
    tail = text[-budget // 2 :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n[... {dropped} characters truncated ...]\n{tail}"


def handle_exec_command(args: Any, ctx: ToolContext) -> str:
    cmd = args.get("cmd")
    if not cmd:
        raise ToolError("exec_command requires `cmd`")
    return _run(["/bin/bash", "-lc", cmd], args.get("workdir") or ctx.cwd)


def handle_shell(args: Any, ctx: ToolContext) -> str:
    command = args.get("command")
    if not isinstance(command, list) or not command:
        raise ToolError("shell requires a non-empty `command` array")
    return _run([str(part) for part in command], args.get("workdir") or ctx.cwd)


def _run(argv: list[str], cwd: str) -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise ToolError("command timed out") from None
    except OSError as exc:
        raise ToolError(f"failed to spawn command: {exc}") from None
    output = truncate_output(proc.stdout + proc.stderr)
    return f"Process exited with code {proc.returncode}\nOutput:\n{output}"


def handle_update_plan(args: Any, ctx: ToolContext) -> str:
    plan = args.get("plan")
    if not isinstance(plan, list) or not plan:
        raise ToolError("update_plan requires a non-empty `plan`")
    in_progress = [step for step in plan if step.get("status") == "in_progress"]
    if len(in_progress) > 1:
        raise ToolError("at most one step may be in_progress")
    ctx.plan = plan
    return "plan updated"


def handle_apply_patch(patch_text: Any, ctx: ToolContext) -> str:
    """Freeform: the payload is the patch itself. s05 implements the parser."""
    if not isinstance(patch_text, str) or not patch_text.lstrip().startswith("*** Begin Patch"):
        raise ToolError("apply_patch input must start with '*** Begin Patch'")
    files = [
        line.split(": ", 1)[1]
        for line in patch_text.splitlines()
        if line.startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: "))
    ]
    return "would patch: " + ", ".join(files) + "  (see s05 for the real applier)"


# --------------------------------------------------------------------------
# Per-turn assembly: the model and the config decide what exists
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolsConfig:
    shell_type: str = "exec_command"  # from the model record: exec_command | shell
    apply_patch: bool = True
    plan_tool: bool = True
    web_search: bool = False


def build_registry(config: ToolsConfig) -> ToolRegistry:
    registry = ToolRegistry()

    if config.shell_type == "shell":
        registry.register(RegisteredTool(SHELL, handle_shell, supports_parallel=False))
    else:
        registry.register(RegisteredTool(EXEC_COMMAND, handle_exec_command, supports_parallel=False))

    if config.apply_patch:
        # Freeform tools never run in parallel: two patches racing on one file
        # is a merge conflict with extra steps.
        registry.register(
            RegisteredTool(APPLY_PATCH, handle_apply_patch, supports_parallel=False, freeform=True)
        )
    if config.plan_tool:
        registry.register(RegisteredTool(UPDATE_PLAN, handle_update_plan))
    if config.web_search:
        registry.register(RegisteredTool(WEB_SEARCH, lambda *_: "", supports_parallel=True))

    return registry


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    name: str
    call_id: str
    payload: dict[str, Any] | str  # dict for function tools, str for freeform


def build_tool_call(item: dict[str, Any]) -> ToolCall | None:
    """One place that knows how a response item becomes a call."""
    if item.get("type") == "function_call":
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise ToolError(f"invalid JSON arguments: {exc}") from None
        return ToolCall(item.get("name", ""), item.get("call_id", ""), args)
    if item.get("type") == "custom_tool_call":
        return ToolCall(item.get("name", ""), item.get("call_id", ""), item.get("input", ""))
    return None


def dispatch(registry: ToolRegistry, call: ToolCall, ctx: ToolContext) -> str:
    tool = registry.get(call.name)
    if tool is None:
        # Not a crash: the model gets told and picks a tool that exists.
        return f"unsupported tool: {call.name}"
    try:
        return tool.handler(call.payload, ctx)
    except ToolError as exc:
        return f"error: {exc}"
    except Exception as exc:  # a bug in a handler must not kill the session
        return f"internal tool error: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# The loop, now registry-driven
# --------------------------------------------------------------------------


class Session:
    def __init__(self, client: ModelClient, registry: ToolRegistry, cwd: str | None = None) -> None:
        self.client = client
        self.registry = registry
        self.ctx = ToolContext(cwd=cwd or os.getcwd())
        self.history: list[dict[str, Any]] = []

    def run_turn(self, text: str, *, echo: bool = True) -> str:
        self.history.append(user_item(text))
        last_message = ""

        while True:
            items: list[dict[str, Any]] = []
            for event in self.client.stream(
                instructions=BASE_INSTRUCTIONS,
                input_items=list(self.history),
                tools=self.registry.specs(),
            ):
                if isinstance(event, OutputTextDelta):
                    if echo:
                        print(event.delta, end="", flush=True)
                elif isinstance(event, OutputItemDone):
                    self.history.append(event.item)
                    items.append(event.item)
                    if event.item.get("type") == "message":
                        last_message = _message_text(event.item)

            calls: list[ToolCall] = []
            rejected = 0
            for item in items:
                try:
                    call = build_tool_call(item)
                except ToolError as exc:
                    # The model emitted something unparseable. Answer the call
                    # anyway -- an unanswered call_id breaks the next request.
                    rejected += 1
                    self.history.append(
                        {
                            "type": "function_call_output",
                            "call_id": item.get("call_id", ""),
                            "output": f"error: {exc}",
                        }
                    )
                    continue
                if call is not None:
                    calls.append(call)

            if not calls:
                if rejected:
                    continue  # give the model a chance to fix its own call
                if echo:
                    print()
                return last_message

            for call in calls:
                if echo:
                    print(f"\n[{call.name}]")
                output = dispatch(self.registry, call, self.ctx)
                self.history.append(
                    {
                        "type": "function_call_output" if isinstance(call.payload, dict) else "custom_tool_call_output",
                        "call_id": call.call_id,
                        "output": output,
                    }
                )


def main(argv: list[str]) -> int:
    registry = build_registry(ToolsConfig())
    if "--list" in argv:
        print(json.dumps(registry.specs(), indent=2))
        return 0

    session = Session(ResponsesClient(), registry)
    if len(argv) > 1:
        session.run_turn(" ".join(argv[1:]))
        return 0

    print("codex (s04) -- ctrl-d to exit")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if line:
            session.run_turn(line)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
