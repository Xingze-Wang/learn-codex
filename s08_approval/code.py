#!/usr/bin/env python3
"""
s08: Approval and escalation

s07 gave the harness a sandbox. This chapter is what happens when the sandbox
says no.

Codex does not ask the user before running a command. It runs the command in
the sandbox first, and only asks when the sandbox blocks it:

    1. assess     -- is this auto-approvable, does it need a human, or is it refused?
    2. run        -- inside the sandbox
    3. denied?    -- the heuristic from s07 fires
    4. ask        -- emit ExecApprovalRequest, and *await* an Op that has not arrived yet
    5. retry      -- on approval, run again with the sandbox off
    6. remember   -- so the same command is never asked about twice

Step 4 is the reason s02 exists. The turn is a coroutine parked on a future;
the answer arrives later as another submission on the same queue. Nothing
blocks, and `Op.Interrupt` still works while the question is on screen.

The approval policy decides which of these steps may happen at all:

    untrusted     ask before anything that is not on the trusted list
    on-request    run sandboxed; ask only when the sandbox blocks it   (default)
    never         never ask; a blocked command just fails, and the model
                  is told so and works around it

`never` is not a weaker mode -- it is the mode for CI, where there is nobody to
ask. The failure text goes to the model, which is exactly what should happen
when no human is present.

Run:
  python s08_approval/code.py "write a file into my home directory"
  python s08_approval/code.py --policy never "..."

Real source: codex-rs/core/src/safety.rs (SafetyCheck), codex-rs/core/src/tools/approvals.rs,
codex-rs/core/src/tools/sandboxing.rs, codex-rs/protocol/src/protocol.rs (ReviewDecision)

Builds on the s02 kernel.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shlex
import subprocess
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"

BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in a terminal harness.
Commands run inside a sandbox that allows writes only under the workspace and
blocks the network. If a command is refused, say so plainly and continue with
what you can do. Finish with a short plain-text summary.
"""

EXEC_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "workdir": {"type": "string"},
            "justification": {
                "type": "string",
                "description": "Why this command needs to run, shown to the user if approval is required.",
            },
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
}

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
# Policies and decisions
# --------------------------------------------------------------------------

UNLESS_TRUSTED = "untrusted"
ON_REQUEST = "on-request"
NEVER = "never"

APPROVED = "approved"
APPROVED_FOR_SESSION = "approved_for_session"
DENIED = "denied"
ABORT = "abort"

# Commands that read and nothing else. Codex keeps this in an execpolicy rule
# file (s09); a hard-coded set is enough to show what it is for.
TRUSTED_PREFIXES = {
    ("ls",), ("cat",), ("head",), ("tail",), ("pwd",), ("wc",), ("file",),
    ("grep",), ("rg",), ("find",), ("git", "status"), ("git", "diff"),
    ("git", "log"), ("git", "show"),
}


@dataclass(frozen=True)
class AutoApprove:
    sandboxed: bool = True


@dataclass(frozen=True)
class AskUser:
    reason: str


@dataclass(frozen=True)
class Reject:
    reason: str


SafetyCheck = AutoApprove | AskUser | Reject


def is_trusted(cmd: str) -> bool:
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    return any(tuple(tokens[: len(prefix)]) == prefix for prefix in TRUSTED_PREFIXES)


def assess_command_safety(
    cmd: str,
    *,
    approval_policy: str,
    sandbox_available: bool,
    approved: set[str],
) -> SafetyCheck:
    """Runs *before* the command. The sandbox is what makes 'just run it' safe."""
    if cmd in approved:
        # Already blessed this session: run it unsandboxed without asking again.
        return AutoApprove(sandboxed=False)

    if approval_policy == UNLESS_TRUSTED and not is_trusted(cmd):
        return AskUser("approval policy is `untrusted`")

    if sandbox_available:
        return AutoApprove(sandboxed=True)

    # No sandbox on this platform: running is a real risk, so the policy has
    # to decide instead of the kernel.
    if approval_policy == NEVER:
        return Reject("no sandbox available and approval policy is `never`")
    return AskUser("no sandbox available on this platform")


# --------------------------------------------------------------------------
# Sandboxed execution (s07, condensed)
# --------------------------------------------------------------------------

SEATBELT_POLICY = """\
(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow file-read*)
(allow file-write-data (require-all (path "/dev/null") (vnode-type CHARACTER-DEVICE)))
(allow file-write* (subpath (param "WRITABLE_ROOT_0")))
"""

DENIAL_MARKERS = ("operation not permitted", "permission denied", "read-only file system")


def sandbox_available() -> bool:
    return platform.system() == "Darwin" and os.path.exists(SANDBOX_EXEC)


@dataclass
class ExecOutput:
    exit_code: int
    output: str
    sandboxed: bool


def run_command(cmd: str, cwd: str, *, sandboxed: bool) -> ExecOutput:
    argv = ["/bin/bash", "-lc", cmd]
    if sandboxed and sandbox_available():
        argv = [
            SANDBOX_EXEC,
            "-p",
            SEATBELT_POLICY,
            f"-DWRITABLE_ROOT_0={os.path.realpath(cwd)}",
            "--",
            *argv,
        ]
    else:
        sandboxed = False
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ExecOutput(124, "command timed out", sandboxed)
    return ExecOutput(proc.returncode, proc.stdout + proc.stderr, sandboxed)


def is_likely_sandbox_denied(result: ExecOutput) -> bool:
    if not result.sandboxed or result.exit_code == 0:
        return False
    lowered = result.output.lower()
    return any(marker in lowered for marker in DENIAL_MARKERS)


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class ExecApproval:
    call_id: str
    decision: str


@dataclass(frozen=True)
class Interrupt:
    pass


@dataclass(frozen=True)
class Shutdown:
    pass


Op = UserTurn | ExecApproval | Interrupt | Shutdown


@dataclass(frozen=True)
class Submission:
    id: str
    op: Op


@dataclass(frozen=True)
class TaskStarted:
    turn_id: str


@dataclass(frozen=True)
class AgentMessageDelta:
    delta: str


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
    cwd: str
    reason: str
    justification: str | None = None


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


EventMsg = (
    TaskStarted
    | AgentMessageDelta
    | ExecCommandBegin
    | ExecCommandEnd
    | ExecApprovalRequest
    | TaskComplete
    | TurnAborted
    | ErrorEvent
    | ShutdownComplete
)


@dataclass(frozen=True)
class Event:
    id: str
    msg: EventMsg


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class Session:
    def __init__(
        self,
        client: ModelClient,
        *,
        cwd: str | None = None,
        approval_policy: str = ON_REQUEST,
    ) -> None:
        self.client = client
        self.cwd = cwd or os.getcwd()
        self.approval_policy = approval_policy
        self.history: list[dict[str, Any]] = []
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.approved: set[str] = set()
        # call_id -> future that some later Op will complete
        self.pending_approvals: dict[str, asyncio.Future[str]] = {}
        self.active: asyncio.Task[None] | None = None

    def emit(self, sub_id: str, msg: EventMsg) -> None:
        self.events.put_nowait(Event(sub_id, msg))

    async def run_turn(self, sub_id: str, text: str) -> None:
        self.emit(sub_id, TaskStarted(uuid.uuid4().hex[:12]))
        self.history.append(user_item(text))
        last_message = ""

        try:
            while True:
                calls: list[dict[str, Any]] = []
                async for event in _astream(
                    self.client,
                    instructions=BASE_INSTRUCTIONS,
                    input_items=list(self.history),
                    tools=[EXEC_COMMAND_TOOL],
                ):
                    if isinstance(event, OutputTextDelta):
                        self.emit(sub_id, AgentMessageDelta(event.delta))
                    elif isinstance(event, OutputItemDone):
                        self.history.append(event.item)
                        if event.item.get("type") == "function_call":
                            calls.append(event.item)
                        elif event.item.get("type") == "message":
                            last_message = _message_text(event.item)

                if not calls:
                    self.emit(sub_id, TaskComplete(last_message))
                    return

                for call in calls:
                    self.history.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": await self._exec_with_approval(sub_id, call),
                        }
                    )
        except asyncio.CancelledError:
            self.emit(sub_id, TurnAborted("interrupted"))
            raise
        except Exception as exc:
            self.emit(sub_id, ErrorEvent(f"{type(exc).__name__}: {exc}"))

    async def _exec_with_approval(self, sub_id: str, call: dict[str, Any]) -> str:
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            return f"error: invalid arguments: {exc}"

        cmd = args.get("cmd", "")
        cwd = args.get("workdir") or self.cwd
        call_id = call.get("call_id", "")
        justification = args.get("justification")

        # --- 1. assess -------------------------------------------------
        check = assess_command_safety(
            cmd,
            approval_policy=self.approval_policy,
            sandbox_available=sandbox_available(),
            approved=self.approved,
        )
        if isinstance(check, Reject):
            return f"command not run: {check.reason}"
        if isinstance(check, AskUser):
            decision = await self._ask(sub_id, call_id, cmd, cwd, check.reason, justification)
            if decision in (DENIED, ABORT):
                return "command not run: the user declined it"
            if decision == APPROVED_FOR_SESSION:
                self.approved.add(cmd)
            return self._finish(sub_id, call_id, run_command(cmd, cwd, sandboxed=False))

        # --- 2. run in the sandbox -------------------------------------
        self.emit(sub_id, ExecCommandBegin(call_id, cmd, check.sandboxed))
        result = await asyncio.to_thread(run_command, cmd, cwd, sandboxed=check.sandboxed)

        # --- 3/4. denied? ask, unless the policy forbids asking ---------
        if is_likely_sandbox_denied(result):
            if self.approval_policy == NEVER:
                self.emit(sub_id, ExecCommandEnd(call_id, result.exit_code, result.output))
                return (
                    f"Process exited with code {result.exit_code}\n"
                    "The sandbox blocked this command and approvals are disabled.\n"
                    f"Output:\n{result.output}"
                )
            decision = await self._ask(
                sub_id, call_id, cmd, cwd, "the sandbox blocked this command", justification
            )
            if decision in (DENIED, ABORT):
                return "command not run: the user declined the escalation"
            # --- 5/6. retry outside the sandbox, and remember ----------
            if decision == APPROVED_FOR_SESSION:
                self.approved.add(cmd)
            retried = await asyncio.to_thread(run_command, cmd, cwd, sandboxed=False)
            return self._finish(sub_id, call_id, retried)

        return self._finish(sub_id, call_id, result)

    def _finish(self, sub_id: str, call_id: str, result: ExecOutput) -> str:
        self.emit(sub_id, ExecCommandEnd(call_id, result.exit_code, result.output))
        return f"Process exited with code {result.exit_code}\nOutput:\n{result.output}"

    async def _ask(
        self,
        sub_id: str,
        call_id: str,
        cmd: str,
        cwd: str,
        reason: str,
        justification: str | None,
    ) -> str:
        """Park the turn on a future. The answer arrives as another Op."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending_approvals[call_id] = future
        self.emit(sub_id, ExecApprovalRequest(call_id, cmd, cwd, reason, justification))
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


class CodexThread:
    def __init__(self, client: ModelClient, **kwargs: Any) -> None:
        self.session = Session(client, **kwargs)
        self.submissions: asyncio.Queue[Submission] = asyncio.Queue()

    def start(self) -> None:
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
                sess.active = asyncio.create_task(sess.run_turn(sub.id, op.text))
            elif isinstance(op, ExecApproval):
                # This is the whole trick: an answer to a question the turn is
                # still waiting on, delivered through the same queue as everything else.
                if not sess.resolve_approval(op.call_id, op.decision):
                    sess.emit(sub.id, ErrorEvent(f"no pending approval for {op.call_id}"))
            elif isinstance(op, Interrupt):
                if sess.active and not sess.active.done():
                    sess.active.cancel()
            elif isinstance(op, Shutdown):
                if sess.active and not sess.active.done():
                    sess.active.cancel()
                sess.emit(sub.id, ShutdownComplete())
                return


# --------------------------------------------------------------------------
# A frontend that can answer questions
# --------------------------------------------------------------------------


async def run_cli(thread: CodexThread, prompt: str) -> int:
    await thread.submit(UserTurn(prompt))
    while True:
        event = await thread.next_event()
        msg = event.msg
        if isinstance(msg, AgentMessageDelta):
            print(msg.delta, end="", flush=True)
        elif isinstance(msg, ExecCommandBegin):
            where = "sandboxed" if msg.sandboxed else "unsandboxed"
            print(f"\n$ {msg.command}   [{where}]")
        elif isinstance(msg, ExecCommandEnd):
            print(f"  (exit {msg.exit_code})")
        elif isinstance(msg, ExecApprovalRequest):
            print(f"\n! {msg.reason}")
            print(f"  command: {msg.command}")
            if msg.justification:
                print(f"  because: {msg.justification}")
            answer = (await asyncio.to_thread(input, "  allow? [y/N/always] ")).strip().lower()
            decision = {
                "y": APPROVED,
                "yes": APPROVED,
                "always": APPROVED_FOR_SESSION,
            }.get(answer, DENIED)
            await thread.submit(ExecApproval(msg.call_id, decision))
        elif isinstance(msg, TaskComplete):
            print()
            return 0
        elif isinstance(msg, (TurnAborted, ErrorEvent)):
            print(f"\n[{msg}]")
            return 1


async def amain(argv: list[str]) -> int:
    args = argv[1:]
    policy = ON_REQUEST
    if "--policy" in args:
        i = args.index("--policy")
        policy = args[i + 1]
        del args[i : i + 2]
    if not args:
        print(__doc__)
        return 0

    thread = CodexThread(ResponsesClient(), approval_policy=policy)
    thread.start()
    return await run_cli(thread, " ".join(args))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain(sys.argv)))
