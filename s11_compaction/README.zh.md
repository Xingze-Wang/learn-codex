# s11: 上下文压缩 —— 在那次会失败的请求之前

[English](README.md) · [中文](README.zh.md)

[s10](../s10_rollout/) → `s11` → [s12](../s12_instructions/) → ... → [s15](../s15_harness/)

> *"在那次会失败的请求之前压缩，而不是之后。"*
>
> **Harness 层**：上下文管理 —— 有限的窗口怎么服务一个长任务。

---

## 问题

到目前为止每一章的 `history` 都在无限增长。一个跑了两小时的会话里躺着：

- 几十份 `pytest` 输出，每份几千行
- 几百次文件读取的完整内容
- 模型每一轮的 reasoning

然后有一次请求会撞上模型的上下文上限，接口报错，**整件事停在那里** ——
停在任务进行到一半的时候。

---

## 先理解：上下文窗口是什么

可以把它想成模型手上的**一张固定大小的草稿纸**。

每次你发请求，整个 `history` 都要重新写到这张纸上（[s01](../s01_agent_loop/) 的
`store: false` 决定了这一点）。模型读完这一整张纸，才开始想下一步。

纸的大小是固定的（比如 27 万 token）。内容超了，接口直接拒绝请求。

而在编程任务里，**占地方最多的几乎永远是工具输出**：

- 读一个长文件，整个文件内容进纸；
- 一次测试或构建，几十 KB 文本进纸；
- 搜索多个文件，结果不断追加。

任务跑得越久，纸就越满。

---

## 解决方案

**在下一次请求之前**检查，超了就先压缩：

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

**为什么不能等报错再处理？** 因为那时你正处在任务中途，那次请求反正也得丢掉重建 ——
而且此时上下文里可能有一半是刚刚跑完、还没被读过的关键结果。
在**边界处**主动压缩，比在**任意一点**被动崩溃可控得多。

阈值是窗口的一个比例：

```python
AUTO_COMPACT_RATIO = 0.80

def needs_compaction(self, ratio: float = AUTO_COMPACT_RATIO) -> bool:
    return self.used >= self.window * ratio
```

留 20% 不是浪费，是**给这一次压缩本身留的余量** —— 压缩也要发一次请求。

---

## 工作原理

压缩 = 让模型总结自己 + 重建 history。分四步。

**第 1 步**：决定什么留、什么丢。

| | |
|---|---|
| **留** | 会话前缀（instructions、environment）—— 很便宜，而且没了它 agent 就迷路了 |
| **留** | 最近的用户消息，从新到旧直到 token 预算用完 |
| **留** | 一条摘要，且被打上标记 |
| **丢** | 所有工具输出、所有 reasoning、所有中间步骤 |

被丢掉的那部分占了 **90% 的 token，却几乎不含价值**：
一份 4000 行的 `pytest` 日志，真正重要的只是「test_auth 里有三个用例挂了」这一句。

而写摘要的，正是**刚刚做完这些事的那个模型** —— 所以它知道那一句是哪一句。

**第 2 步**：让模型总结自己。注意这次调用**不带工具**。

```python
def request_summary(client, history) -> str:
    """在同一份 history 上的一次独立模型调用。不给工具：
    它的任务是总结，不是继续干活。"""
    prompt = [*history, user_item(SUMMARIZATION_PROMPT)]
```

把工具留着会怎样？一个正干到一半的模型会做最自然的事：**再跑一次 `pytest`**。

提示词要的东西也很具体（这是 Codex 的原文）：

```
Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue
```

**决定、约束、下一步 —— 不是「发生过什么」的叙事。**
一份读起来像 changelog 的摘要，对那个必须接着干活的模型毫无用处。

**第 3 步**：预算从新往旧分配。

```python
for message in reversed(user_messages):          # 从最新的开始
    if remaining <= 0:
        break
    cost = approx_tokens(message)
    if cost <= remaining:
        selected.append(message)
        remaining -= cost
    else:
        selected.append(message[: remaining * CHARS_PER_TOKEN])   # 塞不下就截断
        break
selected.reverse()
```

**为什么从新往旧？** 如果只塞得下一条用户消息，那必须是 agent **此刻正在处理**的那一条，
而不是会话最开始说的那句。

差一点塞不下的那条**截断而不是丢掉** —— 半句「另外老接口要保持可用」仍然带着那个约束。

**第 4 步**：给摘要打上标记。

```python
SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary "
    "of its thinking process. ... use the information in this summary to assist "
    "with your own analysis:"
)
```

这个前缀干**两件**事：

1. 告诉模型它在读什么 —— **这是一次交接，在它上面继续，别重做一遍**。
2. 让**下一次**压缩能认出这是一条既有摘要：

```python
def is_summary_item(item) -> bool:
    return (item.get("type") == "message"
            and item.get("role") == "user"
            and _text_of(item).startswith(SUMMARY_PREFIX))
```

没有第 2 点会怎样？下一次压缩会把上一条摘要当成「用户说的话」重新收集一遍，
于是你得到**摘要的摘要的摘要**，每一层都更模糊。

---

## 压缩是有损的，而且要说出来

假装它无损，正是 agent **悄悄忘掉约束**的方式。

摘要提示词专门点名要「约束和偏好」，因为那恰恰是「描述发生了什么」型摘要会漏掉的东西 ——
而它们也恰恰是用户三轮之后会发现 agent 违反了的东西。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `TokenStatus` | 已用、剩余、阈值 |
| `collect_user_messages` | 真正的用户轮；不含注入块，也不含旧摘要 |
| `session_prefix` | 值得保留的注入块 |
| `build_compacted_history` | 前缀 + 按预算保留的用户轮 + 摘要 |
| `request_summary` | 那次不带工具的模型调用 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| history 大小 | 无限增长 | 被压在窗口的一个比例以内 |
| 撞上上限 | 请求报错，任务停在那里 | 提前压缩，这一轮继续 |
| 丢掉什么 | — | 工具输出、reasoning、中间步骤 |
| 留下什么 | — | 前缀 + 最近的用户轮 + 一条摘要 |
| 谁写摘要 | — | 刚做完这些活的那个模型，且不给它工具 |

---

## 试一下

先看一次**不调 API** 的重建演示：

```bash
python s11_compaction/code.py --explain
```

```
history: 18 items, ~3947 tokens
status: 99% of a 4k window -> needs compaction: True

rebuilt: 4 items, ~274 tokens  (6% of before)
  [user] <environment_context>...
  [user] port the auth module to the new session API...
  [user] also keep the old endpoint working...
  [summary] Another language model started to solve this problem...
```

**观察重点**：18 项变 4 项，token 降到 6%。而**两条用户消息一条都没丢** ——
被丢掉的全是 reasoning 和工具输出。

想看真实的自动压缩，把窗口调小逼它发生：

```bash
python s11_compaction/code.py --window 8000 "把这个仓库里每个 python 文件的行数都数一遍"
```

你会在中途看到 `[auto-compacted: 8200 -> 900 tokens]`，然后它**继续干活** —— 你不用管。

---

## 对应真实源码

- `codex-rs/core/src/compact.rs` —— `build_compacted_history`
- `codex-rs/prompts/templates/compact/prompt.md`、`summary_prefix.md`
- `codex-rs/core/src/session/context_window.rs`

---

## 接下来

十一章了，`BASE_INSTRUCTIONS` 一直是一个硬编码的字符串。

真实会话里，提示词是从四个来源组装出来的 —— 而且每个来源走**哪条通道**都是刻意的。
更重要的是：一个仓库怎么告诉 agent「这里的规矩是什么」？

[s12](../s12_instructions/) 讲 `AGENTS.md` 是怎么被**发现**的（不是配置的），
以及 skills 用的那个反过来的招。
