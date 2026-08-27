# s13：MCP —— 不是 harness 自己写的工具

[English](README.md) · [中文](README.zh.md)

[s12](../s12_instructions/) → `s13` → [s14](../s14_hooks/)

> *"一个编码 agent 没有理由自带一个 Jira 客户端。"*

---

`exec_command` 和 `apply_patch` 是内置的。一个团队需要的其余一切——他们的工单系统、
他们的部署 API、他们的内部搜索——都不是内置的，也不应该是。

MCP 就是这些东西进来的方式。一个 server 是一个在 stdin/stdout 上说"换行分隔 JSON-RPC"的进程，
在配置里声明：

```toml
[mcp_servers.docs]
command = "python3"
args = ["tools/docs_server.py"]
```

握手就三条消息，之后这些工具就只是工具：

```
-> initialize            {protocolVersion, capabilities, clientInfo}
-> notifications/initialized
-> tools/list            <- [{name, description, inputSchema}, ...]
-> tools/call            <- {content: [{type: "text", text: ...}]}
```

`code.py` 同时是两侧：`--serve` 是一个能用的 MCP server，`--demo` 会起两份它、再加一个根本不存在的。

## 它们同时带来三个 harness 问题

**名字会撞。** 两个 server 都导出了 `search`：

```
handbook.oncall      Who is on call for a service right now.
handbook.search      Search the team handbook for a phrase.
wiki.oncall          Who is on call for a service right now.
wiki.search          Search the team handbook for a phrase.
```

Codex 在名字旁边单独带一个 namespace（`ToolName { namespace, name }`），而不是把两者拼成一个字符串，
于是模型调用的是 `wiki` 命名空间下的 `search`，而路由知道该找哪个进程。

**慢的 server 不能拖慢会话。**

```
ready:  handbook, wiki
failed: broken: could not start: No such file or directory
```

server 并发启动，各有各的超时，失败只是一条警告。
九个能用的 server 加配置文件里的一个笔误，结果应该是九个能用的 server。

**工具本身就是上下文。** 三十个 server 意味着每次请求都带上上千行 schema：

```
with 4 MCP tools, threshold 8:
  request carries: ['exec_command', 'wiki.search', 'wiki.oncall', 'handbook.search', 'handbook.oncall']

with 20 MCP tools, threshold 8:
  request carries: ['exec_command', 'tool_search']
  tool_search('tool7') -> ['server7.tool7']
```

超过阈值，Codex 就不再罗列它们，而是暴露一个 `tool_search` 工具：模型自己去要，然后拿回 schema。
这就是 s12 的 skills 那一招，套用到工具上——**先挂牌子，再去取**。

## 决定成败的几个细节

```python
stderr=subprocess.DEVNULL,  # 一个话多的 server 不能污染这条通道
```

stdout **就是**协议本身。往 stdout 打日志的 server 会直接搞坏传输；往 stderr 打的只是吵，
而丢掉它不花任何代价。

```python
if message.get("id") != request_id:
    continue  # 是通知或者别的响应；忽略
```

JSON-RPC 并不保证请求-响应严格配对。通知随时会来，
所以一个"假定下一行就是自己答复"的客户端，迟早会把一条日志当成工具结果读进去。

```python
if result.get("isError"):
    # 工具错误是内容，不是传输故障：模型会读它。
    return f"error: {text}"
```

一个 MCP 工具失败，和一条 shell 命令返回非零是同一类事件——模型读到它然后调整。
只有传输本身死掉才算真正的错误。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `StdioMcpClient` | 握手、`tools/list`、`tools/call` |
| `McpConnectionManager` | 并发启动、逐 server 失败、分发 |
| `McpTool.to_spec` | 带命名空间的工具 spec |
| `build_tool_specs` / `search_tools` | 要么全列，要么变成可搜索的 |
| `serve` | 40 行的一个真 MCP server |

## 跑起来

```bash
python s13_mcp/code.py --demo
python s13_mcp/code.py --serve     # 在 stdin/stdout 上当一个 MCP server
```

## 对应真实源码

- `codex-rs/rmcp-client/`、`codex-rs/core/src/mcp.rs`、`mcp_tool_call.rs`
- `codex-rs/tools/src/tool_search.rs`
- `codex-rs/mcp-server/` —— 反过来：Codex 自己作为一个 MCP server

## 下一章

MCP 扩展的是 agent **能做什么**。[s14](../s14_hooks/) 扩展的是它**被允许做什么**。
