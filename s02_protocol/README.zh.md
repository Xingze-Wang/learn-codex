# s02: Submission / Event 协议 —— 把循环变成一个可以说话的东西

[English](README.md) · [中文](README.zh.md)

[s01](../s01_agent_loop/) → `s02` → [s03](../s03_turn_context/) → ... → [s15](../s15_harness/)

> *"调用方不调用 agent。它提交一个 Op，然后读 Event。"*
>
> **Harness 层**：协议缝 —— 一个内核，多个前端。

---

## 问题

你让它重构一个模块。跑了两分钟，你从输出里看到它在改错的文件。

你想说一句：「等等，不是那个文件。」

**但你没有地方说这句话。** s01 的 `run_turn` 是一个普通函数：调用它，它跑，跑完 return。
在「跑」的那段时间里，你唯一能做的是 Ctrl-C —— 把整个进程杀掉，连同它已经想明白的一切。

同一个毛病还有两个变体：

- 它准备跑 `rm -rf build`，按理该先问你一声。但函数只能 return 一次，
  它没法「中途问一句、等你回答、然后接着跑」。
- 它卡在一个跑了五分钟的命令上。你想只停这一轮，保留对话。做不到。

---

## 先理解：函数这个形状装不下这些需求

一次函数调用的形状是固定的：

```
调用 ──────────────────────────────► 返回
       （这段时间里，外面进不来东西）
```

「中途插一句话」「中途叫停」「中途回答一个问题」—— 这三件事都要求在那条横线**中间**
有一个入口。函数没有这个入口。

所以 Codex 不把循环暴露成函数。它暴露成**两条队列**：

- 你往第一条队列里**放**东西（想让 agent 做什么）。
- 你从第二条队列里**读**东西（agent 正在发生什么）。

放和读是两件独立的事，谁都不等谁。于是「中途」这个概念第一次有了位置。

两条队列在 Codex 里的名字：

- **SQ（Submission Queue）** —— 进来的东西，每一条叫一个 **`Op`**（operation，操作）。
- **EQ（Event Queue）** —— 出去的东西，每一条叫一个 **`Event`**。

---

## 解决方案

```
                        ┌──────────────── SQ ────────────────┐
   调用方 ── Op ──────► │ UserTurn / Interrupt / Shutdown ... │
                        └──────────────┬─────────────────────┘
                                       │  submission_loop 逐条取
                                       ▼
                               ┌───────────────┐
                               │  一轮 (task)   │  ◄── 可以被 cancel
                               └───────┬───────┘
                        ┌──────────────▼─────── EQ ──────────┐
   调用方 ◄─ Event ───  │ TaskStarted / ExecCommandBegin ...  │
                        └────────────────────────────────────┘
```

`CodexThread.submit(op)` 把一条提交丢进 SQ 就**立刻返回**。`next_event()` 从 EQ 读。

Codex 自带的每个前端 —— 终端界面、`codex exec --json`、给编辑器用的 app-server、
它自己作为 MCP server —— 都只是**同一条事件流的不同读者**。
没有哪一个是「那个」界面，这正是它能同时有四个的原因。

---

## 工作原理

**第 1 步**：定义调用方能提交什么。就四种。

```python
@dataclass(frozen=True)
class UserTurn:
    text: str          # 用户说了一句话

@dataclass(frozen=True)
class Interrupt:
    pass               # 停下当前这一轮

@dataclass(frozen=True)
class Shutdown:
    pass               # 收摊

Op = UserTurn | Interrupt | Shutdown
```

每条提交带一个 id，好让回来的事件对得上号：

```python
@dataclass(frozen=True)
class Submission:
    id: str
    op: Op
```

**第 2 步**：定义会发生什么。这些就是前端拿来渲染的原料。

```python
TaskStarted          # 一轮开始了
AgentMessageDelta    # 模型吐出一小段文字
ExecCommandBegin     # 要跑这条命令了
ExecCommandEnd       # 跑完了，退出码是这个
UserMessageQueued    # 你刚才那句话被排进了正在跑的这一轮
TokenCount           # 用掉多少 token 了
TaskComplete         # 这一轮结束
TurnAborted          # 这一轮被打断了
```

注意 `ExecCommandBegin(call_id, command, cwd)` 带的是**命令本身**，不是一行排好版的文字。
终端界面把它画成带颜色的提示符，`--json` 把它打印成一个对象，测试直接断言字段。
**一旦事件里带的是渲染好的文本，前端就只能有一个了。**

**第 3 步**：`submit` 只做一件事 —— 入队。

```python
async def submit(self, op: Op) -> str:
    sub_id = uuid.uuid4().hex[:8]
    await self.submissions.put(Submission(sub_id, op))
    return sub_id
```

它不等这一轮跑完。它甚至不关心现在有没有一轮在跑。

**第 4 步**：一个消费者，逐条取。

```python
async def _submission_loop(self) -> None:
    while True:
        sub = await self.submissions.get()
        op = sub.op
        ...
```

只有一个消费者，所以不会有两条 Op 同时改同一份状态。而它**从不阻塞在正在跑的那一轮上** ——
这是下面三件事能成立的前提。

**第 5 步**：一轮跑在一个 task 里，不是跑在这个循环里。

```python
task = asyncio.create_task(sess.run_turn(sub.id, op.text))
sess.active = ActiveTurn(sub.id, uuid.uuid4().hex[:12], task)
```

`asyncio.create_task` 是「让它自己在后台跑，我继续往下走」。
所以 `_submission_loop` 立刻回到 `await self.submissions.get()`，可以接着收下一条 Op。

**关键点：一个 task 可以被取消，一个函数调用不行。**

**第 6 步**：中断，就是取消那个 task。

```python
elif isinstance(op, Interrupt):
    active = sess.active
    if active and not active.task.done():
        active.task.cancel()
    else:
        sess.emit(sub.id, TurnAborted("no active turn"))
```

这一轮不需要「同意」停下，也不需要跑到某个检查点。

**第 7 步**：插话。用户在一轮跑着的时候又说了一句话 —— **不开新的一轮**。

```python
if sess.active is not None and not sess.active.task.done():
    # 已经有一轮在跑：去引导它，而不是再开一轮。
    # 这就是这条队列存在的全部理由。
    sess.pending_input.append(op.text)
    sess.emit(sub.id, UserMessageQueued(op.text))
    continue
```

那句话被存在 `pending_input` 里，然后在**下一个 step 边界**并进 history：

```python
while True:
    # step 边界：模型思考时你打的字，在下一次请求之前并进历史
    for queued in self.drain_pending_input():
        self.record_user_text(queued)

    ... 发请求 ...
```

「step 边界」= 当前工具跑完了、下一次模型调用还没发出去的那个位置。
所以模型在**下一次请求里**就能看到你的纠正。

为什么不直接开第二轮？因为两轮会在同一份 history 上竞争，产出交错的工具调用打在同一个工作目录上。
**一轮加一条用户消息，只是一次正常对话。**

---

## 一个细节：模型流必须桥接到事件循环上

第 6 步说「取消那个 task」。但如果这一轮正卡在一个阻塞的网络读上，取消就落不了地 ——
Python 只能在 `await` 的位置切走。

所以 s01 那条流 —— 它来自一个你必须坐在那儿干等的连接 —— 被包了一层。
（**事件循环**是 Python 给异步代码用的调度器：决定哪个暂停中的 `await` 下一个继续跑。）

```python
async def _astream(client: ModelClient, **kwargs: Any) -> AsyncIterator[ResponseEvent]:
    """把阻塞式 SSE 迭代器桥接到事件循环上。

    重点不是那个线程 —— 重点是每一次 yield 都变成了一次 `await`，
    所以 `Op.Interrupt` 能在两个 chunk 之间取消这一轮。
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def produce() -> None:                       # 在后台线程里跑阻塞的读
        for event in client.stream(**kwargs):
            loop.call_soon_threadsafe(queue.put_nowait, event)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=produce, daemon=True).start()
    while True:
        item = await queue.get()                 # <-- 每个 chunk 都是一次 await
        if item is None:
            return
        yield item
```

真实的 codex 用 tokio 的 `CancellationToken` 得到同一个性质。写法不同，要的东西一样：
**取消要能落在两个数据块之间，而不是等整个响应结束。**

---

## 中断必须写进对话

```python
except asyncio.CancelledError:
    self.history.append(user_item("[turn interrupted by user]"))
    self.emit(sub_id, TurnAborted("interrupted"))
    raise
```

少了中间这行会怎样？

被中断时，history 里可能已经有一个 `function_call`，但它的 `function_call_output` 还没来得及写。
下一次请求带着这样一段历史发出去，**接口会直接判定这段对话畸形**（一个调用没有配对的结果）。
就算不报错，模型看到的也是「那条命令还在跑」。

所以中断这件事本身，必须成为对话里的一个事实。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `Op` / `Submission` | 调用方可以要求什么 |
| `Event` / `EventMsg` | 线程回报什么 |
| `CodexThread` | 两条队列，加 `submit()` / `next_event()` |
| `_submission_loop` | 单消费者；从不阻塞在正在跑的一轮上 |
| `Session.pending_input` | 插话队列 |
| `_astream` | 一条必须干等的流 → 一条中途可以取消的流 |
| `_render` | 一个前端。它只做一件事：读事件，打印 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 怎么调用 | 调一个函数然后干等 | 提交一个 `Op`，读 `Event` |
| 怎么叫停 | Ctrl-C 杀掉整个进程 | `Op.Interrupt` 取消一个 task |
| 跑到一半纠正它 | 做不到 | 排进队列，在下一个 step 边界并入 |
| 前端数量 | 只能一个 | 任意多个，读同一条流 |

---

## 试一下

```bash
python s02_protocol/code.py            # 交互模式
```

然后做这三件事：

1. 输入 `找出这个仓库里最大的三个文件`，**在它跑的时候**再输入一句 `只看 .py 文件`。
   你会看到 `[queued for the running turn: ...]`，而且**没有**第二个 `TaskStarted`。
2. 让它跑一件慢的事（`统计每个文件有多少行，一个一个数`），然后输入 `/interrupt`。
3. 输入 `/quit`。

**观察重点**：第 1 题里数一数 `[turn complete]` 出现了几次 —— 应该只有一次。
你插的那句话没有开启新的一轮，它被塞进了同一轮的 history 里。

---

## 一层之上：对外的表面更粗

上面这些 `Op` / `Event` 是**内部**协议。别的程序真正消费的那一层更粗一些：
`codex exec --json` 和 app-server 说的是 **Thread / Turn / Item** 这套词汇 ——
`thread.started`、`turn.started`、`item.completed`、`turn.completed`。

[s15](../s15_harness/) 实现那次翻译。它存在的理由是：
**内部事件名可以随时改，而一份已经发布出去的 schema 不行。**

---

## 对应真实源码

- `codex-rs/protocol/src/protocol.rs` —— `Op`、`Event`、`EventMsg` 三个枚举
- `codex-rs/core/src/session/handlers.rs` —— `submission_loop`
- `codex-rs/core/src/session/input_queue.rs` —— pending input 与插话

---

## 接下来

队列负责搬运 Op。但一轮开始时，有一堆东西必须定下来：在哪个目录跑？用哪个模型？
能不能写文件？

而且 —— 用户完全可以在一轮跑到一半时去改这些设置（s02 刚刚让这件事变得可能）。

[s03](../s03_turn_context/) 讲一轮开始时冻结了什么，以及**模型是怎么知道自己在哪的**。
