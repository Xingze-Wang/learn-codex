from __future__ import annotations

import sys
from pathlib import Path

import pytest
from helpers import ROOT, load

mod = load("s13_mcp")
SERVER = str(ROOT / "s13_mcp" / "code.py")


def config(name: str) -> object:
    return mod.McpServerConfig(name, sys.executable, [SERVER, "--serve"])


@pytest.fixture
def manager():
    m = mod.McpConnectionManager()
    yield m
    m.close_all()


def test_handshake_lists_tools(manager):
    report = manager.start_all([config("handbook")])
    assert report.ready == ["handbook"]
    assert set(manager.tools) == {"handbook.search", "handbook.oncall"}


def test_tools_are_namespaced_so_two_servers_can_share_a_name(manager):
    manager.start_all([config("a"), config("b")])
    assert "a.search" in manager.tools and "b.search" in manager.tools
    spec = manager.tools["a.search"].to_spec()
    assert spec["name"] == "search" and spec["namespace"] == "a"


def test_calling_a_tool_returns_text_content(manager):
    manager.start_all([config("handbook")])
    assert "Tuesday" in manager.call("handbook.search", {"query": "deploy"})


def test_tool_errors_come_back_as_text_not_exceptions(manager):
    manager.start_all([config("handbook")])
    assert manager.call("handbook.nope", {}).startswith("unknown tool")


def test_a_server_that_cannot_start_is_a_warning_not_a_failure(manager):
    report = manager.start_all([config("good"), mod.McpServerConfig("bad", "no-such-binary-xyz")])
    assert report.ready == ["good"]
    assert "bad" in report.failed
    assert manager.call("good.oncall", {"service": "billing"}).startswith("billing")


def test_a_server_that_never_answers_times_out(manager):
    slow = mod.McpServerConfig("slow", "/bin/sleep", ["30"], startup_timeout=0.5)
    report = manager.start_all([slow])
    assert "slow" in report.failed
    assert report.ready == []


def test_small_tool_sets_are_listed_inline():
    tools = [mod.McpTool("s", f"t{i}", "d", {}) for i in range(3)]
    specs = mod.build_tool_specs([{"type": "function", "name": "exec_command"}], tools)
    assert [s.get("name") for s in specs] == ["exec_command", "t0", "t1", "t2"]


def test_large_tool_sets_are_replaced_by_tool_search():
    tools = [mod.McpTool("s", f"t{i}", "d", {}) for i in range(50)]
    specs = mod.build_tool_specs([{"type": "function", "name": "exec_command"}], tools)
    assert [s["name"] for s in specs] == ["exec_command", "tool_search"]


def test_tool_search_ranks_exact_names_first():
    tools = [
        mod.McpTool("s", "deploy", "ship a release", {}),
        mod.McpTool("s", "status", "check deploy status", {}),
    ]
    found = mod.search_tools(tools, "deploy")
    assert found[0]["name"] == "deploy"


def test_junk_on_stdout_does_not_break_the_channel(manager):
    # The bundled server ignores unknown methods; a client must survive them.
    manager.start_all([config("handbook")])
    client = manager.clients["handbook"]
    with pytest.raises(mod.McpError):
        client._request("does/not/exist", {}, timeout=2)
    assert "Tuesday" in manager.call("handbook.search", {"query": "deploy"})
