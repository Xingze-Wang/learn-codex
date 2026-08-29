#!/usr/bin/env python3
"""
s15: The harness

Fourteen chapters, fourteen mechanisms, each demonstrated alone. This one puts
them in the same process and runs a real turn through all of them.

It is the only chapter that does not repeat its dependencies: it imports the
earlier chapters' `code.py` files directly. That is the argument, not a
shortcut -- these are separable modules with narrow interfaces, and a harness
is what you get when you compose them.

The order of the checks around a single `exec_command` is the whole design:

    PreToolUse hook          s14   someone else's policy, first
    exec policy rule         s09   allow / prompt / forbidden, per segment
    safety assessment        s08   auto-approve, ask, or refuse
    run inside the sandbox   s07   the kernel enforces it, not a string check
    denial -> ask -> retry   s08   escalate only on an actual denial
    PostToolUse hook         s14
    record to the rollout    s10   so this turn survives the process

Wrapped around that: one turn context (s03), one tool registry (s04), the
patch tool (s05), MCP tools (s13), token accounting and auto-compaction (s11),
instructions assembled from AGENTS.md and skills (s12) -- all driven through
the submission/event queues (s02) so a frontend can steer, interrupt, and
answer approvals while the turn runs.

Two frontends read the same event stream, which is the point of s02:

    python s15_harness/code.py "fix the failing test"
    python s15_harness/code.py --json "fix the failing test"   # one JSON event per line
    python s15_harness/code.py --dry-run                       # wire everything, call no model

`--json` is `codex exec --json`: the same core, a different reader -- and a coarser,
stable schema (thread.started / item.completed / turn.completed) rather than the
internal event names.

Real source: codex-rs/core/src/session/turn.rs (run_turn), codex-rs/core/src/tools/router.rs,
codex-rs/exec/src/event_processor_with_jsonl_output.rs
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
# Sessions written by this repo go to ~/.learn-codex, not ~/.codex: a teaching
# harness has no business appearing in your real `codex resume` list. Point
# CODEX_HOME at ~/.codex to read (and write) the real one.
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.expanduser("~/.learn-codex"))
ROOT = Path(__file__).resolve().parent.parent


def _chapter(name: str) -> Any:
    """Load a sibling chapter as a module. s15 composes; it does not copy."""
    spec = importlib.util.spec_from_file_location(f"learn_codex_{name}", ROOT / name / "code.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


patcher = _chapter("s05_apply_patch")
sandboxing = _chapter("s07_sandbox")
execpolicy = _chapter("s09_exec_policy")
rollout = _chapter("s10_rollout")
compaction = _chapter("s11_compaction")
instructions = _chapter("s12_instructions")
mcp = _chapter("s13_mcp")
hooks = _chapter("s14_hooks")

# --------------------------------------------------------------------------
# Kernel (s01 + s02) -- streaming client bridged onto the event loop
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


async def _astream(client: ModelClient, **kwargs: Any) -> AsyncIterator[ResponseEvent]:
    """Blocking SSE iterator -> async iterator, so the turn stays cancellable."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def produce() -> None:
        try:
            for event in client.stream(**kwargs):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=produce, daemon=True).start()
    while True:
        item = await queue.get()
        if item is None:
            return
        if isinstance(item, Exception):
            raise item
        yield item


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
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    cwd: str = field(default_factory=os.getcwd)
    model: str = DEFAULT_MODEL
    codex_home: str = CODEX_HOME
    approval_policy: str = "on-request"
    sandbox_mode: str = "workspace-write"
    network_access: bool = False
    context_window: int = 272_000
    auto_compact_ratio: float = 0.80
    mcp_servers: tuple[Any, ...] = ()
    record_rollout: bool = True


@dataclass(frozen=True)
class TurnContext:
    """Frozen at turn start (s03). Tools read this, never a global."""

    cwd: str
    model: str
    approval_policy: str
    sandbox_mode: str
    network_access: bool

    def sandbox_policy(self) -> Any:
        return sandboxing.SandboxPolicy(mode=self.sandbox_mode, network_access=self.network_access)


# --------------------------------------------------------------------------
# Protocol (s02)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class ExecApproval:
    call_id: str
    decision: str


@dataclass(frozen=True)
class Compact:
    pass


@dataclass(frozen=True)
class Interrupt:
    pass


@dataclass(frozen=True)
class Shutdown:
    pass


Op = UserTurn | ExecApproval | Compact | Interrupt | Shutdown


@dataclass(frozen=True)
class Submission:
    id: str
    op: Op


@dataclass(frozen=True)
class SessionConfigured:
    session_id: str
    model: str
    cwd: str
    rollout_path: str
    tools: list[str]
    mcp_ready: list[str]
    mcp_failed: dict[str, str]


@dataclass(frozen=True)
class TaskStarted:
    turn_id: str


@dataclass(frozen=True)
class AgentMessageDelta:
    delta: str


@dataclass(frozen=True)
class AgentMessage:
    message: str


@dataclass(frozen=True)
class ExecCommandBegin:
    call_id: str
    command: str
    sandboxed: bool


@dataclass(frozen=True)
class ExecCommandEnd:
    call_id: str
    exit_code: int
    output: str


@dataclass(frozen=True)
class ExecApprovalRequest:
    call_id: str
    command: str
    reason: str


@dataclass(frozen=True)
class PatchApplyEnd:
    call_id: str
    files: list[str]
    success: bool


@dataclass(frozen=True)
class TurnDiff:
    unified_diff: str


@dataclass(frozen=True)
class PlanUpdate:
    plan: list[dict[str, str]]


@dataclass(frozen=True)
class McpToolCallEnd:
    call_id: str
    tool: str
    output: str


@dataclass(frozen=True)
class TokenCount:
    used: int
    window: int
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ContextCompacted:
    before: int
    after: int


@dataclass(frozen=True)
class BackgroundNotice:
    message: str


@dataclass(frozen=True)
class TaskComplete:
    last_agent_message: str


@dataclass(frozen=True)
class TurnAborted:
    reason: str


@dataclass(frozen=True)
class ErrorEvent:
    message: str


@dataclass(frozen=True)
class ShutdownComplete:
    pass


@dataclass(frozen=True)
class Event:
    id: str
    msg: Any


# --------------------------------------------------------------------------
# Tool specs (s04, s05)
# --------------------------------------------------------------------------

EXEC_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "workdir": {"type": "string"},
            "justification": {"type": "string"},
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
}

APPLY_PATCH_TOOL: dict[str, Any] = {
    "type": "custom",
    "name": "apply_patch",
    "description": "Edit files. FREEFORM: emit the patch itself, not JSON.",
    "format": {
        "type": "grammar",
        "syntax": "lark",
        "definition": (
            'start: begin_patch hunk+ end_patch\n'
            'begin_patch: "*** Begin Patch" LF\n'
            'end_patch: "*** End Patch" LF?\n'
            "hunk: add_hunk | delete_hunk | update_hunk\n"
            'add_hunk: "*** Add File: " filename LF add_line+\n'
            'delete_hunk: "*** Delete File: " filename LF\n'
            'update_hunk: "*** Update File: " filename LF change_move? change?\n'
            "filename: /(.+)/\n"
            'add_line: "+" /(.*)/ LF -> line\n'
            'change_move: "*** Move to: " filename LF\n'
            "change: (change_context | change_line)+ eof_line?\n"
            'change_context: ("@@" | "@@ " /(.+)/) LF\n'
            'change_line: ("+" | "-" | " ") /(.*)/ LF\n'
            'eof_line: "*** End of File" LF\n'
            "%import common.LF\n"
        ),
    },
}

UPDATE_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "update_plan",
    "description": "Replace the plan. Exactly one step may be in_progress.",
    "parameters": {
        "type": "object",
        "properties": {
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
            }
        },
        "required": ["plan"],
        "additionalProperties": False,
    },
}


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class Session:
    def __init__(self, client: ModelClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.session_id = str(uuid.uuid4())
        self.history: list[dict[str, Any]] = []
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.pending_input: list[str] = []
        self.pending_approvals: dict[str, asyncio.Future[str]] = {}
        self.approved_commands: set[str] = set()
        self.plan: list[dict[str, str]] = []
        self.turn_diffs: list[str] = []
        self.active: asyncio.Task[None] | None = None
        self.compactions = 0
        self.input_tokens = 0
        self.output_tokens = 0

        # s09, s12, s13, s14, s10 -- built once, read every turn.
        self.exec_policy = execpolicy.default_policy()
        self.prompt = instructions.build_prompt(
            config.cwd, codex_home=config.codex_home, permissions_block=self._permissions_block()
        )
        self.hook_runner = hooks.HookRunner(
            hooks.HookConfig.load(Path(config.codex_home) / "hooks.json"),
            cwd=config.cwd,
            session_id=self.session_id,
        )
        self.mcp = mcp.McpConnectionManager()
        self.mcp_report = self.mcp.start_all(list(config.mcp_servers)) if config.mcp_servers else mcp.StartupReport()
        self.recorder = (
            rollout.RolloutRecorder.create(
                config.codex_home, cwd=config.cwd, model=config.model, thread_id=self.session_id
            )
            if config.record_rollout
            else None
        )

        self.history.extend(self.prompt.items)
        start = self.hook_runner.run(hooks.SESSION_START, subject="startup", source="startup")
        for text in start.additional_context:
            self.history.append(user_item(f"<hook_context>\n{text}\n</hook_context>"))

    # -- setup helpers ---------------------------------------------------

    def _permissions_block(self) -> str:
        return (
            "<permissions>\n"
            f"  <sandbox_mode>{self.config.sandbox_mode}</sandbox_mode>\n"
            f"  <approval_policy>{self.config.approval_policy}</approval_policy>\n"
            f"  <network_access>{str(self.config.network_access).lower()}</network_access>\n"
            "</permissions>"
        )

    def turn_context(self) -> TurnContext:
        return TurnContext(
            cwd=self.config.cwd,
            model=self.config.model,
            approval_policy=self.config.approval_policy,
            sandbox_mode=self.config.sandbox_mode,
            network_access=self.config.network_access,
        )

    def tool_specs(self) -> list[dict[str, Any]]:
        builtin = [EXEC_COMMAND_TOOL, APPLY_PATCH_TOOL, UPDATE_PLAN_TOOL]
        return mcp.build_tool_specs(builtin, list(self.mcp.tools.values()))

    def tool_names(self) -> list[str]:
        names = []
        for spec in self.tool_specs():
            if spec.get("type") == "namespace":
                names.extend(spec["name"] + tool["name"] for tool in spec["tools"])
            else:
                names.append(spec.get("name", spec["type"]))
        return names

    # -- event plumbing --------------------------------------------------

    def emit(self, sub_id: str, msg: Any) -> None:
        self.events.put_nowait(Event(sub_id, msg))

    def record_item(self, item: dict[str, Any]) -> None:
        self.history.append(item)
        if self.recorder:
            self.recorder.record_response_item(item)

    def record_event(self, event_type: str, **payload: Any) -> None:
        if self.recorder:
            self.recorder.record_event(event_type, **payload)

    # -- the turn --------------------------------------------------------

    async def run_turn(self, sub_id: str, text: str) -> None:
        turn = self.turn_context()
        turn_id = uuid.uuid4().hex[:12]
        self.emit(sub_id, TaskStarted(turn_id))
        if self.recorder:
            self.recorder.record_turn_context(
                turn_id=turn_id, cwd=turn.cwd, approval_policy=turn.approval_policy,
                sandbox_policy=turn.sandbox_mode, model=turn.model,
            )
        self.record_event("task_started", turn_id=turn_id)

        submitted = self.hook_runner.run(hooks.USER_PROMPT_SUBMIT, subject=text, prompt=text)
        if submitted.blocked:
            self.emit(sub_id, ErrorEvent(f"prompt blocked by hook: {submitted.reason}"))
            self.emit(sub_id, TaskComplete(""))
            return
        for extra in submitted.additional_context:
            self.record_item(user_item(f"<hook_context>\n{extra}\n</hook_context>"))

        self.record_item(user_item(text))
        self.record_event("user_message", message=text)
        last_message = ""

        try:
            while True:
                for queued in self._drain_pending():
                    self.record_item(user_item(queued))

                status = self._token_status()
                if status.needs_compaction(self.config.auto_compact_ratio):
                    await self._compact(sub_id)

                calls: list[tuple[str, str, Any]] = []
                async for event in _astream(
                    self.client,
                    instructions=self.prompt.instructions,
                    input_items=list(self.history),
                    tools=self.tool_specs(),
                ):
                    if isinstance(event, OutputTextDelta):
                        self.emit(sub_id, AgentMessageDelta(event.delta))
                    elif isinstance(event, OutputItemDone):
                        self.record_item(event.item)
                        parsed = _build_tool_call(event.item)
                        if parsed:
                            calls.append(parsed)
                        elif event.item.get("type") == "message":
                            last_message = _message_text(event.item)
                            self.emit(sub_id, AgentMessage(last_message))
                            self.record_event("agent_message", message=last_message)
                    elif isinstance(event, Completed):
                        self.input_tokens += event.input_tokens
                        self.output_tokens += event.output_tokens
                        self.emit(
                            sub_id,
                            TokenCount(
                                self._token_status().used,
                                self.config.context_window,
                                self.input_tokens,
                                self.output_tokens,
                            ),
                        )

                if not calls:
                    if self.pending_input:
                        continue
                    if self.turn_diffs:
                        self.emit(sub_id, TurnDiff("\n".join(self.turn_diffs)))
                        self.turn_diffs.clear()
                    self.emit(sub_id, TaskComplete(last_message))
                    self.record_event("task_complete", turn_id=turn_id, last_agent_message=last_message)
                    return

                for name, call_id, payload in calls:
                    output = await self._dispatch(sub_id, turn, name, call_id, payload)
                    self.record_item(
                        {
                            "type": "function_call_output" if isinstance(payload, dict) else "custom_tool_call_output",
                            "call_id": call_id,
                            "output": output,
                        }
                    )
        except asyncio.CancelledError:
            self.record_item(user_item("[turn interrupted by user]"))
            self.record_event("turn_aborted", turn_id=turn_id)
            self.emit(sub_id, TurnAborted("interrupted"))
            raise
        except Exception as exc:
            self.emit(sub_id, ErrorEvent(f"{type(exc).__name__}: {exc}"))
            self.record_event("error", message=str(exc))

    def _drain_pending(self) -> list[str]:
        drained, self.pending_input = self.pending_input, []
        return drained

    def _token_status(self) -> Any:
        return compaction.TokenStatus(
            compaction.history_tokens(self.history), self.config.context_window
        )

    async def _compact(self, sub_id: str) -> None:
        before = compaction.history_tokens(self.history)
        summary = await asyncio.to_thread(compaction.request_summary, self.client, self.history)
        self.history = compaction.build_compacted_history(
            compaction.session_prefix(self.history),
            compaction.collect_user_messages(self.history),
            summary,
        )
        self.compactions += 1
        after = compaction.history_tokens(self.history)
        self.emit(sub_id, ContextCompacted(before, after))
        self.record_event("context_compacted", before=before, after=after)

    # -- dispatch --------------------------------------------------------

    async def _dispatch(
        self, sub_id: str, turn: TurnContext, name: str, call_id: str, payload: Any
    ) -> str:
        pre = self.hook_runner.run(
            hooks.PRE_TOOL_USE, subject=name, tool_name=name, tool_input=payload
        )
        for extra in pre.additional_context:
            self.record_item(user_item(f"<hook_context>\n{extra}\n</hook_context>"))
        if pre.blocked:
            return f"blocked by a hook: {pre.reason}"

        if name == "exec_command":
            output = await self._exec_command(sub_id, turn, call_id, payload, ask_first=pre.decision == hooks.ASK)
        elif name == "apply_patch":
            output = self._apply_patch(sub_id, turn, call_id, payload)
        elif name == "update_plan":
            output = self._update_plan(sub_id, payload)
        elif name in self.mcp.tools:
            output = await asyncio.to_thread(self.mcp.call, name, payload if isinstance(payload, dict) else {})
            self.emit(sub_id, McpToolCallEnd(call_id, name, output))
        else:
            output = f"unsupported tool: {name}"

        self.hook_runner.run(hooks.POST_TOOL_USE, subject=name, tool_name=name, tool_output=output[:2000])
        return output

    async def _exec_command(
        self, sub_id: str, turn: TurnContext, call_id: str, payload: Any, *, ask_first: bool
    ) -> str:
        if not isinstance(payload, dict):
            return "error: exec_command expects JSON arguments"
        cmd = payload.get("cmd", "")
        cwd = payload.get("workdir") or turn.cwd
        justification = payload.get("justification", "")

        # s09: the rule file gets the first word after the hooks.
        verdict = execpolicy.evaluate(self.exec_policy, cmd)
        if verdict.decision == execpolicy.FORBIDDEN:
            return f"command not run: forbidden by policy ({verdict.reason})"

        needs_ask = ask_first or (
            verdict.decision == execpolicy.PROMPT and turn.approval_policy == "untrusted"
        )
        already_approved = cmd in self.approved_commands
        if needs_ask and not already_approved:
            decision = await self._ask(sub_id, call_id, cmd, verdict.reason or justification)
            if decision in ("denied", "abort"):
                return "command not run: the user declined it"
            if decision == "approved_for_session":
                self.approved_commands.add(cmd)
            return self._run(sub_id, call_id, cmd, cwd, sandboxed=False)

        sandboxed = not already_approved and turn.sandbox_mode != sandboxing.DANGER_FULL_ACCESS
        self.emit(sub_id, ExecCommandBegin(call_id, cmd, sandboxed))
        self.record_event("exec_command_begin", call_id=call_id, command=cmd)
        result = await asyncio.to_thread(
            sandboxing.run_sandboxed,
            cmd,
            turn.sandbox_policy() if sandboxed else sandboxing.SandboxPolicy(mode=sandboxing.DANGER_FULL_ACCESS),
            cwd,
        )

        if sandboxing.is_likely_sandbox_denied(result):
            if turn.approval_policy == "never":
                self._end_exec(sub_id, call_id, result)
                return (
                    f"Process exited with code {result.exit_code}\n"
                    "The sandbox blocked this command and approvals are disabled.\n"
                    f"Output:\n{result.aggregated}"
                )
            decision = await self._ask(sub_id, call_id, cmd, "the sandbox blocked this command")
            if decision in ("denied", "abort"):
                return "command not run: the user declined the escalation"
            if decision == "approved_for_session":
                self.approved_commands.add(cmd)
            return self._run(sub_id, call_id, cmd, cwd, sandboxed=False)

        self._end_exec(sub_id, call_id, result)
        return f"Process exited with code {result.exit_code}\nOutput:\n{result.aggregated}"

    def _run(self, sub_id: str, call_id: str, cmd: str, cwd: str, *, sandboxed: bool) -> str:
        self.emit(sub_id, ExecCommandBegin(call_id, cmd, sandboxed))
        policy = sandboxing.SandboxPolicy(
            mode=sandboxing.WORKSPACE_WRITE if sandboxed else sandboxing.DANGER_FULL_ACCESS
        )
        result = sandboxing.run_sandboxed(cmd, policy, cwd)
        self._end_exec(sub_id, call_id, result)
        return f"Process exited with code {result.exit_code}\nOutput:\n{result.aggregated}"

    def _end_exec(self, sub_id: str, call_id: str, result: Any) -> None:
        self.emit(sub_id, ExecCommandEnd(call_id, result.exit_code, result.aggregated))
        self.record_event("exec_command_end", call_id=call_id, exit_code=result.exit_code)

    def _apply_patch(self, sub_id: str, turn: TurnContext, call_id: str, payload: Any) -> str:
        if not isinstance(payload, str):
            return "error: apply_patch is freeform; send the patch text, not JSON"
        try:
            changes = patcher.apply_patch(payload, turn.cwd)
        except patcher.PatchError as exc:
            self.emit(sub_id, PatchApplyEnd(call_id, [], False))
            return f"patch failed: {exc}"

        for change in changes:
            diff = change.unified_diff()
            if diff:
                self.turn_diffs.append(diff)
        files = [change.path for change in changes]
        self.emit(sub_id, PatchApplyEnd(call_id, files, True))
        self.record_event("patch_apply_end", call_id=call_id, files=files, success=True)
        return "applied: " + ", ".join(files)

    def _update_plan(self, sub_id: str, payload: Any) -> str:
        if not isinstance(payload, dict):
            return "error: update_plan expects JSON arguments"
        plan = payload.get("plan") or []
        if sum(1 for step in plan if step.get("status") == "in_progress") > 1:
            return "error: at most one step may be in_progress"
        self.plan = plan
        self.emit(sub_id, PlanUpdate(plan))
        return "plan updated"

    # -- approvals -------------------------------------------------------

    async def _ask(self, sub_id: str, call_id: str, cmd: str, reason: str) -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending_approvals[call_id] = future
        self.emit(sub_id, ExecApprovalRequest(call_id, cmd, reason))
        try:
            return await future
        finally:
            self.pending_approvals.pop(call_id, None)

    def resolve_approval(self, call_id: str, decision: str) -> bool:
        future = self.pending_approvals.get(call_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def close(self) -> None:
        self.hook_runner.run(hooks.SESSION_END, subject="")
        self.mcp.close_all()


def _tool_name(item: dict[str, Any]) -> str:
    """Rejoin the split name.

    A namespaced tool (s13) arrives as {"namespace": "mcp__wiki__", "name":
    "search"}; the router dispatches on the flat "mcp__wiki__search". Codex
    does the same join in ToolName::new(namespace, name).
    """
    name = item.get("name", "")
    namespace = item.get("namespace") or ""
    return f"{namespace}{name}" if namespace not in ("", "functions") else name


def _build_tool_call(item: dict[str, Any]) -> tuple[str, str, Any] | None:
    if item.get("type") == "function_call":
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        return _tool_name(item), item.get("call_id", ""), args
    if item.get("type") == "custom_tool_call":
        return _tool_name(item), item.get("call_id", ""), item.get("input", "")
    return None


# --------------------------------------------------------------------------
# Thread
# --------------------------------------------------------------------------


class CodexThread:
    def __init__(self, client: ModelClient, config: Config) -> None:
        self.session = Session(client, config)
        self.submissions: asyncio.Queue[Submission] = asyncio.Queue()

    def start(self) -> None:
        sess = self.session
        sess.emit(
            "init",
            SessionConfigured(
                session_id=sess.session_id,
                model=sess.config.model,
                cwd=sess.config.cwd,
                rollout_path=str(sess.recorder.path) if sess.recorder else "",
                tools=sess.tool_names(),
                mcp_ready=sess.mcp_report.ready,
                mcp_failed=sess.mcp_report.failed,
            ),
        )
        asyncio.create_task(self._submission_loop())

    async def submit(self, op: Op) -> str:
        sub_id = uuid.uuid4().hex[:8]
        await self.submissions.put(Submission(sub_id, op))
        return sub_id

    async def next_event(self) -> Event:
        return await self.session.events.get()

    async def _submission_loop(self) -> None:
        sess = self.session
        while True:
            sub = await self.submissions.get()
            op = sub.op
            if isinstance(op, UserTurn):
                if sess.active and not sess.active.done():
                    sess.pending_input.append(op.text)
                    sess.emit(sub.id, BackgroundNotice(f"queued for the running turn: {op.text}"))
                    continue
                sess.active = asyncio.create_task(sess.run_turn(sub.id, op.text))
            elif isinstance(op, ExecApproval):
                if not sess.resolve_approval(op.call_id, op.decision):
                    sess.emit(sub.id, ErrorEvent(f"no pending approval for {op.call_id}"))
            elif isinstance(op, Compact):
                await sess._compact(sub.id)
            elif isinstance(op, Interrupt):
                if sess.active and not sess.active.done():
                    sess.active.cancel()
                else:
                    sess.emit(sub.id, TurnAborted("no active turn"))
            elif isinstance(op, Shutdown):
                if sess.active and not sess.active.done():
                    sess.active.cancel()
                sess.close()
                sess.emit(sub.id, ShutdownComplete())
                return


# --------------------------------------------------------------------------
# Frontends
# --------------------------------------------------------------------------


def event_to_json(event: Event) -> str:
    """The raw internal event, for debugging. `--json` emits the public
    ThreadEvent schema instead; see ThreadEventWriter."""
    msg = event.msg
    payload = asdict(msg) if is_dataclass(msg) else {}
    return json.dumps(
        {"id": event.id, "msg": {"type": _snake(type(msg).__name__), **payload}},
        ensure_ascii=False,
    )


def _snake(name: str) -> str:
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


async def render_human(thread: CodexThread, done: asyncio.Event, *, interactive: bool) -> None:
    while True:
        event = await thread.next_event()
        msg = event.msg
        if isinstance(msg, SessionConfigured):
            print(f"session {msg.session_id[:8]}  model {msg.model}")
            print(f"tools: {', '.join(msg.tools)}")
            if msg.mcp_failed:
                for name, reason in msg.mcp_failed.items():
                    print(f"mcp {name} unavailable: {reason}")
            if msg.rollout_path:
                print(f"rollout: {msg.rollout_path}")
            print()
        elif isinstance(msg, AgentMessageDelta):
            print(msg.delta, end="", flush=True)
        elif isinstance(msg, ExecCommandBegin):
            print(f"\n$ {msg.command}   [{'sandboxed' if msg.sandboxed else 'unsandboxed'}]")
        elif isinstance(msg, ExecCommandEnd):
            for line in msg.output.strip().splitlines()[:4]:
                print(f"  {line}")
            print(f"  (exit {msg.exit_code})")
        elif isinstance(msg, PatchApplyEnd):
            print(f"\n[patch {'applied' if msg.success else 'failed'}: {', '.join(msg.files)}]")
        elif isinstance(msg, PlanUpdate):
            print("\n[plan]")
            for step in msg.plan:
                mark = {"completed": "x", "in_progress": ">"}.get(step.get("status", ""), " ")
                print(f"  [{mark}] {step.get('step')}")
        elif isinstance(msg, McpToolCallEnd):
            print(f"\n[{msg.tool}] {msg.output[:200]}")
        elif isinstance(msg, ContextCompacted):
            print(f"\n[auto-compacted {msg.before} -> {msg.after} tokens]")
        elif isinstance(msg, TurnDiff):
            print("\n[turn diff]")
            print(msg.unified_diff)
        elif isinstance(msg, ExecApprovalRequest):
            print(f"\n! {msg.reason}\n  command: {msg.command}")
            if interactive:
                answer = (await asyncio.to_thread(input, "  allow? [y/N/always] ")).strip().lower()
            else:
                answer = "n"
                print("  (non-interactive: declining)")
            await thread.submit(
                ExecApproval(
                    msg.call_id,
                    {"y": "approved", "yes": "approved", "always": "approved_for_session"}.get(answer, "denied"),
                )
            )
        elif isinstance(msg, TaskComplete):
            print()
            done.set()
        elif isinstance(msg, (TurnAborted, ErrorEvent)):
            print(f"\n[{_snake(type(msg).__name__)}] {getattr(msg, 'reason', getattr(msg, 'message', ''))}")
            done.set()
        elif isinstance(msg, ShutdownComplete):
            done.set()
            return


class ThreadEventWriter:
    """Translate internal events into the public `codex exec --json` schema.

        thread.started -> turn.started -> item.started / item.completed -> turn.completed

    Internal `EventMsg` names are an implementation detail and change with the
    harness. `item.completed` is a contract other programs parse. The headless
    frontend is exactly where one becomes the other -- which is why the
    translation lives here and not in the session.

    Deltas have no place in this schema: a consumer that wanted a token stream
    would not be parsing JSONL.
    """

    def __init__(self) -> None:
        self._next_id = 0
        self._open: dict[str, tuple[str, str]] = {}  # call_id -> (item id, command)
        self._usage = {"input_tokens": 0, "cached_input_tokens": 0,
                       "cache_write_input_tokens": 0, "output_tokens": 0,
                       "reasoning_output_tokens": 0}

    def _item_id(self) -> str:
        item_id = f"item_{self._next_id}"
        self._next_id += 1
        return item_id

    def translate(self, msg: Any) -> list[dict[str, Any]]:
        if isinstance(msg, SessionConfigured):
            return [{"type": "thread.started", "thread_id": msg.session_id}]
        if isinstance(msg, TaskStarted):
            return [{"type": "turn.started"}]

        if isinstance(msg, ExecCommandBegin):
            item_id = self._item_id()
            self._open[msg.call_id] = (item_id, msg.command)
            return [{"type": "item.started", "item": {
                "id": item_id, "type": "command_execution", "command": msg.command,
                "aggregated_output": "", "exit_code": None, "status": "in_progress"}}]
        if isinstance(msg, ExecCommandEnd):
            # The completed item repeats the whole command: a consumer reading
            # only item.completed must not have to correlate with item.started.
            item_id, command = self._open.pop(msg.call_id, None) or (self._item_id(), "")
            return [{"type": "item.completed", "item": {
                "id": item_id, "type": "command_execution", "command": command,
                "aggregated_output": msg.output, "exit_code": msg.exit_code,
                "status": "completed" if msg.exit_code == 0 else "failed"}}]

        if isinstance(msg, AgentMessage):
            return [{"type": "item.completed", "item": {
                "id": self._item_id(), "type": "agent_message", "text": msg.message}}]
        if isinstance(msg, PatchApplyEnd):
            return [{"type": "item.completed", "item": {
                "id": self._item_id(), "type": "file_change",
                "changes": [{"path": path, "kind": "update"} for path in msg.files],
                "status": "completed" if msg.success else "failed"}}]
        if isinstance(msg, PlanUpdate):
            return [{"type": "item.completed", "item": {
                "id": self._item_id(), "type": "todo_list",
                "items": [{"text": step.get("step", ""),
                           "completed": step.get("status") == "completed"}
                          for step in msg.plan]}}]
        if isinstance(msg, McpToolCallEnd):
            server, _, tool = msg.tool.partition("__")
            return [{"type": "item.completed", "item": {
                "id": self._item_id(), "type": "mcp_tool_call",
                "server": server, "tool": tool, "arguments": {},
                "result": {"content": [{"type": "text", "text": msg.output}]},
                "error": None, "status": "completed"}}]

        if isinstance(msg, TokenCount):
            self._usage["input_tokens"] = msg.input_tokens
            self._usage["output_tokens"] = msg.output_tokens
            return []
        if isinstance(msg, TaskComplete):
            return [{"type": "turn.completed", "usage": dict(self._usage)}]
        if isinstance(msg, TurnAborted):
            return [{"type": "turn.failed", "error": {"message": msg.reason}}]
        if isinstance(msg, ErrorEvent):
            return [{"type": "error", "message": msg.message}]
        return []


async def render_json(thread: CodexThread, done: asyncio.Event) -> None:
    """`codex exec --json`: one JSON object per line, nothing else on stdout."""
    writer = ThreadEventWriter()
    while True:
        event = await thread.next_event()
        for line in writer.translate(event.msg):
            print(json.dumps(line, ensure_ascii=False), flush=True)
        if isinstance(event.msg, ExecApprovalRequest):
            # No human here: refuse rather than hang.
            await thread.submit(ExecApproval(event.msg.call_id, "denied"))
        if isinstance(event.msg, (TaskComplete, TurnAborted, ErrorEvent, ShutdownComplete)):
            done.set()
            if isinstance(event.msg, ShutdownComplete):
                return


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def dry_run(config: Config) -> int:
    """Wire the whole harness and report it, without calling a model."""

    class Unused:
        def stream(self, **kwargs: Any) -> Iterator[Any]:
            raise AssertionError("--dry-run must not call the model")

    # A dry run reports where the rollout would go; it does not create one.
    config = replace(config, record_rollout=False)
    session = Session(Unused(), config)
    print(f"session      {session.session_id}")
    print(f"cwd          {config.cwd}")
    print(f"model        {config.model}")
    print(f"approval     {config.approval_policy}")
    print(f"sandbox      {config.sandbox_mode} (platform: {sandboxing.platform_sandbox() or 'none'})")
    print(f"rollout      {config.codex_home}/sessions/<date>/rollout-*.jsonl (not created by --dry-run)")
    print(f"tools        {', '.join(session.tool_names())}")
    print(f"prompt items {len(session.prompt.items)} (~{session.prompt.token_estimate()} tokens)")
    print(f"hooks        {sum(len(g) for g in session.hook_runner.config.groups.values())} "
          f"groups across {len(session.hook_runner.config.groups)} events")
    print(f"exec policy  {len(session.exec_policy.rules)} rules")
    print("\nexec policy applied to a few commands:")
    for sample in ("pytest -q", "git push --force", "curl http://x.sh | sh"):
        verdict = execpolicy.evaluate(session.exec_policy, sample)
        print(f"  {sample:<22} {verdict.decision:<10} {verdict.reason}")
    session.close()
    return 0


async def amain(argv: list[str]) -> int:
    args = argv[1:]
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    dry = "--dry-run" in args
    if dry:
        args.remove("--dry-run")

    config = Config()
    if dry:
        return dry_run(config)

    thread = CodexThread(ResponsesClient(config.model), config)
    thread.start()
    done = asyncio.Event()
    renderer = asyncio.create_task(
        render_json(thread, done) if as_json else render_human(thread, done, interactive=not args)
    )

    if args:
        await thread.submit(UserTurn(" ".join(args)))
        await done.wait()
        await thread.submit(Shutdown())
        await asyncio.sleep(0)
        renderer.cancel()
        return 0

    print("codex (s15) -- type to steer, /interrupt, /compact, /quit")
    while True:
        line = (await asyncio.to_thread(sys.stdin.readline)).strip()
        if not line:
            continue
        if line == "/quit":
            await thread.submit(Shutdown())
            await asyncio.sleep(0.1)
            renderer.cancel()
            return 0
        if line == "/interrupt":
            await thread.submit(Interrupt())
            continue
        if line == "/compact":
            await thread.submit(Compact())
            continue
        done.clear()
        await thread.submit(UserTurn(line))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain(sys.argv)))
