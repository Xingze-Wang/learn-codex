# s04: 工具注册表 —— 按轮组装，按名字分发，永远不抛异常

[English](README.md) · [中文](README.zh.md)

[s03](../s03_turn_context/) → `s04` → [s05](../s05_apply_patch/) → ... → [s15](../s15_harness/)

> *"按轮组装工具，按名字分发，永远不抛异常。"*
>
> **Harness 层**：工具层 —— agent 的手。

---

## 问题

s01 把工具写死了：一个 `EXEC_COMMAND_TOOL`，一个 `if name == "exec_command"`。

现在要加东西了：`apply_patch`（改文件）、`update_plan`（列计划）、若干 MCP 工具
（[s13](../s13_mcp/)）。于是 `if / elif` 越排越长。

但真正撑不住的不是长度，是这三件事**每一轮都可能不一样**：

1. **模型不一样。** 有的模型训练时用的工具叫 `exec_command`，有的叫 `shell`，参数形状都不同。
   给错了，模型会调用一个它没学过的东西。
2. **配置不一样。** 用户关掉了 web 搜索、接了三个 MCP server —— 工具清单跟着变。
3. **调用的形状不一样。** 不是每个工具都吃 JSON。

第 3 点最容易被忽略，所以先讲它。

---

## 先理解：两种工具，两种载荷

**第一种：function tool（函数工具）。** 你给一份 JSON Schema 描述参数，模型给你一段 JSON。

```python
{
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {                          # 这就是 JSON Schema
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
        },
        "required": ["cmd"],
        "additionalProperties": False,       # 不许多给字段
    },
}
```

模型产出：`{"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\": \"ls\"}"}`。
注意 `arguments` 是**一个字符串**，里面装着 JSON。

**第二种：freeform tool（自由格式工具），在接口里叫 `custom`。** 模型直接吐原始文本，
不包 JSON。文本的合法形状由一份**文法**（这里用 Lark 语法）约束：

```python
{
    "type": "custom",
    "name": "apply_patch",
    "description": "…… 这是一个 FREEFORM 工具，不要把 patch 包进 JSON。",
    "format": {"type": "grammar", "syntax": "lark", "definition": APPLY_PATCH_LARK},
}
```

模型产出：`{"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n..."}`。
载荷在 `input` 里，是**一个裸字符串**。

**为什么 patch 要用第二种？** 因为把一份 200 行的 patch 塞进 JSON 字符串字段，
意味着里面每一个换行和引号都要转义。模型只要转义错一次，就赔进去一整轮。

而且文法是在**解码阶段**生效的：一个语法非法的 patch 不是「下游会被拒」，而是**根本生成不出来**。

---

## 解决方案

一个注册表：名字 → (spec, handler, 能否并行)。每轮按配置组装。

```
        ToolsConfig(shell_type="exec_command", apply_patch=True, ...)
                              │
                              ▼
                    build_registry(config)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      exec_command      apply_patch      update_plan
       (function)        (custom)        (function)
              │               │               │
              └───────────────┼───────────────┘
                              ▼
                   registry.specs()  ──►  请求体里的 tools 数组
                   registry.get(name) ──►  该跑哪个 handler
```

---

## 工作原理

**第 1 步**：模型和配置决定清单里有什么。

```python
def build_registry(config: ToolsConfig) -> ToolRegistry:
    registry = ToolRegistry()

    if config.shell_type == "shell":
        registry.register(RegisteredTool(SHELL, handle_shell, supports_parallel=False))
    else:
        registry.register(RegisteredTool(EXEC_COMMAND, handle_exec_command, supports_parallel=False))

    if config.apply_patch:
        registry.register(RegisteredTool(APPLY_PATCH, handle_apply_patch,
                                         supports_parallel=False, freeform=True))
    if config.plan_tool:
        registry.register(RegisteredTool(UPDATE_PLAN, handle_update_plan))
    return registry
```

`shell_type` 不是我编的 —— 它是模型记录里的一个字段。跑 `--list` 看看真实的清单长什么样。

**第 2 步**：`registry.specs()` 返回的东西，就是原封不动放进请求体的那份 `tools`。

```python
def specs(self) -> list[dict[str, Any]]:
    """完全就是请求体里那个 `tools` 数组。"""
    return [tool.spec for tool in self._tools.values()]
```

中间没有第二次转换。你 `--list` 看到的 JSON，就是模型收到的 JSON。

**第 3 步**：把回复里的一个 item 变成一次调用。这是唯一知道两种形状区别的地方。

```python
def build_tool_call(item: dict[str, Any]) -> ToolCall | None:
    if item.get("type") == "function_call":
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise ToolError(f"invalid JSON arguments: {exc}") from None
        return ToolCall(item.get("name", ""), item.get("call_id", ""), args)

    if item.get("type") == "custom_tool_call":
        return ToolCall(item.get("name", ""), item.get("call_id", ""), item.get("input", ""))

    return None      # 不是工具调用（可能是普通消息、reasoning）
```

`payload` 是 `dict` 就是函数工具，是 `str` 就是自由格式工具。下游靠这个区分。

**第 4 步**：分发。注意这里**一层套一层地兜住了所有错误**。

```python
def dispatch(registry: ToolRegistry, call: ToolCall, ctx: ToolContext) -> str:
    tool = registry.get(call.name)
    if tool is None:
        # 不是崩溃：告诉模型，让它去挑一个真实存在的工具。
        return f"unsupported tool: {call.name}"
    try:
        return tool.handler(call.payload, ctx)
    except ToolError as exc:
        return f"error: {exc}"
    except Exception as exc:  # handler 里的 bug 不能杀掉会话
        return f"internal tool error: {type(exc).__name__}: {exc}"
```

三层：名字不认识、handler 主动报错、handler 里有 bug。三种都变成一段文字还给模型。

---

## 一个更隐蔽的规则：连解析失败也必须回答

假设模型吐出了 `arguments: "{oops"`。第 3 步抛了 `ToolError`。现在怎么办？

**你仍然必须回答这次调用。**

```python
except ToolError as exc:
    # 模型吐出了没法解析的东西。仍然要回答这次调用 --
    # 一个没有被回答的 call_id 会让下一次请求直接报错。
    rejected += 1
    self.history.append({
        "type": "function_call_output",
        "call_id": item.get("call_id", ""),
        "output": f"error: {exc}",
    })
    continue

if not calls:
    if rejected:
        continue      # 给模型一个机会去修自己的调用
    return last_message
```

为什么？因为 history 里一个 `function_call` 如果没有配对的 `function_call_output`，
**这段对话就是畸形的**。下一次请求会失败，而失败的原因跟模型真正犯的错毫无关系 ——
你会去查一个不存在的问题。

注意最后那个 `if rejected: continue`：光回答还不够，还得**再发一次请求**，
模型才有机会看到自己的错误并改正。

---

## 输出有预算

一次 `make` 能吐出四万行。全塞进 history，[s11](../s11_compaction/) 想保护的上下文窗口就没了。

```python
def truncate_output(text: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """留头留尾；中间那段才是日志自我重复的地方。"""
    budget = max_tokens * CHARS_PER_TOKEN
    if len(text) <= budget:
        return text
    head = text[: budget // 2]
    tail = text[-budget // 2 :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n[... {dropped} characters truncated ...]\n{tail}"
```

为什么是留头留尾？因为构建日志的**头几行说明跑了什么**，**最后几行说明怎么失败的**，
中间四万行是同一条 warning 重复了四万遍。

---

## 并行是工具的属性，不是调用点的属性

```python
assert reg.supports_parallel("update_plan") is True
assert reg.supports_parallel("exec_command") is False
assert reg.supports_parallel("apply_patch") is False
```

两份 patch 在同一个文件上竞争，等于一次带额外步骤的合并冲突。两条 shell 命令在同一个工作目录里
竞争，更糟。

这里的注册表只是**记录**了这个标志，循环本身仍然逐个执行；真实的 router 用它来把可重叠的调用
批量并发（`ToolRouter::tool_supports_parallel`）。值得抄的正是「单独记录」这件事。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `function_tool` / `freeform_tool` | 两种 spec 形状 |
| `ToolRegistry` | 名字 → handler，以及给请求用的 `specs()` |
| `ToolsConfig` / `build_registry` | 按轮组装 |
| `build_tool_call` | 区分 `function_call` 与 `custom_tool_call` |
| `dispatch` | 查名字、兜住错误 |
| `truncate_output` | 头尾 token 预算 |

---

## 试一下

先看看发出去的工具清单长什么样：

```bash
python s04_tool_registry/code.py --list
```

**观察重点**：往下翻到 `apply_patch` 那一项，它的 `type` 是 `custom` 而不是 `function`，
而且带着一份 `format.definition` —— 那就是 [s05](../s05_apply_patch/) 里 patch 的完整文法。

然后真跑：

```bash
python s04_tool_registry/code.py "这里有多少行 python？"
```

---

## 对应真实源码

- `codex-rs/core/src/tools/registry.rs`、`router.rs`
- `codex-rs/tools/src/tool_spec.rs` —— `ToolSpec::{Function, Freeform, WebSearch, Namespace}`
- `codex-rs/core/src/tools/handlers/apply_patch_spec.rs` —— 那个文法工具

---

## 接下来

`apply_patch` 已经当了两章的占位符 —— 现在它只会打印「would patch: ...」。

[s05](../s05_apply_patch/) 把它真正实现出来，并回答一个问题：
**为什么 Codex 宁可用一种自定义的 patch 格式，也不让模型直接重写整个文件？**
