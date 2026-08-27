from __future__ import annotations

import asyncio

import pytest
from helpers import ScriptedClient, call, load, say

mod = load("s02_protocol")


async def collect(thread, stop_on) -> list:
    seen = []
    while True:
        event = await asyncio.wait_for(thread.next_event(), timeout=5)
        seen.append(event.msg)
        if isinstance(event.msg, stop_on):
            return seen


@pytest.mark.asyncio
async def test_ops_in_events_out(tmp_path):
    client = ScriptedClient(mod, [call(mod, "echo hi"), say(mod, "done")])
    thread = mod.CodexThread(client, cwd=str(tmp_path))
    thread.start()

    await thread.submit(mod.UserTurn("go"))
    seen = await collect(thread, mod.TaskComplete)

    kinds = [type(m).__name__ for m in seen]
    assert kinds[0] == "TaskStarted"
    assert "ExecCommandBegin" in kinds and "ExecCommandEnd" in kinds
    assert seen[-1].last_agent_message == "done"


@pytest.mark.asyncio
async def test_mid_turn_message_steers_instead_of_starting_a_turn(tmp_path):
    gate = asyncio.Event()

    class SlowClient(ScriptedClient):
        def stream(self, **kwargs):
            if len(self.requests) == 0:
                # let the test submit a second UserTurn while we are "thinking"
                asyncio.run_coroutine_threadsafe(_set(gate), loop).result(timeout=5)
            return super().stream(**kwargs)

    async def _set(ev):
        ev.set()

    loop = asyncio.get_running_loop()
    client = SlowClient(mod, [call(mod, "sleep 0.2"), say(mod, "ok")])
    thread = mod.CodexThread(client, cwd=str(tmp_path))
    thread.start()

    await thread.submit(mod.UserTurn("first"))
    await gate.wait()
    await thread.submit(mod.UserTurn("actually, do this instead"))

    seen = await collect(thread, mod.TaskComplete)
    assert any(isinstance(m, mod.UserMessageQueued) for m in seen)

    # One turn ran, and the steer arrived in history before the second request.
    texts = [
        item["content"][0]["text"]
        for item in thread.session.history
        if item.get("type") == "message" and item.get("role") == "user"
    ]
    assert texts == ["first", "actually, do this instead"]
    assert len(client.requests) == 2
    assert sum(1 for m in seen if isinstance(m, mod.TaskStarted)) == 1


@pytest.mark.asyncio
async def test_interrupt_cancels_the_running_turn(tmp_path):
    started = asyncio.Event()

    class BlockingClient:
        requests: list = []

        def stream(self, **kwargs):
            loop.call_soon_threadsafe(started.set)
            import time

            while True:  # never yields; only cancellation gets us out
                time.sleep(0.05)

    loop = asyncio.get_running_loop()
    thread = mod.CodexThread(BlockingClient(), cwd=str(tmp_path))
    thread.start()

    await thread.submit(mod.UserTurn("go"))
    await asyncio.wait_for(started.wait(), timeout=5)
    await thread.submit(mod.Interrupt())

    seen = await collect(thread, mod.TurnAborted)
    assert isinstance(seen[-1], mod.TurnAborted)
    # The interruption is recorded so the next turn's model call can see it.
    assert thread.session.history[-1]["content"][0]["text"] == "[turn interrupted by user]"


@pytest.mark.asyncio
async def test_interrupt_without_active_turn_is_reported_not_crashed(tmp_path):
    thread = mod.CodexThread(ScriptedClient(mod, []), cwd=str(tmp_path))
    thread.start()
    await thread.submit(mod.Interrupt())
    seen = await collect(thread, mod.TurnAborted)
    assert seen[-1].reason == "no active turn"
