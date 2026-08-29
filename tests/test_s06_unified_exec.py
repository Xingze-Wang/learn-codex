from __future__ import annotations

import pytest
from helpers import load

mod = load("s06_unified_exec")


@pytest.fixture
def manager():
    m = mod.ProcessManager()
    yield m
    m.close_all()


def test_finished_command_reports_exit_code(manager):
    result = manager.exec_command("echo hello", yield_time_ms=3000)
    assert result.exit_code == 0
    assert "hello" in result.output
    assert result.session_id is None
    assert "Process exited with code 0" in result.render()


def test_nonzero_exit_is_reported_not_raised(manager):
    result = manager.exec_command("exit 7", yield_time_ms=3000)
    assert result.exit_code == 7


def test_unfinished_command_yields_a_session(manager):
    result = manager.exec_command("cat", yield_time_ms=300)
    assert result.exit_code is None
    assert result.session_id is not None
    assert "Process running with session ID" in result.render()

    echoed = manager.write_stdin(result.session_id, "ping\n", yield_time_ms=500)
    assert "ping" in echoed.output


def test_session_keeps_state_between_calls(manager):
    opened = manager.exec_command("python3 -i -q -u", yield_time_ms=800)
    assert opened.session_id
    manager.write_stdin(opened.session_id, "value = 41\n", yield_time_ms=500)
    seen = manager.write_stdin(opened.session_id, "print(value + 1)\n", yield_time_ms=800)
    assert "42" in seen.output


def test_writing_to_a_dead_session_is_an_error(manager):
    result = manager.exec_command("true", yield_time_ms=2000)
    assert result.session_id is None
    with pytest.raises(mod.ExecError, match="no live session"):
        manager.write_stdin("999", "x\n")


def test_finished_sessions_are_reaped(manager):
    manager.exec_command("echo done", yield_time_ms=2000)
    assert manager.sessions == {}


def test_head_tail_buffer_keeps_both_ends():
    buf = mod.HeadTailBuffer(max_chars=20)
    buf.append("START")
    buf.append("x" * 500)
    buf.append("END")
    rendered = buf.render()
    assert rendered.startswith("START")
    assert rendered.endswith("END")
    assert "truncated" in rendered


def test_large_output_is_capped_and_counted(manager):
    result = manager.exec_command(
        "python3 -c \"print('y' * 100000)\"", yield_time_ms=5000, max_output_tokens=40
    )
    assert result.original_tokens is not None and result.original_tokens > 40
    assert len(result.output) < 100_000
    assert "truncated" in result.output


def test_yield_time_is_clamped(manager):
    result = manager.exec_command("echo x", yield_time_ms=1)
    assert result.output is not None  # 1ms is clamped up to MIN_YIELD_MS, not honored literally


def test_a_closed_pty_waits_for_the_exit_status(manager):
    """The pty closing and the process being reaped are separate events.

    Reading only `poll()` reports a live session id for a command that has
    already finished -- rare, but it showed up as a flaky test under load.
    """
    import os
    import pty
    import subprocess
    import time

    master_fd, slave_fd = pty.openpty()
    # Still running when _result is called, but the pty is already marked closed.
    process = subprocess.Popen(["/bin/sh", "-c", "sleep 0.2"], stdin=slave_fd,
                               stdout=slave_fd, stderr=slave_fd, start_new_session=True)
    os.close(slave_fd)
    session = mod.ExecSession(
        session_id="test",
        process=process,
        master_fd=master_fd,
        command="sleep 0.2",
        cwd=".",
        buffer=mod.HeadTailBuffer(1000),
        pty_closed=True,
    )
    manager.sessions[session.session_id] = session

    assert process.poll() is None            # not reaped yet
    result = manager._result(session, time.monotonic())

    assert result.exit_code == 0             # we waited for the status
    assert result.session_id is None         # not reported as a live session
    assert manager.sessions == {}
