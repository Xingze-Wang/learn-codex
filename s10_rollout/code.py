#!/usr/bin/env python3
"""
s10: Rollout -- the session as an append-only file

Nothing so far survives the process exiting. Codex writes every session to a
JSONL file as it happens:

    ~/.codex/sessions/2026/05/23/rollout-2026-05-23T18-18-36-<thread-id>.jsonl

One line per event, appended, never rewritten. Four line types, and the split
between them is the interesting part:

    session_meta    once, first line: id, cwd, cli version, instructions
    turn_context    once per turn: cwd, approval policy, sandbox policy, model
    response_item   what goes back to the model on resume -- messages,
                    reasoning, function_call, function_call_output
    event_msg       what the *user* saw -- exec begin/end, token counts,
                    task_started, task_complete

`response_item` lines rebuild the model's view. `event_msg` lines rebuild the
human's view. Rendering a resumed session needs both, and replaying it to the
model needs only the first -- which is why they are separate types rather than
one stream with a flag.

Two operations fall out of an append-only log for free:

    resume   replay response_items -> the model continues as if nothing happened
    fork     copy the first N turns into a new file -> two futures, one past

A live `Compaction` item (s11) is also persisted, so a resumed session
inherits the summary rather than re-reading a history that was already
discarded.

Run:
  python s10_rollout/code.py --demo               # record, list, resume, fork
  python s10_rollout/code.py --list [DIR]         # list real sessions
  python s10_rollout/code.py --show FILE

Real source: codex-rs/rollout/src/recorder.rs, policy.rs, list.rs,
codex-rs/core/src/session/rollout_reconstruction.rs
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SESSIONS_SUBDIR = "sessions"

SESSION_META = "session_meta"
TURN_CONTEXT = "turn_context"
RESPONSE_ITEM = "response_item"
EVENT_MSG = "event_msg"

# Only items the model needs on replay. Anything else is presentation.
PERSISTED_RESPONSE_ITEMS = {
    "message",
    "reasoning",
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "web_search_call",
    "compaction",
}

# Events worth keeping for rendering a resumed session. Deltas are not: a
# stream of 4000 token fragments is noise once the message exists.
PERSISTED_EVENTS = {
    "task_started",
    "task_complete",
    "user_message",
    "agent_message",
    "exec_command_begin",
    "exec_command_end",
    "token_count",
    "turn_aborted",
    "error",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def should_persist(line_type: str, payload: dict[str, Any]) -> bool:
    if line_type == RESPONSE_ITEM:
        return payload.get("type") in PERSISTED_RESPONSE_ITEMS
    if line_type == EVENT_MSG:
        return payload.get("type") in PERSISTED_EVENTS
    return line_type in (SESSION_META, TURN_CONTEXT)


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------


@dataclass
class RolloutRecorder:
    path: Path
    thread_id: str

    @classmethod
    def create(
        cls,
        codex_home: str | Path,
        *,
        cwd: str,
        model: str,
        instructions: str = "",
        thread_id: str | None = None,
        timestamp: dt.datetime | None = None,
    ) -> RolloutRecorder:
        thread_id = thread_id or str(uuid.uuid4())
        stamp = timestamp or dt.datetime.now()
        # The date path is what makes `codex resume` able to list by day
        # without opening every file on disk.
        directory = Path(codex_home) / SESSIONS_SUBDIR / stamp.strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        name = f"rollout-{stamp.strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
        recorder = cls(directory / name, thread_id)
        recorder._append(
            SESSION_META,
            {
                "id": thread_id,
                "timestamp": _now(),
                "cwd": cwd,
                "originator": "learn-codex",
                "cli_version": "0.0.1",
                "source": "cli",
                "model": model,
                "instructions": instructions,
            },
        )
        return recorder

    def record_turn_context(self, **context: Any) -> None:
        self._append(TURN_CONTEXT, {"timestamp": _now(), **context})

    def record_response_item(self, item: dict[str, Any]) -> None:
        self._append(RESPONSE_ITEM, item)

    def record_event(self, event_type: str, **payload: Any) -> None:
        self._append(EVENT_MSG, {"type": event_type, **payload})

    def _append(self, line_type: str, payload: dict[str, Any]) -> None:
        if not should_persist(line_type, payload):
            return
        line = json.dumps(
            {"timestamp": _now(), "type": line_type, "payload": payload},
            ensure_ascii=False,
        )
        # Append and flush per line: a crash mid-turn must not lose the turn.
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@dataclass
class Rollout:
    path: Path
    meta: dict[str, Any]
    lines: list[dict[str, Any]] = field(default_factory=list)

    @property
    def thread_id(self) -> str:
        return self.meta.get("id", "")

    def response_items(self) -> list[dict[str, Any]]:
        """Exactly what gets replayed into the next request."""
        return [line["payload"] for line in self.lines if line["type"] == RESPONSE_ITEM]

    def events(self) -> list[dict[str, Any]]:
        return [line["payload"] for line in self.lines if line["type"] == EVENT_MSG]

    def turn_count(self) -> int:
        return sum(
            1
            for line in self.lines
            if line["type"] == EVENT_MSG and line["payload"].get("type") == "task_started"
        )

    def first_user_message(self) -> str:
        for line in self.lines:
            if line["type"] == EVENT_MSG and line["payload"].get("type") == "user_message":
                return line["payload"].get("message", "")
        return ""


def read_rollout(path: str | Path) -> Rollout:
    path = Path(path)
    lines: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                # A crash can leave a half-written last line. Everything before
                # it is still a valid session; refuse to throw it away.
                continue
            if line.get("type") == SESSION_META and not meta:
                meta = line.get("payload", {})
            lines.append(line)
    return Rollout(path, meta, lines)


def list_rollouts(codex_home: str | Path) -> list[Path]:
    root = Path(codex_home) / SESSIONS_SUBDIR
    if not root.is_dir():
        return []
    return sorted(root.rglob("rollout-*.jsonl"), reverse=True)


def head_summary(path: str | Path, max_lines: int = 40) -> dict[str, Any]:
    """Enough to render one row of a session picker, without reading the file.

    A long session is megabytes; a picker showing 50 of them must not read
    50 megabytes to draw a list.
    """
    meta: dict[str, Any] = {}
    preview = ""
    with Path(path).open(encoding="utf-8") as handle:
        for index, raw in enumerate(handle):
            if index >= max_lines:
                break
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            payload = line.get("payload", {})
            if line.get("type") == SESSION_META:
                meta = payload
            elif line.get("type") == EVENT_MSG and payload.get("type") == "user_message":
                preview = payload.get("message", "")
                break
    return {"path": str(path), "id": meta.get("id", ""), "cwd": meta.get("cwd", ""), "preview": preview}


# --------------------------------------------------------------------------
# Resume and fork
# --------------------------------------------------------------------------


def resume(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rebuild (history, session meta). The model sees what it saw before."""
    rollout = read_rollout(path)
    return rollout.response_items(), rollout.meta


def fork(path: str | Path, codex_home: str | Path, *, keep_turns: int) -> Path:
    """Copy the first `keep_turns` turns into a new thread.

    The original file is never edited. Rewriting history in place would mean a
    crash during the rewrite loses both futures.
    """
    rollout = read_rollout(path)
    new_id = str(uuid.uuid4())
    recorder = RolloutRecorder.create(
        codex_home,
        cwd=rollout.meta.get("cwd", os.getcwd()),
        model=rollout.meta.get("model", ""),
        instructions=rollout.meta.get("instructions", ""),
        thread_id=new_id,
    )

    turns_seen = 0
    for line in rollout.lines:
        payload = line["payload"]
        if line["type"] == SESSION_META:
            continue
        if line["type"] == EVENT_MSG and payload.get("type") == "task_started":
            turns_seen += 1
            if turns_seen > keep_turns:
                break
        recorder._append(line["type"], payload)
    return recorder.path


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------


def demo() -> int:
    home = Path(tempfile.mkdtemp(prefix="learn-codex-s10-"))
    recorder = RolloutRecorder.create(home, cwd="/repo", model="gpt-5.5")
    print(f"recording to {recorder.path}\n")

    for turn, (question, command, answer) in enumerate(
        [
            ("how many tests are there?", "ls tests | wc -l", "There are 12 test files."),
            ("run them", "pytest -q", "All 12 pass."),
        ],
        start=1,
    ):
        recorder.record_turn_context(
            turn=turn, cwd="/repo", approval_policy="on-request", sandbox_policy="workspace-write"
        )
        recorder.record_event("task_started", turn_id=f"t{turn}")
        recorder.record_event("user_message", message=question)
        recorder.record_response_item(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": question}]}
        )
        recorder.record_response_item(
            {"type": "function_call", "name": "exec_command",
             "arguments": json.dumps({"cmd": command}), "call_id": f"call_{turn}"}
        )
        recorder.record_event("exec_command_begin", call_id=f"call_{turn}", command=command)
        recorder.record_event("exec_command_end", call_id=f"call_{turn}", exit_code=0)
        recorder.record_response_item(
            {"type": "function_call_output", "call_id": f"call_{turn}", "output": "..."}
        )
        recorder.record_response_item(
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]}
        )
        recorder.record_event("agent_message", message=answer)
        recorder.record_event("token_count", total=1200 * turn)
        recorder.record_event("task_complete", turn_id=f"t{turn}")
        # Deltas are emitted constantly and never persisted:
        recorder.record_event("agent_message_delta", delta="All")

    print("--- file ---")
    for line in recorder.path.read_text().splitlines():
        parsed = json.loads(line)
        print(f'{parsed["type"]:<14} {json.dumps(parsed["payload"])[:88]}')

    rollout = read_rollout(recorder.path)
    print(f"\nturns: {rollout.turn_count()}")
    print(f"replayable items: {len(rollout.response_items())}")
    print(f"renderable events: {len(rollout.events())}  (no deltas: dropped by policy)")

    history, meta = resume(recorder.path)
    print(f"\nresume -> {len(history)} items, cwd {meta['cwd']}")

    forked = fork(recorder.path, home, keep_turns=1)
    print(f"fork(keep_turns=1) -> {forked.name}")
    print(f"  forked turns: {read_rollout(forked).turn_count()}")
    print(f"  original untouched: {read_rollout(recorder.path).turn_count()} turns")

    print("\nsessions on disk:")
    for path in list_rollouts(home):
        summary = head_summary(path)
        print(f"  {summary['id'][:8]}  {summary['cwd']:<8} {summary['preview']}")
    return 0


def main(argv: list[str]) -> int:
    if "--list" in argv:
        index = argv.index("--list")
        home = argv[index + 1] if len(argv) > index + 1 else os.path.expanduser("~/.codex")
        for path in list_rollouts(home)[:20]:
            summary = head_summary(path)
            print(f"{summary['id'][:8]}  {summary['cwd']:<40} {summary['preview'][:60]}")
        return 0
    if "--show" in argv:
        rollout = read_rollout(argv[argv.index("--show") + 1])
        print(f"thread {rollout.thread_id}  cwd {rollout.meta.get('cwd')}")
        print(f"turns {rollout.turn_count()}  items {len(rollout.response_items())}")
        for item in rollout.response_items():
            print(f"  {item.get('type')}")
        return 0
    return demo()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
