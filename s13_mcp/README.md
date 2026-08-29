# s13: MCP — tools the harness did not write

[English](README.md) · [中文](README.zh.md)

[s12](../s12_instructions/) → `s13` → [s14](../s14_hooks/)

> *"A coding agent has no business shipping a Jira client."*

---

`exec_command` and `apply_patch` are built in. Everything else a team needs — their issue
tracker, their deploy API, their internal search — is not, and should not be.

MCP is how those arrive. A server is a process speaking newline-delimited JSON-RPC over
stdin/stdout, declared in config:

```toml
[mcp_servers.docs]
command = "python3"
args = ["tools/docs_server.py"]
```

The handshake is three messages, and then the tools are just tools:

```
-> initialize            {protocolVersion, capabilities, clientInfo}
-> notifications/initialized
-> tools/list            <- [{name, description, inputSchema}, ...]
-> tools/call            <- {content: [{type: "text", text: ...}]}
```

`code.py` is both sides: `--serve` is a working MCP server, and `--demo` starts two copies of it
plus one that does not exist.

## Three harness problems come with them

**Names collide.** Two servers both export `search`. Codex does not mangle the names — it sends
each server's tools grouped inside a `type: "namespace"` object:

```json
{"type": "namespace",
 "name": "mcp__handbook__",
 "description": "Tools in the mcp__handbook__ namespace.",
 "tools": [{"type": "function", "name": "search", "parameters": {...}},
           {"type": "function", "name": "oncall", "parameters": {...}}]}
```

Inside the namespace the tool is a plain `search`, so its schema and description stay readable.
On the way back in, the response item carries `namespace` and `name` separately
(`ToolName { namespace, name }`) and the router joins them into the flat form that hooks and
dispatch use:

```
mcp__handbook__search
mcp__handbook__oncall
mcp__wiki__search
mcp__wiki__oncall
```

The trailing `__` lives in the namespace string itself, which is why joining is concatenation
and not a `format!` with a separator.

**A slow server must not slow the session.**

```
ready:  handbook, wiki
failed: broken: could not start: No such file or directory
```

Servers start concurrently, each with its own timeout, and a failure is a warning. Nine working
servers and one typo in a config file should produce nine working servers.

**Tools are context.** Thirty servers is a thousand lines of schema in every request:

```
with 4 MCP tools, threshold 8:
  request carries: ['exec_command', 'mcp__wiki__{search, oncall}', 'mcp__handbook__{search, oncall}']

with 20 MCP tools, threshold 8:
  request carries: ['exec_command', 'tool_search']
  tool_search('tool7') -> ['mcp__server7__tool7']
```

Past a threshold, Codex stops listing them and exposes a `tool_search` tool: the model asks for
what it needs and gets the schemas back. This is s12's skills trick applied to tools —
advertise, then fetch.

## Details that decide whether it works

```python
stderr=subprocess.DEVNULL,  # a chatty server must not corrupt the channel
```

stdout *is* the protocol. A server that logs to stdout breaks the transport; one that logs to
stderr is merely noisy, and discarding it costs nothing.

```python
if message.get("id") != request_id:
    continue  # a notification or another response; ignore it
```

JSON-RPC is not request/response ordered. Notifications arrive whenever, so a client that
assumes the next line is its answer will eventually read a log line as a tool result.

```python
if result.get("isError"):
    # A tool error is content, not a transport failure: the model reads it.
    return f"error: {text}"
```

An MCP tool failing is the same class of event as a shell command exiting non-zero — the model
reads it and adjusts. Only the transport dying is an actual error.

## In `code.py`

| Piece | Job |
|---|---|
| `StdioMcpClient` | Handshake, `tools/list`, `tools/call` |
| `McpConnectionManager` | Concurrent startup, per-server failure, dispatch |
| `McpTool.to_spec` / `namespace_spec` | A plain function spec, grouped into a `type: "namespace"` object |
| `build_tool_specs` / `search_tools` | List them, or make them searchable |
| `serve` | A real MCP server in 40 lines |

## Run it

```bash
python s13_mcp/code.py --demo
python s13_mcp/code.py --serve     # be an MCP server on stdin/stdout
```

## Real source

- `codex-rs/rmcp-client/`, `codex-rs/core/src/mcp.rs`, `mcp_tool_call.rs`
- `codex-rs/tools/src/tool_search.rs`
- `codex-rs/mcp-server/` — Codex as an MCP server, the other direction

## Next

MCP extends what the agent can do. [s14](../s14_hooks/) extends what it is allowed to do.
