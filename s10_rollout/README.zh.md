# s10: Rollout —— 把会话写成一个只追加的文件

[English](README.md) · [中文](README.zh.md)

[s09](../s09_exec_policy/) → `s10` → [s11](../s11_compaction/) → ... → [s15](../s15_harness/)

> *"只追加，不重写。resume 和 fork 就顺手白拿了。"*
>
> **Harness 层**：持久化 —— 让一次会话活过一个进程。

---

## 问题

你和 agent 一起干了两个小时。然后你关掉了终端。

**全没了。** 丢掉的不只是聊天记录：

- 它读过哪些文件、试过哪些路、哪条路走不通
- 你告诉过它的约束（「别动 vendor/」）
- 它跑到一半的那个重构

想接着干，你只能从头讲一遍。

于是你说：那就存下来嘛，`json.dump(history)` 不就行了。

**问题在于「什么时候存」。** 存在每轮结束时？那进程崩在一轮中间，这一轮就全没了 ——
而恰恰是长的、慢的、崩得最多的那种轮次，最值得保住。

---

## JSONL 与「只追加」

**JSONL** = 每行一个 JSON 对象的文本文件。

```
{"type": "session_meta", "payload": {...}}
{"type": "response_item", "payload": {...}}
{"type": "event_msg", "payload": {...}}
```

它比一个大 JSON 数组好在哪？

1. **可以往后追加**，不用把整个文件读出来再写回去。
2. **可以只读前几行**（一个会话可能有几 MB，而列表界面只需要头部）。
3. **写到一半崩了，前面的行仍然是完好的**。

「只追加」（append-only）的意思是：**这个文件只往后长，从不修改已经写下的内容**。

Codex 把它放在这里：

```
~/.codex/sessions/2026/05/23/rollout-2026-05-23T18-18-36-<thread-id>.jsonl
```

按日期分层不是装饰：`codex resume` 列出昨天的会话，靠的是**打开一个目录**，
而不是把磁盘上每个文件都读一遍。

---

## 解决方案

四种行类型。其中有一处切分很关键：

```
session_meta    只有一次，第一行：id、cwd、cli 版本、instructions
turn_context    每轮一次：cwd、审批策略、沙箱策略、模型
response_item   resume 时【回放给模型】的东西
event_msg       用户当时【看到】的东西
```

真实文件长这样（这几行来自一个真的 `~/.codex` rollout）：

```json
{"timestamp":"2026-05-23T10:18:47.419Z","type":"session_meta","payload":{"id":"019e5458-...","cwd":"/Users/you","cli_version":"0.128.0"}}
{"timestamp":"2026-05-23T10:18:57.334Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"sed -n '1,220p' ...\"}","call_id":"call_ZX8V..."}}
{"timestamp":"2026-05-23T10:18:57.391Z","type":"event_msg","payload":{"type":"exec_command_end","call_id":"call_ZX8V...","exit_code":0}}
```

**为什么 `response_item` 和 `event_msg` 要分开？**

- 恢复一个会话给**人**看 → 两种都要（要画出当时的命令、输出、耗时）
- 回放给**模型** → 只要第一种（模型不需要知道当时终端上是什么颜色）

这个切分在**读的时候**是承重的，所以在**写的时候**就得存在。

> `python s10_rollout/code.py --show ~/.codex/sessions/.../rollout-xxx.jsonl`
> 能直接读你自己真实的 Codex 会话文件 —— 上面就是它解析的格式。

---

## 工作原理

**第 1 步**：不是什么都往里写。

```python
PERSISTED_EVENTS = {
    "task_started", "task_complete", "user_message", "agent_message",
    "exec_command_begin", "exec_command_end", "token_count", ...
}
```

`agent_message_delta` **不在**这个集合里。一轮会发出成千上万条 delta 和一条完整的
`agent_message`；把碎片也写进去，只是为了一份**已经存在**的信息把文件放大几十倍。

**第 2 步**：每行追加，每行 flush。

```python
# 逐行追加并 flush：一轮跑到一半崩掉，不能把这一轮丢掉。
with self.path.open("a", encoding="utf-8") as handle:
    handle.write(line + "\n")
    handle.flush()
```

这直接回答了开头那个「什么时候存」的问题：**每发生一件事就存一次。**

**第 3 步 —— 读的一侧要预期那次终究会发生的崩溃。**

```python
try:
    line = json.loads(raw)
except json.JSONDecodeError:
    # 崩溃会留下写了一半的最后一行。它前面的一切仍然是
    # 一个有效的会话；不许因此把它们全扔了。
    continue
```

最后一行被截断，是一个**被杀掉的进程的正常终态**。
因为这个就拒绝加载整个文件，等于把它本来要保护的那个会话给扔了。

**第 4 步**：resume —— 把 `response_item` 挑出来，就是模型的视角。

```python
def resume(path):
    """重建 (history, session meta)。模型看到的还是它当初看到的。"""
    rollout = read_rollout(path)
    return rollout.response_items(), rollout.meta
```

就这两行。因为[s01](../s01_agent_loop/) 的 `store: false` 决定了历史归 harness 所有 ——
所以「恢复」只是把一个 list 装回去而已。

**第 5 步**：fork —— 「退回三轮，换个路子试试」。

```python
def fork(path, codex_home, *, keep_turns):
    """把前 `keep_turns` 轮拷进一个新线程。

    原文件永远不被修改。原地重写历史意味着：重写过程中一崩，
    两个未来一起没了。"""
```

fork 的实现就是：新建一个文件，把旧文件的行抄进去，数到第 `keep_turns` 个
`task_started` 就停。

**因为日志是只追加的，旧的那个还在，而且是完整的。** 你得到两条平行的时间线。

**第 6 步**：列表界面 —— 不读全文。

```python
def head_summary(path, max_lines=40):
    """够渲染会话选择器里的一行，且不必读整个文件。"""
```

一个长会话是几 MB。一个要展示五十个会话的选择器，不能为了画一张列表去读五十 MB。
所以摘要只从文件头取：meta，然后第一条用户消息，然后停。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `should_persist` | 写入侧的过滤器 |
| `RolloutRecorder` | 创建按日期分层的路径、追加行 |
| `read_rollout` / `Rollout` | 读回来，容忍截断 |
| `resume` / `fork` | 回放，以及分叉 |
| `head_summary` / `list_rollouts` | 会话选择器 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 会话存在哪 | 只在内存里 | 磁盘上一个 JSONL 文件 |
| 什么时候写 | — | 每一行，立刻 flush |
| 崩溃了 | 全丢 | 最多丢最后写了一半的那一行 |
| 接着昨天干 | 做不到 | `resume` |
| 从第 3 轮换个路子 | 做不到 | `fork`，原文件一个字节不动 |

---

## 试一下

**不需要 API key：**

```bash
python s10_rollout/code.py --demo
```

它会录两轮，打印出整个文件，然后 resume 一次、fork 一次。

**观察重点**：看 demo 输出里的这三行：

```
turns: 2
replayable items: 8
renderable events: 14  (no deltas: dropped by policy)
```

14 个事件 vs 8 个可回放项 —— 这就是那处切分。再看 fork 的结果：

```
fork(keep_turns=1) -> rollout-....jsonl
  forked turns: 1
  original untouched: 2 turns
```

**原文件一个字节都没动。**

然后读你自己的真实会话：

```bash
python s10_rollout/code.py --list ~/.codex
python s10_rollout/code.py --show ~/.codex/sessions/2026/.../rollout-xxx.jsonl
```

---

## 对应真实源码

- `codex-rs/rollout/src/recorder.rs`、`policy.rs`、`list.rs`
- `codex-rs/core/src/session/rollout_reconstruction.rs`

---

## 接下来

历史持久化了。而且它会**一直长下去**。

一个跑了两小时的会话，history 里躺着几十份 `pytest` 输出、几百次文件读取。
终于有一次请求会撞上模型的上下文上限，然后**整件事停在那里**。

[s11](../s11_compaction/) 讲怎么腾地方 —— 关键在于**在那次会失败的请求之前**腾，而不是之后。
