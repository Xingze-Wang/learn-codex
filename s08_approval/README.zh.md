# s08：审批与提权

[English](README.md) · [中文](README.zh.md)

[s07](../s07_sandbox/) → `s08` → [s09](../s09_exec_policy/)

> *"先在沙箱里跑。只有沙箱说不，才去问人。"*

---

Codex 不会在跑命令之前先问。它先在沙箱里跑，只有沙箱挡住了才问：

```
1. 评估    -- 可以自动批准、需要人、还是直接拒绝？
2. 执行    -- 在沙箱里
3. 被拒？  -- s07 的那个启发式命中
4. 询问    -- 发出 ExecApprovalRequest，然后 await 一个还没到达的 Op
5. 重试    -- 获批后，关掉沙箱再跑一次
6. 记住    -- 同一条命令不会被问第二次
```

**顺序就是设计。** 先问意味着每个 `ls` 都要弹一次窗；沙箱让"直接跑"成为安全的默认值，
于是能抵达用户的问题，只剩下内核真正提出来的那些。

## 第 4 步就是 s02 存在的理由

```python
async def _ask(self, sub_id, call_id, cmd, cwd, reason, justification) -> str:
    """把这一轮挂在一个 future 上。答复会作为另一个 Op 到来。"""
    future = asyncio.get_running_loop().create_future()
    self.pending_approvals[call_id] = future
    self.emit(sub_id, ExecApprovalRequest(call_id, cmd, cwd, reason, justification))
    return await future
```

这一轮是一个挂在 future 上的协程。答复稍后作为同一条队列上的另一条 submission 到达：

```python
elif isinstance(op, ExecApproval):
    # 全部的把戏就在这里：一个对"这一轮仍在等待的问题"的答复，
    # 走的是和其它一切完全相同的那条队列。
    if not sess.resolve_approval(op.call_id, op.decision):
        sess.emit(sub.id, ErrorEvent(f"no pending approval for {op.call_id}"))
```

没有任何东西被阻塞。问题还在屏幕上时，`Op::Interrupt` 依然有效。
前端想怎么渲染这个问题都行——TUI 对话框、JSON 事件、HTTP 响应——因为它只是一个事件加一条提交。

## 三种策略是三种处境，不是三档严格程度

| 策略 | 行为 |
|---|---|
| `untrusted` | 不在可信列表上的一律先问 |
| `on-request` | 先沙箱跑；只有被沙箱挡住才问（默认） |
| `never` | 从不问；被挡住就是失败，并把原因告诉模型 |

`never` 不是"更不安全"，它是**给 CI 用的模式**——那里根本没有人可以问：

```python
if self.approval_policy == NEVER:
    return (
        f"Process exited with code {result.exit_code}\n"
        "The sandbox blocked this command and approvals are disabled.\n"
        f"Output:\n{result.output}"
    )
```

**把失败的原因告诉模型才是重点。** "Permission denied" 会诱发重试循环；
"沙箱挡住了它，而且审批被禁用"则告诉模型该另找一条路，或者如实报告自己进行不下去。

## 记住

```python
if cmd in approved:
    # 本会话已经批过了：不再问，直接不带沙箱跑。
    return AutoApprove(sandboxed=False)
```

`approved_for_session` 是让这套东西可用的关键。一次需要联网的构建不是一个问题，
而是每次尝试各一个问题；同一个问题被问六遍之后，用户就不再读它了。
Codex 还支持把这个决定持久化成一条 execpolicy 规则——那是 s09。

## "没有可用沙箱"意味着什么

```python
if approval_policy == NEVER:
    return Reject("no sandbox available and approval policy is `never`")
return AskUser("no sandbox available on this platform")
```

在没有沙箱的平台上，"跑跑看"不再是安全的默认值，于是只能由策略来决定，而不是内核。
在 `never` 下没有人可以决定，那就拒绝执行，而不是不设防地跑。
当沙箱和人**同时缺席**时，fail closed 是唯一站得住的选择。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `assess_command_safety` | `AutoApprove` / `AskUser` / `Reject` |
| `Session._exec_with_approval` | 上面那六步 |
| `_ask` / `resolve_approval` | 挂在一轮里的 future，由一个 Op 来兑现 |
| `approved` | 会话级的审批缓存 |

## 跑起来

```bash
python s08_approval/code.py "往我的 home 目录里写一个文件"
python s08_approval/code.py --policy never "..."
```

## 对应真实源码

- `codex-rs/core/src/safety.rs` —— `SafetyCheck`
- `codex-rs/core/src/tools/approvals.rs`、`tools/sandboxing.rs`
- `codex-rs/protocol/src/protocol.rs` —— `AskForApproval`、`ReviewDecision`

## 下一章

会问了，但问得太多本身也是一种失败。[s09](../s09_exec_policy/) 讲 Codex 怎么在不问的情况下做决定。
