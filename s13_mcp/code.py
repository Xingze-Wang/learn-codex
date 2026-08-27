#!/usr/bin/env python3
"""
s13: MCP -- tools the harness did not write

`exec_command` and `apply_patch` are built in. Everything else a team needs --
their issue tracker, their deploy API, their internal search -- is not, and
should not be: a coding agent has no business shipping a Jira client.

MCP (Model Context Protocol) is how those arrive. A server is a process that
speaks newline-delimited JSON-RPC over stdin/stdout, declared in config:

    [mcp_servers.docs]
    command = "python3"
    args = ["tools/docs_server.py"]

The handshake is three messages, then the tools are just tools:

    -> initialize            {protocolVersion, capabilities, clientInfo}
    -> notifications/initialized
    -> tools/list            <- [{name, description, inputSchema}, ...]
    -> tools/call            <- {content: [{type: "text", text: ...}]}

Three harness problems come with them, and all three are the harness's job:

  * **Names collide.** Two servers both export `search`. Tools are namespaced
    by server, so the model calls `docs.search`, not `search`.
  * **A slow server must not slow the session.** Servers start concurrently,
    each with its own timeout, and one that fails to start is reported as a
    warning -- the session runs with the tools that did come up.
  * **Tools are context.** Thirty servers is a thousand lines of schema in
    every request. Past a threshold, Codex stops listing them and exposes a
    `tool_search` tool instead: the model asks for what it needs by name.

Run:
  python s13_mcp/code.py --demo      # starts the bundled server below and talks to it
  python s13_mcp/code.py --serve     # be an MCP server on stdin/stdout

Real source: codex-rs/rmcp-client/, codex-rs/core/src/mcp.rs,
codex-rs/core/src/mcp_tool_call.rs, codex-rs/tools/src/tool_search.rs
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_STARTUP_TIMEOUT = 10.0
DEFAULT_CALL_TIMEOUT = 60.0
TOOL_SEARCH_THRESHOLD = 8


class McpError(Exception):
    """A server-level failure. Never fatal: the session runs without that server."""


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT


@dataclass(frozen=True)
class McpTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.name}"

    def to_spec(self, *, defer_loading: bool = False) -> dict[str, Any]:
        spec = {
            "type": "function",
            "name": self.name,
            "namespace": self.server,
            "description": self.description,
            "parameters": self.input_schema or {"type": "object", "properties": {}},
        }
        if defer_loading:
            spec["defer_loading"] = True
        return spec


class StdioMcpClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def start(self) -> list[McpTool]:
        env = {**os.environ, **self.config.env}
        try:
            self.process = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # a chatty server must not corrupt the channel
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise McpError(f"{self.config.name}: could not start: {exc}") from None

        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "learn-codex", "version": "0.0.1"},
            },
            timeout=self.config.startup_timeout,
        )
        self._notify("notifications/initialized")
        listed = self._request("tools/list", {}, timeout=self.config.startup_timeout)
        return [
            McpTool(
                server=self.config.name,
                name=tool["name"],
                description=tool.get("description", ""),
                input_schema=tool.get("inputSchema", {}),
            )
            for tool in listed.get("tools", [])
        ]

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = DEFAULT_CALL_TIMEOUT) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        parts = [
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part)
        if result.get("isError"):
            # A tool error is content, not a transport failure: the model reads it.
            return f"error: {text}"
        return text or json.dumps(result, ensure_ascii=False)

    def close(self) -> None:
        if self.process is None:
            return
        try:
            self.process.stdin.close()  # type: ignore[union-attr]
            self.process.wait(timeout=2)
        except Exception:
            self.process.kill()
        self.process = None

    # -- JSON-RPC over newline-delimited JSON ------------------------------

    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise McpError(f"{self.config.name}: server is not running")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                message = self._read_line(deadline)
                if message is None:
                    break
                if message.get("id") != request_id:
                    continue  # a notification or another response; ignore it
                if "error" in message:
                    raise McpError(f"{self.config.name}: {message['error'].get('message')}")
                return message.get("result", {})
        raise McpError(f"{self.config.name}: timed out waiting for `{method}`")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _write(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _read_line(self, deadline: float) -> dict[str, Any] | None:
        assert self.process and self.process.stdout
        line = self.process.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {}  # junk on stdout: skip the line, keep the session


# --------------------------------------------------------------------------
# Connection manager
# --------------------------------------------------------------------------


@dataclass
class StartupReport:
    ready: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class McpConnectionManager:
    def __init__(self) -> None:
        self.clients: dict[str, StdioMcpClient] = {}
        self.tools: dict[str, McpTool] = {}
        self.report = StartupReport()

    def start_all(self, configs: list[McpServerConfig]) -> StartupReport:
        """Concurrent, and a failure is a warning. One broken server in a
        config file must not stop the other nine from being usable."""
        threads = []
        for config in configs:
            thread = threading.Thread(target=self._start_one, args=(config,), daemon=True)
            thread.start()
            threads.append((config, thread))
        for config, thread in threads:
            thread.join(timeout=config.startup_timeout + 1)
            if thread.is_alive():
                self.report.failed[config.name] = "startup timed out"
        return self.report

    def _start_one(self, config: McpServerConfig) -> None:
        client = StdioMcpClient(config)
        try:
            tools = client.start()
        except McpError as exc:
            self.report.failed[config.name] = str(exc)
            client.close()
            return
        self.clients[config.name] = client
        for tool in tools:
            self.tools[tool.qualified_name] = tool
        self.report.ready.append(config.name)

    def call(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(qualified_name)
        if tool is None:
            return f"unknown tool: {qualified_name}"
        client = self.clients.get(tool.server)
        if client is None:
            return f"server {tool.server} is not running"
        try:
            return client.call_tool(tool.name, arguments)
        except McpError as exc:
            return f"error: {exc}"

    def close_all(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()


# --------------------------------------------------------------------------
# Tool assembly: list them, or make them searchable
# --------------------------------------------------------------------------

TOOL_SEARCH_SPEC = {
    "type": "function",
    "name": "tool_search",
    "description": (
        "Search for tools that are available but not listed above. Returns "
        "matching tool names and their schemas, which you may then call."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}


def build_tool_specs(
    builtin: list[dict[str, Any]],
    mcp_tools: list[McpTool],
    *,
    threshold: int = TOOL_SEARCH_THRESHOLD,
) -> list[dict[str, Any]]:
    """Below the threshold, list everything. Above it, list nothing from MCP
    and hand the model a way to ask."""
    if len(mcp_tools) <= threshold:
        return [*builtin, *(tool.to_spec() for tool in mcp_tools)]
    return [*builtin, TOOL_SEARCH_SPEC]


def search_tools(mcp_tools: list[McpTool], query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    terms = [term for term in query.lower().split() if term]
    scored = []
    for tool in mcp_tools:
        haystack = f"{tool.qualified_name} {tool.description}".lower()
        score = sum(1 for term in terms if term in haystack)
        # An exact name hit beats a description that happens to share a word.
        if tool.name.lower() in terms or tool.qualified_name.lower() in terms:
            score += 10
        if score:
            scored.append((score, tool))
    scored.sort(key=lambda pair: (-pair[0], pair[1].qualified_name))
    return [tool.to_spec() for _, tool in scored[:limit]]


# --------------------------------------------------------------------------
# A tiny MCP server, so the demo has something real to talk to
# --------------------------------------------------------------------------

SERVER_TOOLS = [
    {
        "name": "search",
        "description": "Search the team handbook for a phrase.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "oncall",
        "description": "Who is on call for a service right now.",
        "inputSchema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
]

HANDBOOK = {
    "deploy": "Deploys go out Tuesday and Thursday, never on Friday.",
    "review": "Two approvals for anything touching billing.",
}


def serve() -> int:
    """Newline-delimited JSON-RPC on stdin/stdout. That is the whole transport."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        message_id = message.get("id")
        if message_id is None:
            continue  # a notification; nothing to answer

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "handbook", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": SERVER_TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})
            if name == "search":
                query = str(arguments.get("query", "")).lower()
                hits = [text for key, text in HANDBOOK.items() if query in key or query in text.lower()]
                result = {"content": [{"type": "text", "text": "\n".join(hits) or "no matches"}]}
            elif name == "oncall":
                service = arguments.get("service", "unknown")
                result = {"content": [{"type": "text", "text": f"{service}: alex until 18:00 UTC"}]}
            else:
                result = {"content": [{"type": "text", "text": f"no such tool: {name}"}], "isError": True}
        else:
            sys.stdout.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": message_id,
                     "error": {"code": -32601, "message": f"unknown method {method}"}}
                )
                + "\n"
            )
            sys.stdout.flush()
            continue

        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
        sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------


def _label(spec: dict[str, Any]) -> str:
    namespace = spec.get("namespace")
    return f"{namespace}.{spec['name']}" if namespace else spec["name"]


def demo() -> int:
    here = os.path.abspath(__file__)
    configs = [
        McpServerConfig("handbook", sys.executable, [here, "--serve"]),
        McpServerConfig("wiki", sys.executable, [here, "--serve"]),
        McpServerConfig("broken", "definitely-not-a-real-binary", []),
    ]

    manager = McpConnectionManager()
    report = manager.start_all(configs)
    print(f"ready:  {', '.join(sorted(report.ready)) or 'none'}")
    for name, reason in report.failed.items():
        print(f"failed: {name}: {reason}")

    print("\ntools (namespaced by server, so `search` twice is not a collision):")
    for name, tool in sorted(manager.tools.items()):
        print(f"  {name:<20} {tool.description}")

    print("\ncalling handbook.search:")
    print(" ", manager.call("handbook.search", {"query": "deploy"}))
    print("calling wiki.oncall:")
    print(" ", manager.call("wiki.oncall", {"service": "billing"}))
    print("calling a tool that does not exist:")
    print(" ", manager.call("handbook.nope", {}))

    tools = list(manager.tools.values())
    builtin = [{"type": "function", "name": "exec_command"}]
    print(f"\nwith {len(tools)} MCP tools, threshold 8:")
    print("  request carries:", [_label(spec) for spec in build_tool_specs(builtin, tools)])

    many = [
        McpTool(f"server{i}", f"tool{i}", f"does thing number {i} with deploys", {})
        for i in range(20)
    ]
    print(f"\nwith {len(many)} MCP tools, threshold 8:")
    print("  request carries:", [_label(spec) for spec in build_tool_specs(builtin, many)])
    found = search_tools(many, "tool7")
    print("  tool_search('tool7') ->", [_label(spec) for spec in found])

    manager.close_all()
    return 0


def main(argv: list[str]) -> int:
    if "--serve" in argv:
        return serve()
    return demo()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
