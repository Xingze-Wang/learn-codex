# s01: Agent Loop —— 一个循环 + 一个 shell

[English](README.md) · [中文](README.zh.md)

`s01` → [s02](../s02_protocol/) → s03 → ... → [s15](../s15_harness/)

> *"一个循环，一个 shell。"*
>
> **Harness 层**：循环 —— 模型和真实世界之间的第一根线。

---

## 问题

你想让模型帮你干活：「看看这个目录里有哪些 Python 文件，然后把测试跑一遍。」

模型能写出 `ls *.py` 和 `pytest`。但它写完就停了 —— 它自己跑不了，也看不到输出。

于是你手动跑一遍，把结果粘回对话框。它读完说「有三个测试挂了，我看看第一个」，又写出一条命令。
你再跑一遍，再粘回去。再一条，再跑，再粘。

**每一个来回，你都在当那个中间人。** 把这个中间人换成三十行代码，就是这一章。

---

## 模型是怎么「要求」执行命令的


模型不能执行任何东西。它只能输出文本。

但 OpenAI 的接口提供了一个约定，叫 **function calling**（函数调用）：你在请求里附上一份
「工具清单」，说明有哪些函数、每个函数吃什么参数。模型如果想用其中一个，它输出的就不是普通句子，
而是一个结构化的东西：

```json
{"type": "function_call", "name": "exec_command",
 "arguments": "{\"cmd\": \"ls *.py\"}", "call_id": "call_abc"}
```

这就是模型在说：**「请你帮我跑 `ls *.py`，跑完把结果按 `call_abc` 这个编号还给我。」**

你的程序（也就是 harness）真正去跑这条命令，然后把结果作为一条新消息追加回去：

```json
{"type": "function_call_output", "call_id": "call_abc", "output": "app.py\ntest_app.py\n"}
```

`call_id` 是把「请求」和「结果」配上对的那把钥匙。一次回复里模型可以要求跑好几条命令，
全靠这个编号对上号。

**记住两件事，后面十四章都用得上：**

- 模型产出的每一样东西（普通消息、思考、function_call）都叫一个 **item**。
- 整个对话就是一个 item 的列表，我们把它叫 `history`。

---

## 解决方案

一个 `while True`：模型要求调工具就继续，不要求就停。

```
  +-----------+   把 history 发过去   +-------+
  | history[] | -------------------> | 模型  |
  +-----------+                      +---+---+
       ^                                 |
       |                                 v
       |                         回复里有 function_call 吗？
       |                          /                    \
       |                        有                      没有
       |                         |                       |
       |                     跑这条命令               结束这一轮
       |                         |
       +---- 追加 function_call_output ----+
```

| 信号 | 含义 | 循环动作 |
|---|---|---|
| 回复里有 `function_call` | 模型要求执行命令 | 执行 → 结果追加回 history → 再发一次 |
| 回复里没有 `function_call` | 模型说完了 | 退出循环，把最后那段话给用户 |

---

## 工作原理

一步一步来。

**第 1 步**：把用户的话作为 history 里的第一个 item。

```python
self.history.append({
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": user_text}],
})
```

注意它不是一个裸字符串，而是一个带 `type` 的结构。history 里所有东西都长这样。

**第 2 步**：把 history 和工具清单一起发给模型。

```python
for event in self.client.stream(
    instructions=BASE_INSTRUCTIONS,      # 系统提示词
    input_items=list(self.history),      # 整段对话
    tools=[EXEC_COMMAND_TOOL],           # 工具清单
):
```

`stream` 是「流式」：回复不是一次性返回，而是一小块一小块地到。这样用户能一边看模型打字，
而不是盯着空屏幕等十秒。每一小块我们叫一个**事件**。

**第 3 步**：处理事件。只有三种。

```python
if isinstance(event, OutputTextDelta):
    print(event.delta, end="", flush=True)      # 一小段文字，直接打出来
elif isinstance(event, OutputItemDone):
    self.history.append(event.item)             # 一个完整的 item 做好了
    if event.item.get("type") == "function_call":
        calls.append(event.item)                # 记下来，等会要跑
elif isinstance(event, Completed):
    self.tokens += event.input_tokens + event.output_tokens
```

关键是中间那行：**模型产出的每个 item 都原样进 history**，不管是普通消息、思考，还是工具调用。
后面会讲为什么「原样」这两个字很重要。

**第 4 步**：如果一个 `function_call` 都没有，这一轮就结束了。

```python
if not calls:
    return last_message
```

这就是全部的退出条件。没有轮数上限，没有「任务完成」的判断 —— **模型不再要求工具，就是结束**。

**第 5 步**：有的话，就一条条跑。

```python
for call in calls:
    output = self._dispatch(call, echo=echo)
    self.history.append({
        "type": "function_call_output",
        "call_id": call["call_id"],
        "output": output,
    })
```

`call_id` 原样带回去，模型才知道这个结果对应它刚才哪一条请求。

**第 6 步**：回到第 2 步。history 现在长了三个 item（用户的话、模型的调用、命令的结果），
模型下一次就能看到命令跑出来什么了。

组装起来就是全部。这一段每一行都标了注释 —— 如果你能读懂它，这个仓库剩下的部分你都能读：

```python
def run_turn(self, user_text: str) -> str:        # 定义一个函数，收一句话，最后交回一句话
    self.history.append(user_item(user_text))     # 把用户这句话放进历史列表的末尾

    while True:                                   # 开始重复。没有次数上限
        calls = []                                # 准备一个空列表，装这一圈要跑的命令

        for event in self.client.stream(          # 问模型，一小块一小块地接它的回答
            instructions=BASE_INSTRUCTIONS,       #   系统提示词：你是谁、你该怎么干活
            input_items=list(self.history),       #   到目前为止的全部对话
            tools=[EXEC_COMMAND_TOOL],            #   它能用的工具清单（这里只有一个）
        ):
            if isinstance(event, OutputItemDone): # 如果这一小块是"一个完整的 item 做好了"
                self.history.append(event.item)   #   原样收进历史（消息、思考、工具调用都收）
                if event.item.get("type") == "function_call":   # 如果这个 item 是一次工具调用
                    calls.append(event.item)      #     记下来，等会要跑

        if not calls:                             # 这一圈模型一个工具都没要求
            return last_message                   #   说明它说完了。交回它最后那段话，函数结束

        for call in calls:                        # 否则，把记下的命令一条条跑掉
            self.history.append({                 #   跑完的结果也放进历史
                "type": "function_call_output",   #     标明这是"一次工具调用的结果"
                "call_id": call["call_id"],       #     贴上原来那次调用的编号，好让模型对上号
                "output": self._dispatch(call),   #     真正去执行它，拿到输出
            })
                                                  # 回到 while True 的开头，再问一次模型 --
                                                  # 这次它能看到命令跑出来什么了
```

**读一遍这个循环在干什么：**

> 把你的话记下来 → 问模型 → 它要求跑命令吗？
> **不要求** → 结束，把它的话给你。
> **要求** → 跑掉，把结果记下来 → 再问一次模型 → 回到开头。

三十行，一个能干活的 agent 就有了。**后面十四章全部围绕它展开，没有一章去改它。**

---

## 让它变成 Codex 的两个字段

请求体里有两个字段，是 Codex 和大多数教程写法不一样的地方。

```python
request = {
    "model": self.model,
    "instructions": instructions,
    "input": input_items,
    "tools": tools,
    "tool_choice": "auto",
    "store": False,                                  # <--
    "stream": True,
    "include": ["reasoning.encrypted_content"],      # <--
}
```

### `store: false` —— 服务端什么都不记

很多接口允许你只发新消息，服务端自己记着前面聊过什么。Codex 关掉了这个：**每一次请求都把整段
对话重新发一遍。**

听上去很浪费。但换来的东西是这个仓库后面一半章节的前提：

**历史归 harness 所有。** 它就是你内存里一个普通的 list。所以你可以：

- 把它写到磁盘上，下次接着聊（[s10](../s10_rollout/)）；
- 在它太长时把中间部分换成一段摘要（[s11](../s11_compaction/)）；
- 从第三轮切一刀，复制出两条不同的未来（[s10](../s10_rollout/) 的 fork）。

如果历史是「服务端的一个 id」，上面这三件事你一件都做不了。

### `include: ["reasoning.encrypted_content"]` —— 加密的思考

推理模型在回答之前会先「想」。这段思考你没有权限读，但它必须在下一次请求里回到模型手上，
否则模型会在任务中途丢掉自己的思路。

于是接口把它加密后返回，Codex 原样回传。所以第 3 步那行「每个 item 都进 history」是这么写的：

```python
elif isinstance(event, OutputItemDone):
    self.history.append(event.item)      # 不解析、不重建、原样收着
```

唯一的加工是把 `id` 抹掉 —— 在 `store: false` 下服务端根本不认这些 id：

```python
raw.pop("id", None)
raw.pop("status", None)
```

---

## 只给一个工具，是刻意的

工具清单只有一项：

```python
EXEC_COMMAND_TOOL = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
            "workdir": {"type": "string", "description": "Working directory."},
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
}
```

没有 `read_file`，没有 `list_directory`，没有 `search`。因为 `cat`、`ls`、`rg` 本来就在那台机器上，
模型也早就会用。而且**你不定义的每个工具，都是你不必在每次请求里发送的一份 schema**。

Codex 后来只多加了一个文件工具 —— `apply_patch`（[s05](../s05_apply_patch/)）——
加它的理由只有一个：**写**文件是 shell 一行命令真正做不好的事。

---

## 循环里绝对不能抛异常

`_dispatch` 里没有一处会往外抛错：

```python
try:
    args = json.loads(call.get("arguments") or "{}")
except json.JSONDecodeError as exc:
    return f"invalid arguments: {exc}"
```

参数畸形、命令不存在、退出码非零 —— 统统变成 `function_call_output` 里的一段文字。

为什么？因为**模型下一轮会读到自己的错误，然后自己改**。在这里抛异常，等于为一件模型本来
能自己修好的小事，把整个会话干掉。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `ResponsesClient` | 真实路径：发一次流式请求，把收到的碎片翻译成三种 Python 对象 |
| `OutputTextDelta` / `OutputItemDone` / `Completed` | 那三种事件（codex 里这个枚举叫 `ResponseEvent`） |
| `exec_command` | 跑一条命令，收集输出，限制体积 |
| `Session.run_turn` | 上面那个循环 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 循环 | 你自己复制粘贴 | `while True` 检查有没有 `function_call` |
| 工具 | 没有 | `exec_command` |
| 历史 | 没有 | 一个不断变长的 item 列表 |
| 什么时候停 | 你累了就停 | 模型不再要求工具 |

---

## 试一下

> **安全提示**：这段代码会执行模型生成的 shell 命令，而且**没有任何防护**。
> 请在一个临时目录里跑。[s07](../s07_sandbox/) 会加上沙箱。

准备：

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...
```

运行：

```bash
python s01_agent_loop/code.py "这个目录里有多少个 python 文件？"
python s01_agent_loop/code.py            # 交互模式
```

试试这几句：

1. `创建一个 hello.py，打印 Hello, World!`
2. `这个仓库里哪个文件最大？`
3. `当前的 git 分支是什么？`

**观察重点**：数一数它跑了几条命令才回答你。第 2 题它多半会先 `ls` 再 `du` —— 那就是循环
转了两圈。它什么时候停下来的？就是它不再要求跑命令的那一刻。

---

## 对应真实源码

- `codex-rs/core/src/client.rs` —— 请求怎么构造、SSE 怎么解析
- `codex-rs/core/src/session/turn.rs` —— `run_turn`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` —— 真正的 `exec_command` schema

---

## 接下来

现在这个循环有个毛病：它一跑起来，你就只能干等。

它跑到一半你发现方向错了，想改口 —— 没有入口。你想让它停下 —— 没有入口。它想问你
「这条命令有点危险，能跑吗？」—— 也没有入口，因为函数只能返回一次。

[s02](../s02_protocol/) 把这个循环放进两条队列里。**中断、插话、审批，这三件事会同时变得可能** ——
而且它们其实是同一件事。
