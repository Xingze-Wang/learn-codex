# s13: MCP —— 不是 harness 自己写的工具

[English](README.md) · [中文](README.zh.md)

[s12](../s12_instructions/) → `s13` → [s14](../s14_hooks/) → [s15](../s15_harness/)

> *"一个编码 agent 没有理由自带一个 Jira 客户端。"*
>
> **Harness 层**：外部能力 —— 别人写的工具怎么进来。

---

## 问题

`exec_command` 和 `apply_patch` 是内置的。但一个真实团队还需要：

- 他们的工单系统（Jira / Linear / 飞书）
- 他们的部署 API
- 他们的内部文档搜索

这些**不该**内置。理由不只是「代码会变臃肿」，而是：
每个团队用的东西都不一样，而 Codex 不可能、也不应该知道你们公司用什么。

---

## 先理解：MCP 是什么

**MCP（Model Context Protocol）** 就是一个约定：
**一个进程，在 stdin/stdout 上说「换行分隔的 JSON-RPC」，就能给 agent 提供工具。**

拆开看：

- **JSON-RPC** = 一种极简的远程调用格式：发一个带 `id`、`method`、`params` 的 JSON 对象过去，
  对面回一个带同样 `id` 和 `result` 的 JSON 对象。
- **换行分隔** = 一行一个 JSON 对象，读一行就是一条消息。（和 [s10](../s10_rollout/) 的 JSONL 同理。）
- **stdin/stdout** = 不用开端口、不用配网络。启动一个子进程，往它 stdin 写，从 stdout 读。

在配置文件里声明它：

```toml
[mcp_servers.docs]
command = "python3"
args = ["tools/docs_server.py"]
```

握手就三条消息，之后这些工具就只是工具：

```
-> initialize                  {protocolVersion, capabilities, clientInfo}
-> notifications/initialized   （通知，不需要回复）
-> tools/list                  <- [{name, description, inputSchema}, ...]
-> tools/call                  <- {content: [{type: "text", text: ...}]}
```

`tools/list` 返回的 `inputSchema` 就是 [s04](../s04_tool_registry/) 讲的那种 JSON Schema ——
**所以一个 MCP 工具进来之后，和内置工具长得一模一样。**

> `code.py` **同时是两侧**：`--serve` 是一个能用的 MCP server，
> `--demo` 会起两份它、再加一个根本不存在的。

---

## 解决方案：MCP，以及它带来的三个 harness 问题

### 问题一：名字会撞

两个 server 都导出了 `search`。怎么办？

一个想当然的做法是把名字拼起来：`docs_search`、`wiki_search`。但这样 schema 里的名字
就不再是 server 作者写的那个了，描述读起来也别扭。

Codex 的做法是**分组**：每个 server 的工具装进一个 `type: "namespace"` 对象里。

```json
{"type": "namespace",
 "name": "mcp__handbook__",
 "description": "Tools in the mcp__handbook__ namespace.",
 "tools": [{"type": "function", "name": "search", "parameters": {...}},
           {"type": "function", "name": "oncall", "parameters": {...}}]}
```

**在命名空间内部，工具就是朴素的 `search`** —— schema 和描述保持原样、保持可读。

回程时，响应项里 `namespace` 和 `name` 是**分开的**（`ToolName { namespace, name }`），
路由把它们拼成扁平名再去查表：

```
mcp__handbook__search
mcp__handbook__oncall
mcp__wiki__search
mcp__wiki__oncall
```

末尾那个 `__` 是**命名空间字符串自带的**，所以拼接就是直接拼：

```python
@property
def namespace(self) -> str:
    """Codex 命名空间自带分隔符：mcp__demo__。"""
    return f"{MCP_NAMESPACE_PREFIX}{self.server}__"

@property
def qualified_name(self) -> str:
    return f"{self.namespace}{self.name}"
```

### 问题二：一个慢的 server 不能拖垮会话

server 是别人写的进程。它可能启动要 8 秒，可能根本不存在，可能挂死。

```python
def start_all(self, configs: list[McpServerConfig]) -> StartupReport:
    """并发，且失败只是一条警告。配置文件里一个坏掉的 server
    不能阻止另外九个变得可用。"""
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

**并发启动 + 各自超时 + 失败降级为警告。** demo 里第三个 server 是故意写坏的：

```
ready:  handbook, wiki
failed: broken: could not start: No such file or directory
```

九个能用的 server 加配置文件里的一个笔误，结果应该是**九个能用的 server**。

### 问题三：工具本身就是上下文

三十个 server、每个五个工具，就是一百五十份 schema，**每次请求都要带上**。
这直接吃掉 [s11](../s11_compaction/) 拼命想保护的窗口。

超过阈值就不再罗列，改成给模型一个 `tool_search`：

```
with 4 MCP tools, threshold 8:
  request carries: ['exec_command', 'mcp__wiki__{search, oncall}', 'mcp__handbook__{search, oncall}']

with 20 MCP tools, threshold 8:
  request carries: ['exec_command', 'tool_search']
  tool_search('tool7') -> ['mcp__server7__tool7']
```

**这就是 [s12](../s12_instructions/) 的 skills 那一招，套用到工具上：先挂牌子，再去取。**

---

## 工作原理：三个决定成败的细节

**一、stderr 必须丢掉。**

```python
stderr=subprocess.DEVNULL,  # 一个话多的 server 不能污染这条通道
```

**stdout 就是协议本身。** 一个往 stdout 打日志的 server 会直接搞坏传输
（你的解析器会读到一行日志然后崩）。往 stderr 打的只是吵，而丢掉它不花任何代价。

**二、不能假设「下一行就是我的答复」。**

```python
if message.get("id") != request_id:
    continue  # 是通知或者别的响应；忽略
```

JSON-RPC 不保证请求-响应严格配对 —— 通知随时会来。
一个假定下一行就是自己答复的客户端，**迟早会把一条通知当成工具结果读进去**。

**三、工具报错 ≠ 传输故障。**

```python
if result.get("isError"):
    # 工具错误是内容，不是传输故障：模型会读它。
    return f"error: {text}"
```

一个 MCP 工具失败，和一条 shell 命令返回非零是**同一类事件** —— 模型读到它然后调整。
只有传输本身死掉才算真正的错误。这和 [s04](../s04_tool_registry/) 的「永远不抛异常」是同一条规矩。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `StdioMcpClient` | 握手、`tools/list`、`tools/call` |
| `McpConnectionManager` | 并发启动、逐 server 失败、分发 |
| `McpTool.to_spec` / `namespace_spec` | 朴素的 function spec，再打包进 `type: "namespace"` |
| `build_tool_specs` / `search_tools` | 要么全列，要么变成可搜索的 |
| `serve` | 40 行的一个真 MCP server |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 外部工具 | 不 fork 就没法加 | 在配置里声明一个 MCP server |
| 两个 server 都有 `search` | 名字撞了 | 各自装进一个 `type: "namespace"` 对象 |
| 一个坏掉的 server | 启动失败 | 一条警告；另外九个照常可用 |
| 三十个 server | 每次请求带 150 份 schema | 一个 `tool_search` 工具 |

---

## 试一下

**不需要 API key：**

```bash
python s13_mcp/code.py --demo
```

它会真的 fork 出两个 MCP server 进程，和它们握手，调用工具。

**观察重点**：`handbook` 和 `wiki` 是**同一份 server 代码**跑了两遍，
所以它们导出的工具重名。看列表里四个工具都拿到了各自的完整名字 —— 一次冲突都没有。

想自己当一次 server：

```bash
python s13_mcp/code.py --serve
```

然后手动敲一行 JSON 进去（回车之后它会答复）：

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

---

## 对应真实源码

- `codex-rs/rmcp-client/`、`codex-rs/core/src/mcp.rs`、`mcp_tool_call.rs`
- `codex-rs/tools/src/responses_api.rs` —— `ResponsesApiNamespace`
- `codex-rs/tools/src/tool_search.rs`
- `codex-rs/mcp-server/` —— 反过来：Codex 自己作为一个 MCP server

---

## 接下来

MCP 扩展的是 agent **能做什么**。

但还有一件事没法扩展：**harness 自己的策略**。
沙箱规则、审批逻辑、注入什么上下文 —— 全都写死在代码里，改它得 fork 整个项目。

[s14](../s14_hooks/) 让用户和组织把**自己的程序**放到 agent 的路径上 ——
而且它最有意思的地方是：**除了 `deny`，hook 返回的一切都只是建议。**
