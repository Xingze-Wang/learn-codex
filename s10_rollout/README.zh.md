# s10：Rollout —— 把会话写成一个只追加的文件

[English](README.md) · [中文](README.zh.md)

[s09](../s09_exec_policy/) → `s10` → [s11](../s11_compaction/)

> *"只追加，不重写。resume 和 fork 就顺手白拿了。"*

---

到目前为止的一切都活不过进程退出。Codex 会边跑边把每个会话写成 JSONL：

```
~/.codex/sessions/2026/05/23/rollout-2026-05-23T18-18-36-<thread-id>.jsonl
```

日期路径不是装饰：`codex resume` 列出昨天的会话，靠的是打开**一个目录**，
而不是把磁盘上每个文件都读一遍。

## 四种行类型，以及一处重要的切分

```
session_meta    只有一次，第一行：id、cwd、cli 版本、instructions
turn_context    每轮一次：cwd、审批策略、沙箱策略、模型
response_item   resume 时回放给模型的东西
event_msg       用户当时看到的东西
```

```json
{"timestamp":"2026-05-23T10:18:47.419Z","type":"session_meta","payload":{"id":"019e5458-...","cwd":"/Users/you","cli_version":"0.128.0"}}
{"timestamp":"2026-05-23T10:18:57.334Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"sed -n '1,220p' ...\"}","call_id":"call_ZX8V..."}}
{"timestamp":"2026-05-23T10:18:57.391Z","type":"event_msg","payload":{"type":"exec_command_end","call_id":"call_ZX8V...","exit_code":0}}
```

`response_item` 行重建的是**模型的视角**，`event_msg` 行重建的是**人的视角**。
渲染一个恢复的会话两者都要，回放给模型只需要前者。这就是它们是两种类型、
而不是一条流加一个标志位的原因——这个切分在读的时候是承重的，所以写的时候就得存在。

（`code.py --show` 能直接读真实的 `~/.codex` rollout 文件，上面就是它解析的格式。）

## 不是什么都往里写

```python
PERSISTED_EVENTS = {"task_started", "task_complete", "user_message", "agent_message",
                    "exec_command_begin", "exec_command_end", "token_count", ...}
```

delta 不在这个集合里。一轮会发出成千上万条 `agent_message_delta` 和一条 `agent_message`；
把碎片也写进去，只是为了一份已经存在的信息把文件放大几十倍。
同一套过滤器也会丢掉模型在回放时用不上的 response item。

## 每行追加、每行 flush

```python
# 逐行追加并 flush：一轮跑到一半崩掉，不能把这一轮丢掉。
with self.path.open("a", encoding="utf-8") as handle:
    handle.write(line + "\n")
    handle.flush()
```

而读的一侧，从设计上就预期了那次终究会发生的崩溃：

```python
except json.JSONDecodeError:
    # 崩溃会留下写了一半的最后一行。它前面的一切仍然是
    # 一个有效的会话；不许因此把它们全扔了。
    continue
```

最后一行被截断，是一个被杀掉的进程的**正常**终态。因为这个就拒绝加载文件，
等于把它本来要保护的那个会话给扔了。

## resume 与 fork

```python
def resume(path):
    """重建 (history, session meta)。模型看到的还是它当初看到的。"""
    rollout = read_rollout(path)
    return rollout.response_items(), rollout.meta
```

```python
def fork(path, codex_home, *, keep_turns):
    """把前 `keep_turns` 轮拷进一个新线程。

    原文件永远不被修改。原地重写历史意味着：重写过程中一崩，
    两个未来一起没了。"""
```

fork 就是"退回三轮、换个路子试试"的真实含义：两个共享同一前缀的文件。
因为日志是只追加的，旧的那个还在，而且是完整的。

## 不读全文也能列表

```python
def head_summary(path, max_lines=40):
    """够渲染会话选择器里的一行，且不必读整个文件。"""
```

一个长会话是几 MB。一个要展示五十个会话的选择器，不能为了画一张列表去读五十 MB，
所以摘要只从文件头取：meta，然后第一条用户消息，然后停。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `should_persist` | 写入侧的过滤器 |
| `RolloutRecorder` | 创建按日期分层的路径、追加行 |
| `read_rollout` / `Rollout` | 读回来，容忍截断 |
| `resume` / `fork` | 回放，以及分叉 |
| `head_summary` / `list_rollouts` | 会话选择器 |

## 跑起来

```bash
python s10_rollout/code.py --demo
python s10_rollout/code.py --list ~/.codex        # 你自己真实的会话
python s10_rollout/code.py --show <file.jsonl>
```

## 对应真实源码

- `codex-rs/rollout/src/recorder.rs`、`policy.rs`、`list.rs`
- `codex-rs/core/src/session/rollout_reconstruction.rs`

## 下一章

历史持久化了，而且会一直长下去。[s11](../s11_compaction/) 负责腾地方。
