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
    assert set(manager.tools) == {"mcp__handbook__search", "mcp__handbook__oncall"}


def test_tools_are_grouped_into_a_namespace_object(manager):
    manager.start_all([config("a"), config("b")])
    assert "mcp__a__search" in manager.tools and "mcp__b__search" in manager.tools

    specs = mod.build_tool_specs([], list(manager.tools.values()))
    by_name = {spec["name"]: spec for spec in specs}
    assert set(by_name) == {"mcp__a__", "mcp__b__"}

    namespace = by_name["mcp__a__"]
    assert namespace["type"] == "namespace"
    assert namespace["description"] == "Tools in the mcp__a__ namespace."
    # Inside the namespace the tool is a plain function, unmangled.
    inner = {tool["name"] for tool in namespace["tools"]}
    assert inner == {"search", "oncall"}
    assert all(tool["type"] == "function" for tool in namespace["tools"])
    assert all("namespace" not in tool for tool in namespace["tools"])


def test_calling_a_tool_returns_text_content(manager):
    manager.start_all([config("handbook")])
    assert "Tuesday" in manager.call("mcp__handbook__search", {"query": "deploy"})


def test_tool_errors_come_back_as_text_not_exceptions(manager):
    manager.start_all([config("handbook")])
    assert manager.call("mcp__handbook__nope", {}).startswith("unknown tool")


def test_a_server_that_cannot_start_is_a_warning_not_a_failure(manager):
    report = manager.start_all([config("good"), mod.McpServerConfig("bad", "no-such-binary-xyz")])
    assert report.ready == ["good"]
    assert "bad" in report.failed
    assert manager.call("mcp__good__oncall", {"service": "billing"}).startswith("billing")


def test_a_server_that_never_answers_times_out(manager):
    slow = mod.McpServerConfig("slow", "/bin/sleep", ["30"], startup_timeout=0.5)
    report = manager.start_all([slow])
    assert "slow" in report.failed
    assert report.ready == []


def test_small_tool_sets_are_listed_inline():
    tools = [mod.McpTool("s", f"t{i}", "d", {}) for i in range(3)]
    specs = mod.build_tool_specs([{"type": "function", "name": "exec_command"}], tools)
    assert [s["name"] for s in specs] == ["exec_command", "mcp__s__"]
    assert [t["name"] for t in specs[1]["tools"]] == ["t0", "t1", "t2"]


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
    assert found[0].qualified_name == "mcp__s__deploy"


def test_the_flat_name_is_what_hooks_and_the_router_see():
    tool = mod.McpTool("filesystem", "read_file", "d", {})
    assert tool.namespace == "mcp__filesystem__"
    assert tool.qualified_name == "mcp__filesystem__read_file"


def test_junk_on_stdout_does_not_break_the_channel(manager):
    # The bundled server ignores unknown methods; a client must survive them.
    manager.start_all([config("handbook")])
    client = manager.clients["handbook"]
    with pytest.raises(mod.McpError):
        client._request("does/not/exist", {}, timeout=2)
    assert "Tuesday" in manager.call("mcp__handbook__search", {"query": "deploy"})
