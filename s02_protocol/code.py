#!/usr/bin/env python3
"""
s02: The Submission / Event protocol

s01 called the loop and waited for a string. That works for a script and
nothing else. Codex instead wraps the loop in two queues:

    caller --Op--> [SQ] --> submission_loop --> turn task --Event--> [EQ] --> caller

The caller never calls the agent. It *submits* an `Op` and *reads* `Event`s.
Every frontend Codex has -- the TUI, `codex exec --json`, the app-server, the
MCP server -- is a different reader of the same two queues.

Three things fall out of that shape, and none of them are possible in s01:

  * **Interrupt.** `Op.Interrupt` arrives on the SQ while the turn task is
    still running, and cancels it. The turn does not have to agree to stop.
  * **Steering.** A user message submitted mid-turn does not start a second
    turn. It lands in the input queue and is drained into history at the next
    step boundary, so the model sees it before its next model call.
  * **Approval.** A tool can block on a decision that arrives later as
    another Op. The turn is a coroutine; the answer is just another submission.

Run:
  python s02_protocol/code.py "list the 3 largest files here"
  python s02_protocol/code.py          # interactive: type while it works, /interrupt, /quit

Real source: codex-rs/protocol/src/protocol.rs (Op, Event, EventMsg),
codex-rs/core/src/session/handlers.rs (submission_loop),
codex-rs/core/src/session/input_queue.rs (steering)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
MAX_OUTPUT_BYTES = 32 * 1024

BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in a terminal harness.
Your one tool is `exec_command`. Work in small steps and keep commands short.
If a new user message appears mid-task, treat it as a correction to follow now.
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
# Op: what a caller may ask the thread to do
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class Interrupt:
    pass


@dataclass(frozen=True)
class Shutdown:
    pass


Op = UserTurn | Interrupt | Shutdown


@dataclass(frozen=True)
class Submission:
    id: str
    op: Op


# --------------------------------------------------------------------------
# Event: what the thread reports back
# --------------------------------------------------------------------------


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
    cwd: str


@dataclass(frozen=True)
class ExecCommandEnd:
    call_id: str
    exit_code: int
    output: str


@dataclass(frozen=True)
class UserMessageQueued:
    text: str


@dataclass(frozen=True)
class TokenCount:
    total: int


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
    | AgentMessage
    | ExecCommandBegin
    | ExecCommandEnd
    | UserMessageQueued
    | TokenCount
    | TaskComplete
    | TurnAborted
    | ErrorEvent
    | ShutdownComplete
)


@dataclass(frozen=True)
class Event:
    id: str  # the submission id this event answers
    msg: EventMsg


# --------------------------------------------------------------------------
# Model client (same wire contract as s01)
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
    """Bridge the blocking SSE iterator onto the event loop.

    The point is not the thread -- it is that every yield is an `await`, so an
    `Op.Interrupt` can cancel the turn task between chunks. codex-rs gets the
    same property from a tokio CancellationToken threaded through the stream.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def produce() -> None:
        try:
            for event in client.stream(**kwargs):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # surfaced on the consumer side
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


# --------------------------------------------------------------------------
# Session: history + input queue + the running turn
# --------------------------------------------------------------------------


@dataclass
class ActiveTurn:
    sub_id: str
    turn_id: str
    task: asyncio.Task[None]


class Session:
    def __init__(self, client: ModelClient, cwd: str | None = None) -> None:
        self.client = client
        self.cwd = cwd or os.getcwd()
        self.history: list[dict[str, Any]] = []
        self.tokens = 0
        self.pending_input: list[str] = []
        self.active: ActiveTurn | None = None
        self.events: asyncio.Queue[Event] = asyncio.Queue()

    def emit(self, sub_id: str, msg: EventMsg) -> None:
        self.events.put_nowait(Event(sub_id, msg))

    def record_user_text(self, text: str) -> None:
        self.history.append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        )

    def drain_pending_input(self) -> list[str]:
        drained, self.pending_input = self.pending_input, []
        return drained

    async def run_turn(self, sub_id: str, first_text: str) -> None:
        turn_id = uuid.uuid4().hex[:12]
        self.emit(sub_id, TaskStarted(turn_id))
        self.record_user_text(first_text)
        last_message = ""

        try:
            while True:
                # Step boundary: anything the user typed while the model was
                # thinking gets folded into history *before* the next request.
                for queued in self.drain_pending_input():
                    self.record_user_text(queued)

                calls: list[dict[str, Any]] = []
                async for event in _astream(
                    self.client,
                    instructions=BASE_INSTRUCTIONS,
                    input_items=list(self.history),  # snapshot: codex clones history per request
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
                            self.emit(sub_id, AgentMessage(last_message))
                    elif isinstance(event, Completed):
                        self.tokens += event.input_tokens + event.output_tokens
                        self.emit(sub_id, TokenCount(self.tokens))

                if not calls:
                    if self.pending_input:
                        # The model stopped, but the user has already said
                        # something else. Keep the same turn going.
                        continue
                    self.emit(sub_id, TaskComplete(last_message))
                    return

                for call in calls:
                    output = await self._dispatch(sub_id, call)
                    self.history.append(
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": output,
                        }
                    )
        except asyncio.CancelledError:
            self.history.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "[turn interrupted by user]"}
                    ],
                }
            )
            self.emit(sub_id, TurnAborted("interrupted"))
            raise
        except Exception as exc:  # keep the thread alive; report and continue
            self.emit(sub_id, ErrorEvent(str(exc)))

    async def _dispatch(self, sub_id: str, call: dict[str, Any]) -> str:
        if call.get("name") != "exec_command":
            return f"unsupported tool: {call.get('name')}"
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            return f"invalid arguments: {exc}"

        cmd = args.get("cmd", "")
        cwd = args.get("workdir") or self.cwd
        call_id = call.get("call_id", "")
        self.emit(sub_id, ExecCommandBegin(call_id, cmd, cwd))
        exit_code, output = await asyncio.to_thread(_run_command, cmd, cwd)
        self.emit(sub_id, ExecCommandEnd(call_id, exit_code, output))
        return f"Process exited with code {exit_code}\nOutput:\n{output}"


def _run_command(cmd: str, cwd: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    except OSError as exc:
        return 1, f"failed to spawn command: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr)[:MAX_OUTPUT_BYTES]


def _message_text(item: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for part in item.get("content", [])
        if part.get("type") in ("output_text", "text")
    )


# --------------------------------------------------------------------------
# CodexThread: the two queues, and the loop that drains the first one
# --------------------------------------------------------------------------


class CodexThread:
    def __init__(self, client: ModelClient, cwd: str | None = None) -> None:
        self.session = Session(client, cwd)
        self.submissions: asyncio.Queue[Submission] = asyncio.Queue()
        self._loop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._submission_loop())

    async def submit(self, op: Op) -> str:
        sub_id = uuid.uuid4().hex[:8]
        await self.submissions.put(Submission(sub_id, op))
        return sub_id

    async def next_event(self) -> Event:
        return await self.session.events.get()

    async def _submission_loop(self) -> None:
        """One consumer, one op at a time. Never blocks on a running turn."""
        sess = self.session
        while True:
            sub = await self.submissions.get()
            op = sub.op

            if isinstance(op, UserTurn):
                if sess.active is not None and not sess.active.task.done():
                    # A turn is already running: steer it, do not start a
                    # second one. This is the whole reason for the queue.
                    sess.pending_input.append(op.text)
                    sess.emit(sub.id, UserMessageQueued(op.text))
                    continue
                task = asyncio.create_task(sess.run_turn(sub.id, op.text))
                sess.active = ActiveTurn(sub.id, uuid.uuid4().hex[:12], task)

            elif isinstance(op, Interrupt):
                active = sess.active
                if active and not active.task.done():
                    active.task.cancel()
                else:
                    sess.emit(sub.id, TurnAborted("no active turn"))

            elif isinstance(op, Shutdown):
                active = sess.active
                if active and not active.task.done():
                    active.task.cancel()
                sess.emit(sub.id, ShutdownComplete())
                return


# --------------------------------------------------------------------------
# A frontend: read stdin, submit Ops, render Events. Nothing more.
# --------------------------------------------------------------------------


async def _render(thread: CodexThread, done: asyncio.Event) -> None:
    while True:
        event = await thread.next_event()
        msg = event.msg
        if isinstance(msg, AgentMessageDelta):
            print(msg.delta, end="", flush=True)
        elif isinstance(msg, ExecCommandBegin):
            print(f"\n$ {msg.command}")
        elif isinstance(msg, ExecCommandEnd):
            head = msg.output.strip().splitlines()[:3]
            for line in head:
                print(f"  {line}")
            print(f"  (exit {msg.exit_code})")
        elif isinstance(msg, UserMessageQueued):
            print(f"\n[queued for the running turn: {msg.text}]")
        elif isinstance(msg, TaskComplete):
            print(f"\n[turn complete · {thread.session.tokens} tokens]")
            done.set()
        elif isinstance(msg, TurnAborted):
            print(f"\n[aborted: {msg.reason}]")
            done.set()
        elif isinstance(msg, ErrorEvent):
            print(f"\n[error: {msg.message}]")
            done.set()
        elif isinstance(msg, ShutdownComplete):
            done.set()
            return


async def amain(argv: list[str]) -> int:
    thread = CodexThread(ResponsesClient())
    thread.start()
    done = asyncio.Event()
    renderer = asyncio.create_task(_render(thread, done))

    if len(argv) > 1:
        await thread.submit(UserTurn(" ".join(argv[1:])))
        await done.wait()
        renderer.cancel()
        return 0

    print("codex (s02) -- type any time; /interrupt cancels; /quit exits")
    while True:
        line = (await asyncio.to_thread(sys.stdin.readline)).strip()
        if not line:
            continue
        if line == "/quit":
            await thread.submit(Shutdown())
            renderer.cancel()
            return 0
        if line == "/interrupt":
            await thread.submit(Interrupt())
            continue
        done.clear()
        await thread.submit(UserTurn(line))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain(sys.argv)))
