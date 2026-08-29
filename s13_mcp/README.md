# s13: MCP — tools the harness did not write

[English](README.md) · [中文](README.zh.md)

[s12](../s12_instructions/) → `s13` → [s14](../s14_hooks/) → [s15](../s15_harness/)

> *"A coding agent has no business shipping a Jira client."*
>
> **Harness layer**: external capability — how someone else's tools get in.

---

## The problem

`exec_command` and `apply_patch` are built in. But a real team also needs:

- their issue tracker (Jira / Linear / whatever)
- their deploy API
- their internal doc search

Those **should not** be built in. Not just because the binary would bloat, but because every team
uses something different, and Codex cannot — and should not — know what your company runs.

---

## what MCP is

**MCP (Model Context Protocol)** is one convention: **a process that speaks newline-delimited
JSON-RPC over stdin/stdout can provide tools to an agent.**

Unpacking that:

- **JSON-RPC** is a minimal remote-call format: send a JSON object with `id`, `method`, `params`;
  get back one with the same `id` and a `result`.
- **Newline-delimited** means one JSON object per line, so reading a line reads a message. (Same
  idea as [s10](../s10_rollout/)'s JSONL.)
- **stdin/stdout** means no ports, no network config. Spawn a child process, write to its stdin,
  read from its stdout.

You declare it in config:

```toml
[mcp_servers.docs]
command = "python3"
args = ["tools/docs_server.py"]
```

The handshake is three messages, and after that the tools are just tools:

```
-> initialize                  {protocolVersion, capabilities, clientInfo}
-> notifications/initialized   (a notification; no reply expected)
-> tools/list                  <- [{name, description, inputSchema}, ...]
-> tools/call                  <- {content: [{type: "text", text: ...}]}
```

The `inputSchema` from `tools/list` is the same JSON Schema from
[s04](../s04_tool_registry/) — **so once an MCP tool arrives, it looks exactly like a built-in
one.**

> `code.py` is **both sides**: `--serve` is a working MCP server, and `--demo` starts two copies
> of it plus one that does not exist.

---

## The solution: MCP, and the three harness problems it brings

### Problem one: names collide

Two servers both export `search`. Now what?

The obvious move is to mangle names into `docs_search`, `wiki_search`. But then the name in the
schema is no longer the one the server's author wrote, and the descriptions read oddly.

Codex **groups** instead: each server's tools go inside a `type: "namespace"` object.

```json
{"type": "namespace",
 "name": "mcp__handbook__",
 "description": "Tools in the mcp__handbook__ namespace.",
 "tools": [{"type": "function", "name": "search", "parameters": {...}},
           {"type": "function", "name": "oncall", "parameters": {...}}]}
```

**Inside the namespace the tool is a plain `search`** — schema and description untouched and
readable.

On the way back, the response item carries `namespace` and `name` **separately**
(`ToolName { namespace, name }`), and the router joins them into the flat name it dispatches on:

```
mcp__handbook__search
mcp__handbook__oncall
mcp__wiki__search
mcp__wiki__oncall
```

The trailing `__` lives in the namespace string itself, so joining is plain concatenation:

```python
@property
def namespace(self) -> str:
    """Codex namespaces carry their own separator: `mcp__demo__`."""
    return f"{MCP_NAMESPACE_PREFIX}{self.server}__"

@property
def qualified_name(self) -> str:
    return f"{self.namespace}{self.name}"
```

### Problem two: a slow server must not stall the session

A server is a process someone else wrote. It may take 8 seconds to start, may not exist, may
hang.

```python
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
```

**Concurrent start, per-server timeout, failure downgraded to a warning.** The demo's third
server is deliberately broken:

```
ready:  handbook, wiki
failed: broken: could not start: No such file or directory
```

Nine working servers plus one typo in a config file should produce **nine working servers**.

### Problem three: tools are themselves context

Thirty servers with five tools each is 150 schemas, **carried on every request**. That eats the
window [s11](../s11_compaction/) is trying to protect.

Past a threshold, stop listing and hand the model a `tool_search` instead:

```
with 4 MCP tools, threshold 8:
  request carries: ['exec_command', 'mcp__wiki__{search, oncall}', 'mcp__handbook__{search, oncall}']

with 20 MCP tools, threshold 8:
  request carries: ['exec_command', 'tool_search']
  tool_search('tool7') -> ['mcp__server7__tool7']
```

**This is [s12](../s12_instructions/)'s skills trick applied to tools: advertise, then fetch.**

---

## How it works: three details that decide whether it does

**One: stderr must be discarded.**

```python
stderr=subprocess.DEVNULL,  # a chatty server must not corrupt the channel
```

**stdout *is* the protocol.** A server that logs to stdout breaks the transport outright (your
parser reads a log line and dies). One that logs to stderr is merely noisy, and discarding it
costs nothing.

**Two: never assume the next line is your answer.**

```python
if message.get("id") != request_id:
    continue  # a notification or another response; ignore it
```

JSON-RPC does not guarantee ordered request/response — notifications arrive whenever. A client
that assumes otherwise will **eventually read a notification as a tool result**.

**Three: a tool error is not a transport failure.**

```python
if result.get("isError"):
    # A tool error is content, not a transport failure: the model reads it.
    return f"error: {text}"
```

An MCP tool failing is **the same class of event** as a shell command exiting non-zero — the
model reads it and adjusts. Only the transport dying is a real error. Same rule as
[s04](../s04_tool_registry/)'s "never raise".

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `StdioMcpClient` | Handshake, `tools/list`, `tools/call` |
| `McpConnectionManager` | Concurrent startup, per-server failure, dispatch |
| `McpTool.to_spec` / `namespace_spec` | A plain function spec, grouped into a `type: "namespace"` object |
| `build_tool_specs` / `search_tools` | List them, or make them searchable |
| `serve` | A real MCP server in 40 lines |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| External tools | not possible without forking | declare an MCP server in config |
| Two servers exporting `search` | a name collision | grouped in a `type: "namespace"` object |
| One broken server | startup fails | a warning; the other nine still work |
| Thirty servers | 150 schemas on every request | one `tool_search` tool |

---

## Try it

**No API key needed:**

```bash
python s13_mcp/code.py --demo
```

It really does fork two MCP server processes, handshake with them, and call their tools.

**What to watch**: `handbook` and `wiki` are **the same server code run twice**, so their tools
have identical names. All four end up with distinct full names — zero collisions.

Be a server yourself:

```bash
python s13_mcp/code.py --serve
```

then type a JSON line at it (it answers on Enter):

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

---

## Real source

- `codex-rs/rmcp-client/`, `codex-rs/core/src/mcp.rs`, `mcp_tool_call.rs`
- `codex-rs/tools/src/responses_api.rs` — `ResponsesApiNamespace`
- `codex-rs/tools/src/tool_search.rs`
- `codex-rs/mcp-server/` — the other direction: Codex as an MCP server

---

## Next

MCP extends **what the agent can do**.

One thing it cannot extend: **the harness's own policy.** The sandbox rules, the approval logic,
what context gets injected — all of it is in the code, and changing it means forking the project.

[s14](../s14_hooks/) lets a user or an organization put **their own program** on the agent's
path — and its most interesting property is that **everything a hook returns is advisory, except
`deny`.**
