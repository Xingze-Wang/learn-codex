#!/usr/bin/env python3
"""
s11: Compaction

Every chapter so far has grown `history` forever. A long session ends the same
way every time: one more request, and the context window is full.

Codex watches the token count returned with each response, and when the
conversation crosses a fraction of the model's window it compacts *before* the
next request rather than after the failure:

    used 82k / 100k  ->  ask the model to summarize its own work
                     ->  rebuild history as: prefix + recent user turns + summary
                     ->  continue the same turn, no user involvement

What survives compaction is chosen deliberately:

    kept     the session prefix (instructions, environment) -- cheap, and the
             agent is lost without it
    kept     the most recent user messages, newest first until a token budget
             runs out -- the actual request must not be summarized away
    kept     one summary item, marked so a later compaction can see it
    dropped  every tool output, every reasoning item, every intermediate step

The dropped part is where 90% of the tokens are and where almost none of the
value is: a 4000-line `pytest` log matters only through the sentence
"three tests fail in test_auth". The summary is written by the model that just
did the work, so it knows which sentence that is.

Compaction is lossy, and pretending otherwise is how agents silently forget
constraints. That is why the summary prompt asks explicitly for decisions,
constraints, and remaining steps -- not for a description of what happened.

Run:
  python s11_compaction/code.py --explain        # rebuild a history, no API call
  python s11_compaction/code.py "..."            # live, auto-compacts if needed

Real source: codex-rs/core/src/compact.rs (build_compacted_history),
codex-rs/prompts/templates/compact/prompt.md, summary_prefix.md,
codex-rs/core/src/session/context_window.rs

Builds on the s01 kernel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
DEFAULT_CONTEXT_WINDOW = 272_000
AUTO_COMPACT_RATIO = 0.80
COMPACT_USER_MESSAGE_MAX_TOKENS = 2_000
CHARS_PER_TOKEN = 4

BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in a terminal harness.
Your one tool is `exec_command`. Finish with a short plain-text summary.
"""

# codex-rs/prompts/templates/compact/prompt.md, verbatim in spirit.
SUMMARIZATION_PROMPT = """\
You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary \
for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly \
continue the work."""

# codex-rs/prompts/templates/compact/summary_prefix.md
SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary "
    "of its thinking process. You also have access to the state of the tools that "
    "were used by that language model. Use this to build on the work that has "
    "already been done and avoid duplicating work. Here is the summary produced "
    "by the other language model, use the information in this summary to assist "
    "with your own analysis:"
)

EXEC_COMMAND_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {
        "type": "object",
        "properties": {"cmd": {"type": "string"}, "workdir": {"type": "string"}},
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
# Token accounting
# --------------------------------------------------------------------------


def approx_tokens(text: str) -> int:
    """Codex uses the real usage numbers from the API and this estimate only
    where none exist yet (a tool output it is about to send, for instance)."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def item_tokens(item: dict[str, Any]) -> int:
    return approx_tokens(json.dumps(item, ensure_ascii=False))


def history_tokens(history: list[dict[str, Any]]) -> int:
    return sum(item_tokens(item) for item in history)


@dataclass
class TokenStatus:
    used: int
    window: int

    @property
    def remaining(self) -> int:
        return max(0, self.window - self.used)

    @property
    def used_percent(self) -> float:
        return 100.0 * self.used / self.window if self.window else 0.0

    def needs_compaction(self, ratio: float = AUTO_COMPACT_RATIO) -> bool:
        return self.used >= self.window * ratio


# --------------------------------------------------------------------------
# What survives
# --------------------------------------------------------------------------


def is_summary_item(item: dict[str, Any]) -> bool:
    return (
        item.get("type") == "message"
        and item.get("role") == "user"
        and _text_of(item).startswith(SUMMARY_PREFIX)
    )


def _text_of(item: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for part in item.get("content", [])
        if part.get("type") in ("input_text", "output_text", "text")
    )


def collect_user_messages(history: list[dict[str, Any]]) -> list[str]:
    """Real user turns only -- not injected context, not previous summaries."""
    messages = []
    for item in history:
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        text = _text_of(item)
        if not text or is_summary_item(item):
            continue
        if text.startswith("<") and text.rstrip().endswith(">"):
            continue  # injected world-state block (s03), not something the user said
        messages.append(text)
    return messages


def session_prefix(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The injected blocks from the start of the session: cheap and load-bearing."""
    prefix = []
    for item in history:
        if item.get("type") != "message" or item.get("role") != "user":
            break
        text = _text_of(item)
        if not (text.startswith("<") and text.rstrip().endswith(">")):
            break
        prefix.append(item)
    return prefix


def build_compacted_history(
    prefix: list[dict[str, Any]],
    user_messages: list[str],
    summary_text: str,
    *,
    max_tokens: int = COMPACT_USER_MESSAGE_MAX_TOKENS,
) -> list[dict[str, Any]]:
    """prefix + as many recent user messages as fit + the summary.

    Newest-first budgeting is the point: if only one user message fits, it must
    be the one the agent is working on right now, not the first thing ever said.
    """
    selected: list[str] = []
    remaining = max_tokens
    for message in reversed(user_messages):
        if remaining <= 0:
            break
        cost = approx_tokens(message)
        if cost <= remaining:
            selected.append(message)
            remaining -= cost
        else:
            selected.append(message[: remaining * CHARS_PER_TOKEN])
            break
    selected.reverse()

    rebuilt = list(prefix)
    rebuilt.extend(user_item(message) for message in selected)
    rebuilt.append(user_item(f"{SUMMARY_PREFIX}\n{summary_text or '(no summary available)'}"))
    return rebuilt


# --------------------------------------------------------------------------
# The compaction request
# --------------------------------------------------------------------------


def request_summary(client: ModelClient, history: list[dict[str, Any]]) -> str:
    """A separate model call over the same history. No tools: it must summarize,
    not keep working."""
    prompt = [*history, user_item(SUMMARIZATION_PROMPT)]
    parts: list[str] = []
    for event in client.stream(instructions=BASE_INSTRUCTIONS, input_items=prompt, tools=[]):
        if isinstance(event, OutputItemDone) and event.item.get("type") == "message":
            parts.append(_message_text(event.item))
    return "\n".join(part for part in parts if part).strip()


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@dataclass
class CompactionRecord:
    before_tokens: int
    after_tokens: int
    summary: str


class Session:
    def __init__(
        self,
        client: ModelClient,
        *,
        cwd: str | None = None,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        auto_compact_ratio: float = AUTO_COMPACT_RATIO,
    ) -> None:
        self.client = client
        self.cwd = cwd or os.getcwd()
        self.context_window = context_window
        self.auto_compact_ratio = auto_compact_ratio
        self.history: list[dict[str, Any]] = []
        self.compactions: list[CompactionRecord] = []

    def token_status(self) -> TokenStatus:
        return TokenStatus(history_tokens(self.history), self.context_window)

    def compact(self) -> CompactionRecord:
        before = history_tokens(self.history)
        summary = request_summary(self.client, self.history)
        self.history = build_compacted_history(
            session_prefix(self.history),
            collect_user_messages(self.history),
            summary,
        )
        record = CompactionRecord(before, history_tokens(self.history), summary)
        self.compactions.append(record)
        return record

    def run_turn(self, text: str, *, echo: bool = True) -> str:
        self.history.append(user_item(text))
        last_message = ""

        while True:
            # Checked here, before the request, not after the failure.
            if self.token_status().needs_compaction(self.auto_compact_ratio):
                record = self.compact()
                if echo:
                    print(
                        f"\n[auto-compacted: {record.before_tokens} -> {record.after_tokens} tokens]"
                    )

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

            if not calls:
                if echo:
                    print()
                return last_message

            for call in calls:
                self.history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": self._exec(call, echo=echo),
                    }
                )

    def _exec(self, call: dict[str, Any], *, echo: bool) -> str:
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            return f"error: invalid arguments: {exc}"
        cmd = args.get("cmd", "")
        if echo:
            print(f"\n$ {cmd}")
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", cmd],
                cwd=args.get("workdir") or self.cwd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except OSError as exc:
            return f"failed to spawn command: {exc}"
        return f"Process exited with code {proc.returncode}\nOutput:\n{proc.stdout + proc.stderr}"


# --------------------------------------------------------------------------
# Explain mode: the rebuild, with no model involved
# --------------------------------------------------------------------------


def explain() -> int:
    history: list[dict[str, Any]] = [
        user_item("<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>"),
        user_item("port the auth module to the new session API"),
    ]
    for step in range(1, 6):
        history.append(
            {"type": "reasoning", "encrypted_content": "gAAAA" + "x" * 400}
        )
        history.append(
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": f"pytest tests/auth -k case{step}"}),
                "call_id": f"call_{step}",
            }
        )
        history.append(
            {
                "type": "function_call_output",
                "call_id": f"call_{step}",
                "output": "Process exited with code 1\nOutput:\n" + ("FAILED ...\n" * 200),
            }
        )
    history.append(user_item("also keep the old endpoint working"))

    window = 4_000  # a small window so the threshold is visible in the demo
    before = history_tokens(history)
    status = TokenStatus(before, window)
    print(f"history: {len(history)} items, ~{before} tokens")
    print(f"status: {status.used_percent:.0f}% of a {window // 1000}k window "
          f"-> needs compaction: {status.needs_compaction()}\n")

    summary = (
        "Ported auth to the new session API. tests/auth::case3 still fails because "
        "the legacy endpoint returns 302 instead of 200. Next: keep /v1/login working "
        "while /v2/session is the default. Do not change tests outside tests/auth."
    )
    print("summary written by the model (stubbed here; no API call):")
    print(f"  {summary}\n")

    rebuilt = build_compacted_history(
        session_prefix(history), collect_user_messages(history), summary
    )
    after = history_tokens(rebuilt)
    print(f"rebuilt: {len(rebuilt)} items, ~{after} tokens  ({100 * after // before}% of before)")
    for item in rebuilt:
        text = _text_of(item)
        label = "summary" if is_summary_item(item) else item.get("role", item.get("type"))
        print(f"  [{label}] {text[:70]}...")
    return 0


def main(argv: list[str]) -> int:
    if "--explain" in argv or len(argv) == 1:
        return explain()
    window = DEFAULT_CONTEXT_WINDOW
    args = argv[1:]
    if "--window" in args:
        index = args.index("--window")
        window = int(args[index + 1])
        del args[index : index + 2]
    session = Session(ResponsesClient(), context_window=window)
    session.run_turn(" ".join(args))
    status = session.token_status()
    print(f"\n[{status.used} tokens, {status.used_percent:.0f}% of the window, "
          f"{len(session.compactions)} compactions]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
