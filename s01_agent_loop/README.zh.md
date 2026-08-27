# s01：Agent Loop —— 一个循环、一个 shell、服务端不留状态

[English](README.md) · [中文](README.zh.md)

`s01` → [s02](../s02_protocol/) → ... → [s15](../s15_harness/)

> *"一个循环，一个 shell。"*
>
> 模型做决定，harness 负责执行，并把发生了什么原样送回去。

---

Codex 的本体是围绕一次模型调用的 `while True`：

```
+-----------+     input items    +-------+   function_call   +-------+
| history[] | -----------------> | 模型  | ----------------> | shell |
+-----------+                    +-------+                   +---+---+
     ^                               |                           |
     |      function_call_output     | 没有 function_call        |
     +-------------------------------+------<--------------------+
                                     v
                                  本轮结束
```

把对话发过去。响应里有 `function_call` 就执行它、追加一条 `function_call_output`、再发一次。
没有就结束这一轮。后面十四章全部围绕这个循环展开，但没有一章会去改这个循环。

## 请求体就是全部约定

```python
request = {
    "model": self.model,
    "instructions": instructions,
    "input": input_items,
    "tools": tools,
    "tool_choice": "auto",
    "parallel_tool_calls": False,
    "store": False,
    "stream": True,
    "include": ["reasoning.encrypted_content"],
}
```

两个字段值得单独说。

**`store: false`。** 服务端在两次请求之间什么都不记。Codex 每次把整段对话重新发一遍。
听上去很浪费，但换来的东西很关键：**历史归 harness 所有**。它可以重放（s10）、可以重写（s11）、
可以分叉、可以随便检查——因为历史是本地内存里的一个 list，而不是别人机器上一份状态的句柄。

**`include: ["reasoning.encrypted_content"]`。** 推理模型会产出客户端无权读取的 reasoning item。
在 `store: false` 下，这些 item 下一次请求还得送回去，否则模型会在任务中途丢掉自己的思路。
于是它们以加密形式返回，Codex 原样回传：

```python
elif isinstance(event, OutputItemDone):
    # 模型产出的每一项都回到 history --
    # message、reasoning、function call 一视同仁。
    self.history.append(event.item)
```

是**原样往返**，不是重新拼装。Codex 只会在回传前把 `id` 抹掉（`store: false` 时服务端根本不认
这些 id），其余一律不动。

## 只给一个工具，是刻意的

工具列表只有一项：

```python
EXEC_COMMAND_TOOL = {
    "type": "function",
    "name": "exec_command",
    "parameters": {"properties": {"cmd": {...}, "workdir": {...}}, "required": ["cmd"]},
}
```

没有 `read_file`，没有 `list_directory`，没有 `search`。`cat`、`ls`、`rg` 本来就存在，模型本来就会用，
而且你不定义的每一个工具，都是你不必在每次请求里发送的一份 schema。Codex 后面只多加了一个文件工具
（`apply_patch`，见 s05），加它的理由只有一个：**写**才是 shell 一行命令真正做不好的事。

## 循环里绝对不能做的事

`_dispatch` 里没有任何一处会抛异常：

```python
try:
    args = json.loads(call.get("arguments") or "{}")
except json.JSONDecodeError as exc:
    return f"invalid arguments: {exc}"
```

参数畸形、命令不存在、退出码非零——统统变成 `function_call_output` 里的一段文本。
模型下一轮读到自己的错误，然后自己改。在这里抛异常，等于为一件模型本可以自行修复的小事终结整个会话。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `ResponsesClient` | 真实路径：一次流式 Responses 请求 |
| `OutputTextDelta` / `OutputItemDone` / `Completed` | 归一化后的流事件（codex 里叫 `ResponseEvent`） |
| `exec_command` | 跑一条命令、收集输出、限制体积 |
| `Session.run_turn` | 那个循环 |

## 跑起来

```bash
export OPENAI_API_KEY=...
python s01_agent_loop/code.py "统计当前目录下有多少个 python 文件"
python s01_agent_loop/code.py            # 交互模式
```

## 对应真实源码

- `codex-rs/core/src/client.rs` —— 请求构造与 SSE 处理
- `codex-rs/core/src/session/turn.rs` —— `run_turn`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` —— 真正的 `exec_command` schema

## 下一章

循环能跑，但只服务于一个愿意阻塞等结果的调用方。[s02](../s02_protocol/) 用两条队列把它包起来，
中断、插话、审批也就随之成为可能。
