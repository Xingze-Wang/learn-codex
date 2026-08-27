# s04：工具注册表

[English](README.md) · [中文](README.zh.md)

[s03](../s03_turn_context/) → `s04` → [s05](../s05_apply_patch/)

> *"按轮组装工具，按名字分发，永远不抛异常。"*

---

s01 把一个工具和一个 `if name == ...` 写死了。有三件事让这种写法撑不住。

**模型会变。** 每个模型记录都带一个 `shell_type`。用 `exec_command` 训练的模型永远看不到 `shell` 工具，
反之亦然：

```python
if config.shell_type == "shell":
    registry.register(RegisteredTool(SHELL, handle_shell, supports_parallel=False))
else:
    registry.register(RegisteredTool(EXEC_COMMAND, handle_exec_command, supports_parallel=False))
```

**配置会变。** `web_search`、`update_plan`、`apply_patch`、MCP server（s13）——每一个都只在启用时存在。
`registry.specs()` 返回的东西，就是原封不动放进请求体的那份 `tools`。

**调用形状会变。** 不是每个工具都吃 JSON。

## Freeform 工具

`apply_patch` 是一个挂着 Lark 文法的 `custom` 工具：

```python
{
    "type": "custom",
    "name": "apply_patch",
    "description": "…… 这是一个 FREEFORM 工具，不要把 patch 包进 JSON。",
    "format": {"type": "grammar", "syntax": "lark", "definition": APPLY_PATCH_LARK},
}
```

模型直接吐出 patch 文本，而解码过程被这份文法约束住，所以一个语法非法的 patch 不只是"下游会被拒"，
而是**根本生成不出来**。调用抵达时是 `custom_tool_call.input`（一个字符串），不是
`function_call.arguments`（JSON），`build_tool_call` 是唯一知道这个区别的地方：

```python
if item.get("type") == "custom_tool_call":
    return ToolCall(item.get("name", ""), item.get("call_id", ""), item.get("input", ""))
```

把一份 200 行的 patch 塞进 JSON 字符串字段，意味着里面每一个换行和引号都要转义，
而模型只要转义错一次，就赔进去一整轮。

## 失败是消息，不是异常

```python
def dispatch(registry, call, ctx):
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

循环里还有一条更隐蔽的同类规则：即使一个调用**连解析都失败**，harness 也必须回答它。

```python
except ToolError as exc:
    # 模型吐出了没法解析的东西。仍然要回答这次调用 --
    # 一个没有被回答的 call_id 会让下一次请求直接报错。
    rejected += 1
    self.history.append({"type": "function_call_output", "call_id": ..., "output": f"error: {exc}"})
```

一个没有配对 `function_call_output` 的 `function_call` 是一段畸形对话。
下一次请求会失败，而失败的原因跟模型真正犯的错毫无关系。

## 输出有预算

```python
def truncate_output(text, max_tokens=MAX_OUTPUT_TOKENS):
    """留头留尾；中间那段才是日志自我重复的地方。"""
```

留头留尾，中间放一个标记。构建日志的头几行说明跑了什么，最后几行说明怎么失败的；
中间四万行是同一条 warning 重复了四万遍。没有这一步，一次 `make` 就能吃光 s11 想保护的上下文窗口。

## 并行是逐工具决定的

```python
assert reg.supports_parallel("update_plan") is True
assert reg.supports_parallel("exec_command") is False
assert reg.supports_parallel("apply_patch") is False
```

两份 patch 在同一个文件上竞争，等于一次带额外步骤的合并冲突。两条 shell 命令在同一个工作目录里竞争，
更糟。只读工具可以重叠；任何会改动工作区的东西一律串行。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `function_tool` / `freeform_tool` | 两种 spec 形状 |
| `ToolRegistry` | 名字 → handler，以及给请求用的 `specs()` |
| `ToolsConfig` / `build_registry` | 按轮组装 |
| `build_tool_call` | 区分 `function_call` 与 `custom_tool_call` |
| `dispatch` | 查名字、兜住错误 |
| `truncate_output` | 头尾 token 预算 |

## 跑起来

```bash
python s04_tool_registry/code.py --list          # 作为 `tools` 发出去的那份 JSON
python s04_tool_registry/code.py "这里有多少行 python？"
```

## 对应真实源码

- `codex-rs/core/src/tools/registry.rs`、`router.rs`
- `codex-rs/tools/src/tool_spec.rs` —— `ToolSpec::{Function, Freeform, WebSearch, Namespace}`
- `codex-rs/core/src/tools/handlers/apply_patch_spec.rs` —— 那个文法工具

## 下一章

`apply_patch` 已经当了两章的占位符。[s05](../s05_apply_patch/) 把它真正实现出来。
