# s11：上下文压缩

[English](README.md) · [中文](README.zh.md)

[s10](../s10_rollout/) → `s11` → [s12](../s12_instructions/)

> *"在那次会失败的请求之前压缩，而不是之后。"*

---

到目前为止每一章的 `history` 都在无限增长，而长会话的结局永远一样：再发一次请求，窗口满了。

Codex 盯着 token 计数，并在下一次请求**之前**压缩：

```
已用 82k / 100k  ->  让模型总结它自己刚做的工作
                 ->  把 history 重建为：前缀 + 最近的用户轮 + 摘要
                 ->  同一轮继续跑，用户全程不介入
```

```python
# 在这里检查——在请求之前，不是在失败之后。
if self.token_status().needs_compaction(self.auto_compact_ratio):
    record = self.compact()
```

改成"等报错再反应"，意味着在最糟的时刻才发现上限——任务进行到一半，
而那次请求反正也得丢掉重建。

## 什么被留下，为什么

| | |
|---|---|
| 留 | 会话前缀（instructions、environment）——很便宜，而且没了它 agent 就迷路了 |
| 留 | 最近的用户消息，从新到旧直到 token 预算用完 |
| 留 | 一条摘要项，且被打上标记，好让下一次压缩认得出它 |
| 丢 | 所有工具输出、所有 reasoning、所有中间步骤 |

被丢掉的那部分占了 90% 的 token，却几乎不含价值：一份 4000 行的 `pytest` 日志，
真正重要的只是"test_auth 里有三个用例挂了"这一句。而摘要是由**刚刚做完这些事的那个模型**写的，
所以它知道那一句是哪一句。

`--explain` 会在一个合成会话上把这笔账算给你看：

```
history: 18 items, ~3947 tokens
status: 99% of a 4k window -> needs compaction: True

rebuilt: 4 items, ~274 tokens  (6% of before)
  [user] <environment_context>...
  [user] port the auth module to the new session API...
  [user] also keep the old endpoint working...
  [summary] Another language model started to solve this problem...
```

## 预算从新往旧分配

```python
for message in reversed(user_messages):
    if remaining <= 0:
        break
    cost = approx_tokens(message)
    if cost <= remaining:
        selected.append(message)
        remaining -= cost
    else:
        selected.append(message[: remaining * CHARS_PER_TOKEN])
        break
```

如果只塞得下一条用户消息，那必须是 agent **此刻正在处理**的那一条，而不是会话最开始说的那句。
差一点塞不下的那条会被截断而不是丢掉，因为半句"另外老接口要保持可用"仍然带着那个约束。

## 摘要请求不带工具

```python
def request_summary(client, history) -> str:
    """在同一份 history 上的一次独立模型调用。不给工具：
    它的任务是总结，不是继续干活。"""
    prompt = [*history, user_item(SUMMARIZATION_PROMPT)]
```

把工具留着，一个正干到一半的模型会做最自然的事：再跑一次 `pytest`。

提示词本身要的东西也是对的：

```
Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue
```

决定、约束、下一步——不是"发生过什么"的叙事。
一份读起来像 changelog 的摘要，对那个必须接着干活的模型毫无用处。

## 给摘要打上标记

```python
SUMMARY_PREFIX = ("Another language model started to solve this problem and produced a summary "
                  "of its thinking process. ...")
```

它干两件事。一是告诉模型它在读什么——这是一次交接，在它上面继续，别重做一遍。
二是让**下一次**压缩能认出这是一条既有摘要，从而不把它当成用户消息再收集一次——
否则你会得到摘要的摘要的摘要。

## 压缩是有损的，而且要说出来

假装它无损，正是 agent 悄悄忘掉约束的方式。
摘要提示词专门点名要"约束和偏好"，因为那恰恰是"描述发生了什么"型摘要会漏掉的东西——
而它们也恰恰是用户三轮之后会发现 agent 违反了的东西。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `TokenStatus` | 已用、剩余、阈值 |
| `collect_user_messages` | 真正的用户轮；不含注入块，也不含旧摘要 |
| `session_prefix` | 值得保留的注入块 |
| `build_compacted_history` | 前缀 + 按预算保留的用户轮 + 摘要 |
| `request_summary` | 那次不带工具的模型调用 |

## 跑起来

```bash
python s11_compaction/code.py --explain             # 只演示重建，不调 API
python s11_compaction/code.py --window 8000 "..."   # 逼出一次真实的自动压缩
```

## 对应真实源码

- `codex-rs/core/src/compact.rs` —— `build_compacted_history`
- `codex-rs/prompts/templates/compact/prompt.md`、`summary_prefix.md`
- `codex-rs/core/src/session/context_window.rs`

## 下一章

提示词已经当了十一章的硬编码字符串。[s12](../s12_instructions/) 把真正的那一份组装出来。
