# s02：Submission / Event 协议

[English](README.md) · [中文](README.zh.md)

[s01](../s01_agent_loop/) → `s02` → [s03](../s03_turn_context/)

> *"调用方不调用 agent，它提交一个 Op，然后读 Event。"*

---

s01 的循环返回一个字符串。这只够写脚本：在它跑的时候，用户没法纠正它、没法叫停它，
它需要一个答复时也没法回答它。

Codex 根本不把循环暴露出来，它暴露的是两条队列：

```
调用方 --Op--> [SQ] --> submission_loop --> turn task --Event--> [EQ] --> 调用方
```

`CodexThread.submit(op)` 把一条 submission 丢进第一条队列并立刻返回，`next_event()` 从第二条读。
Codex 自带的每一个前端——TUI、`codex exec --json`、app-server、MCP server——都只是同一条事件流的
不同读者。没有哪一个是"那个"界面，这也正是它能同时有四个的原因。

## 这个形状带来的三件事

**中断。** 一轮跑在一个 task 里，`Op::Interrupt` 直接取消它：

```python
elif isinstance(op, Interrupt):
    active = sess.active
    if active and not active.task.done():
        active.task.cancel()
```

这一轮不需要"同意"停下，也不需要跑到某个检查点。这正是模型流要被桥接到事件循环上的原因：
每一个 chunk 都是一次 `await`，所以取消发生在两个 chunk 之间，而不是等整个响应结束之后。

**插话（steering）。** 一轮正在跑时输入的消息，不会开启第二轮：

```python
if sess.active is not None and not sess.active.task.done():
    # 已经有一轮在跑：去引导它，而不是再开一轮。
    # 这就是这条队列存在的全部理由。
    sess.pending_input.append(op.text)
```

它会在下一个 step 边界被并入 history——在下一次模型调用之前、当前工具跑完之后。
模型在下一次请求里就能看到这条纠正。两轮在同一份 history 上竞争，产出的是交错的工具调用打在
同一个工作目录上；而一轮加一条用户消息，只是一次正常对话。

**审批。** 一个工具可以停下来，等一个**还没被提交**的答复——因为一轮是协程，不是栈帧。
这就是 s08，它不需要任何新机制。

## 一轮要把自己被中断这件事记下来

```python
except asyncio.CancelledError:
    self.history.append(user_item("[turn interrupted by user]"))
    self.emit(sub_id, TurnAborted("interrupted"))
    raise
```

少了这一行，下一轮的 history 里会出现一个没有 output 的 `function_call`——这在下一次请求里
是协议错误，而且在模型看来就是"那条命令还在跑"。**中断必须成为对话里的一个事实。**

## 事件描述发生了什么，而不是该画什么

```python
TaskStarted, AgentMessageDelta, AgentMessage, ExecCommandBegin, ExecCommandEnd,
UserMessageQueued, TokenCount, TaskComplete, TurnAborted, ErrorEvent, ShutdownComplete
```

`ExecCommandBegin(call_id, command, cwd)` 带的是命令本身，不是一行排好版的文字。
TUI 把它渲染成带颜色的提示符，`--json` 把它打印成一个对象，测试直接断言字段。
一旦事件里带上了渲染好的文本，前端就只能有一个了。

每个 `Event` 都带着触发它的那条 submission 的 `id`，所以同时有多条未决提交的调用方能分清谁是谁的答复。

再往上一层，别的程序真正消费的表面要粗得多：`codex exec --json` 和 app-server 说的是
**Thread / Turn / Item** 这套词汇（`thread.started`、`turn.started`、`item.completed`、
`turn.completed`），而不是这里的内部名字。[s15](../s15_harness/) 实现了那次翻译；
它存在的理由是：内部事件名可以随时改，而一份已发布的 schema 不行。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `Op` / `Submission` | 调用方可以要求什么 |
| `Event` / `EventMsg` | 线程回报什么 |
| `CodexThread` | 两条队列，加 `submit()` / `next_event()` |
| `_submission_loop` | 单消费者；从不阻塞在正在跑的一轮上 |
| `Session.pending_input` | 插话队列 |
| `_astream` | 阻塞式 SSE 迭代器 → 可取消的异步迭代器 |

## 跑起来

```bash
python s02_protocol/code.py "列出这里最大的 3 个文件"
python s02_protocol/code.py            # 然后在它干活时直接打字，或者 /interrupt
```

## 对应真实源码

- `codex-rs/protocol/src/protocol.rs` —— `Op`、`Event`、`EventMsg`
- `codex-rs/core/src/session/handlers.rs` —— `submission_loop`
- `codex-rs/core/src/session/input_queue.rs` —— pending input 与 steering

## 下一章

队列负责搬运 op，而一轮需要自己的设置。[s03](../s03_turn_context/) 讲一轮开始时冻结了什么，
以及模型是怎么被告知这些的。
