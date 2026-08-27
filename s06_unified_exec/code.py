#!/usr/bin/env python3
"""
s06: Unified exec -- the shell that outlives the tool call

s01 ran `/bin/bash -lc CMD` and waited for it to exit. That model breaks on
everything interesting:

    cd build && make     -> the cd is gone by the next call
    python3              -> a REPL that never exits, so the call never returns
    npm run dev          -> a server that must keep running while work continues
    ssh host             -> a prompt that wants an answer

Codex's `exec_command` opens a PTY-backed *session*. If the command finishes
inside `yield_time_ms`, the tool returns its output and the session ends. If it
does not, the tool returns early with a session id, and the model keeps talking
to the live process with `write_stdin`. The process outlives the call.

The response header is the whole protocol:

    Chunk ID: 8f21ac
    Wall time: 0.0031 seconds
    Process exited with code 0            <- finished
    Output:
    ...

    Chunk ID: 44b0e1
    Wall time: 1.0007 seconds
    Process running with session ID 3     <- still alive, talk to it
    Output:
    >>>

Output is capped on a token budget, head and tail. An unbounded `make` log
would otherwise eat the context window that the rest of the task needs.

Run:
  python s06_unified_exec/code.py --demo
  python s06_unified_exec/code.py --repl     # drive a live python session by hand

Real source: codex-rs/core/src/unified_exec/ (mod.rs, process.rs, process_manager.rs,
head_tail_buffer.rs), codex-rs/core/src/tools/context.rs (response_header)
"""

from __future__ import annotations

import errno
import os
import pty
import selectors
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field

DEFAULT_YIELD_MS = 10_000
MIN_YIELD_MS = 250
MAX_YIELD_MS = 30_000
DEFAULT_MAX_OUTPUT_TOKENS = 10_000
CHARS_PER_TOKEN = 4
IDLE_QUIET_MS = 120  # stop waiting once output has gone quiet this long


class ExecError(Exception):
    """Reported to the model as tool output."""


# --------------------------------------------------------------------------
# Head/tail buffer: keep the beginning and the end, drop the middle
# --------------------------------------------------------------------------


class HeadTailBuffer:
    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.head: list[str] = []
        self.tail: list[str] = []
        self.head_len = 0
        self.tail_len = 0
        self.dropped = 0

    def append(self, text: str) -> None:
        half = self.max_chars // 2
        if self.head_len < half:
            take = min(len(text), half - self.head_len)
            self.head.append(text[:take])
            self.head_len += take
            text = text[take:]
            if not text:
                return
        self.tail.append(text)
        self.tail_len += len(text)
        while self.tail_len > half and self.tail:
            oldest = self.tail.pop(0)
            self.tail_len -= len(oldest)
            self.dropped += len(oldest)

    def render(self) -> str:
        head = "".join(self.head)
        tail = "".join(self.tail)
        if not self.dropped:
            return head + tail
        return f"{head}\n[... {self.dropped} characters truncated ...]\n{tail}"


# --------------------------------------------------------------------------
# One PTY-backed session
# --------------------------------------------------------------------------


@dataclass
class ExecSession:
    session_id: str
    process: subprocess.Popen[bytes]
    master_fd: int
    command: str
    cwd: str
    buffer: HeadTailBuffer
    closed: bool = False

    def read_available(self, deadline: float) -> None:
        """Drain the PTY until the process exits, output goes quiet, or time is up."""
        selector = selectors.DefaultSelector()
        selector.register(self.master_fd, selectors.EVENT_READ)
        last_output = time.monotonic()
        try:
            while time.monotonic() < deadline:
                if not selector.select(timeout=0.05):
                    if self.process.poll() is not None:
                        break
                    if time.monotonic() - last_output > IDLE_QUIET_MS / 1000:
                        break
                    continue
                try:
                    chunk = os.read(self.master_fd, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break  # the child closed the pty: it is gone
                    raise
                if not chunk:
                    break
                self.buffer.append(chunk.decode("utf-8", "replace"))
                last_output = time.monotonic()
        finally:
            selector.close()

    def write(self, text: str) -> None:
        os.write(self.master_fd, text.encode())

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Result rendering -- the header is what the model actually reads
# --------------------------------------------------------------------------


@dataclass
class ExecResult:
    output: str
    wall_time: float
    exit_code: int | None = None
    session_id: str | None = None
    original_tokens: int | None = None
    chunk_id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])

    def render(self) -> str:
        sections = [f"Chunk ID: {self.chunk_id}", f"Wall time: {self.wall_time:.4f} seconds"]
        if self.exit_code is not None:
            sections.append(f"Process exited with code {self.exit_code}")
        if self.session_id is not None:
            sections.append(f"Process running with session ID {self.session_id}")
        if self.original_tokens is not None:
            sections.append(f"Original token count: {self.original_tokens}")
        sections.append("Output:")
        return "\n".join(sections) + "\n" + self.output


# --------------------------------------------------------------------------
# The manager: open sessions, reuse them, clean them up
# --------------------------------------------------------------------------


class ProcessManager:
    def __init__(self, shell: str = "/bin/bash") -> None:
        self.shell = shell
        self.sessions: dict[str, ExecSession] = {}
        self._next_id = 1

    def exec_command(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        yield_time_ms: int = DEFAULT_YIELD_MS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> ExecResult:
        yield_time_ms = max(MIN_YIELD_MS, min(MAX_YIELD_MS, yield_time_ms))
        started = time.monotonic()

        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                [self.shell, "-lc", cmd],
                cwd=cwd or os.getcwd(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,  # its own process group, so we can kill the tree
                close_fds=True,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise ExecError(f"failed to spawn command: {exc}") from None
        os.close(slave_fd)

        session_id = str(self._next_id)
        self._next_id += 1
        session = ExecSession(
            session_id=session_id,
            process=process,
            master_fd=master_fd,
            command=cmd,
            cwd=cwd or os.getcwd(),
            buffer=HeadTailBuffer(max_output_tokens * CHARS_PER_TOKEN),
        )
        self.sessions[session_id] = session

        session.read_available(started + yield_time_ms / 1000)
        return self._result(session, started)

    def write_stdin(
        self,
        session_id: str,
        chars: str,
        *,
        yield_time_ms: int = 1_000,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> ExecResult:
        session = self.sessions.get(session_id)
        if session is None or session.closed:
            raise ExecError(f"no live session with id {session_id}")

        started = time.monotonic()
        # Each call reports only what arrived since the last one.
        session.buffer = HeadTailBuffer(max_output_tokens * CHARS_PER_TOKEN)
        try:
            session.write(chars)
        except OSError as exc:
            raise ExecError(f"session {session_id} is not writable: {exc}") from None
        session.read_available(started + max(MIN_YIELD_MS, yield_time_ms) / 1000)
        return self._result(session, started)

    def _result(self, session: ExecSession, started: float) -> ExecResult:
        wall_time = time.monotonic() - started
        raw = session.buffer.render()
        original_tokens = (
            (session.buffer.head_len + session.buffer.tail_len + session.buffer.dropped)
            // CHARS_PER_TOKEN
            if session.buffer.dropped
            else None
        )

        exit_code = session.process.poll()
        if exit_code is None:
            return ExecResult(raw, wall_time, session_id=session.session_id,
                              original_tokens=original_tokens)

        # Finished: drain whatever is still buffered in the pty, then retire it.
        session.read_available(time.monotonic() + 0.05)
        raw = session.buffer.render()
        session.close()
        self.sessions.pop(session.session_id, None)
        return ExecResult(raw, wall_time, exit_code=exit_code, original_tokens=original_tokens)

    def close_all(self) -> None:
        for session in list(self.sessions.values()):
            session.close()
        self.sessions.clear()


# --------------------------------------------------------------------------
# Demos
# --------------------------------------------------------------------------


def demo() -> int:
    manager = ProcessManager()
    try:
        print("== a command that finishes ==")
        print(manager.exec_command("echo hello && exit 0", yield_time_ms=2000).render())

        print("\n== a command that does not finish inside the yield window ==")
        opened = manager.exec_command(
            "python3 -i -q -u", yield_time_ms=800
        )
        print(opened.render())
        assert opened.session_id, "expected a live session"

        print("\n== talking to the live process ==")
        print(manager.write_stdin(opened.session_id, "print(6 * 7)\n", yield_time_ms=800).render())

        print("\n== the same session still remembers its state ==")
        print(manager.write_stdin(opened.session_id, "x = 'kept'\nprint(x)\n", yield_time_ms=800).render())

        print("\n== closing it ==")
        print(manager.write_stdin(opened.session_id, "exit()\n", yield_time_ms=800).render())

        print("\n== output larger than the budget ==")
        big = manager.exec_command(
            "python3 -c \"print('x' * 200000)\"", yield_time_ms=5000, max_output_tokens=50
        )
        print(big.render()[:400])
        return 0
    finally:
        manager.close_all()


def repl() -> int:
    manager = ProcessManager()
    print("s06 -- 'open CMD' to start, then type to send stdin, 'quit' to stop")
    live: str | None = None
    try:
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                return 0
            if line.strip() == "quit":
                return 0
            try:
                if line.startswith("open "):
                    result = manager.exec_command(line[5:], yield_time_ms=1500)
                    live = result.session_id
                elif live:
                    result = manager.write_stdin(live, line + "\n", yield_time_ms=1500)
                    live = result.session_id or live
                else:
                    print("no live session; use `open CMD` first")
                    continue
            except ExecError as exc:
                print(f"error: {exc}")
                continue
            print(result.render())
    finally:
        manager.close_all()


def main(argv: list[str]) -> int:
    if "--repl" in argv:
        return repl()
    return demo()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
