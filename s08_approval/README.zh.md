# s08: 审批与提权 —— 先跑，被拒了再问

[English](README.md) · [中文](README.zh.md)

[s07](../s07_sandbox/) → `s08` → [s09](../s09_exec_policy/) → ... → [s15](../s15_harness/)

> *"先在沙箱里跑。只有沙箱说不，才去问人。"*
>
> **Harness 层**：人在环中 —— 什么时候该打扰用户。

---

## 问题

[s07](../s07_sandbox/) 的沙箱挡住了一次写操作。但那次写**可能是合理的** ——
模型想装个依赖、想写 `~/.npmrc`、想跑一次需要联网的构建。

现在有个决定要做，而做错哪一边都不行：

- **每条命令都先问用户** → 用户被问烦了，开始闭眼点「同意」。
  这时候你的审批机制**已经不再是安全机制**了。
- **从来不问** → agent 撞到墙就只能放弃，一半的任务做不完。

而且还有第三个约束：**问用户这件事，在 s01 的函数形态里根本做不到。**
一个函数只能 return 一次，它没法「问一句、等你答、然后接着跑」。

---

## 先理解：顺序才是设计

大多数人的直觉顺序是「先问，再跑」。Codex 反过来：

```
1. 评估    -- 可以自动批准、需要人、还是直接拒绝？
2. 执行    -- 在沙箱里
3. 被拒？  -- s07 的启发式命中
4. 询问    -- 发出 ExecApprovalRequest，然后 await 一个还没到达的 Op
5. 重试    -- 获批后，关掉沙箱再跑一次
6. 记住    -- 同一条命令不会被问第二次
```

**为什么「先跑」是安全的？** 因为[s07](../s07_sandbox/)。
沙箱把「跑跑看」的最坏结果压到了「命令失败」。

**这个顺序换来了什么？** 能抵达用户的问题，只剩下**内核真正提出来的那些**。
`ls`、`pytest`、`git status` —— 几百条命令，零次弹窗。

---

## 解决方案

三种审批策略。它们是**三种处境**，不是三档严格程度：

| 策略 | 行为 | 什么时候用 |
|---|---|---|
| `untrusted` | 不在可信列表上的一律先问 | 陌生代码库、第一次跑 |
| `on-request` | 先沙箱跑；只有被沙箱挡住才问 | 默认 |
| `never` | 从不问；被挡住就是失败，并把原因告诉模型 | **CI —— 那里根本没有人可以问** |

`never` 不是「更不安全的模式」，它是**没有人在场时的正确行为**。

---

## 工作原理

**第 1 步**：跑之前先评估。

```python
def assess_command_safety(cmd, *, approval_policy, sandbox_available, approved) -> SafetyCheck:
    if cmd in approved:
        # 本会话已经批过了：不再问，直接不带沙箱跑。
        return AutoApprove(sandboxed=False)

    if approval_policy == UNLESS_TRUSTED and not is_trusted(cmd):
        return AskUser("approval policy is `untrusted`")

    if sandbox_available:
        return AutoApprove(sandboxed=True)          # <-- 最常走的分支

    # 这台机器没有沙箱：跑就是真的有风险，
    # 只能由策略来决定，而不是内核。
    if approval_policy == NEVER:
        return Reject("no sandbox available and approval policy is `never`")
    return AskUser("no sandbox available on this platform")
```

三种返回值：`AutoApprove`（跑）、`AskUser`（问）、`Reject`（不跑，也不问）。

注意最后那一段：**沙箱和人同时缺席时，选择拒绝。** fail closed 是这里唯一站得住的选择。

**第 2 步**：在沙箱里跑（[s07](../s07_sandbox/) 的代码）。

```python
self.emit(sub_id, ExecCommandBegin(call_id, cmd, check.sandboxed))
result = await asyncio.to_thread(run_command, cmd, cwd, sandboxed=check.sandboxed)
```

**第 3 步**：被拒了吗？

```python
if is_likely_sandbox_denied(result):
    ...
```

**第 4 步 —— 这一步是 [s02](../s02_protocol/) 存在的理由。** 问用户，而答案还没到。

```python
async def _ask(self, sub_id, call_id, cmd, cwd, reason, justification) -> str:
    """把这一轮挂在一个 future 上。答复会作为另一个 Op 到来。"""
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    self.pending_approvals[call_id] = future
    self.emit(sub_id, ExecApprovalRequest(call_id, cmd, cwd, reason, justification))
    try:
        return await future                 # <-- 停在这里，但不阻塞任何东西
    finally:
        self.pending_approvals.pop(call_id, None)
```

`await future` 是「这个协程停在这里，直到有人把结果填进去」。
**它不占用线程，不阻塞事件循环** —— 别的 Op 照样能被处理，`/interrupt` 照样有效。

答案后来作为**同一条队列上的另一条提交**到达：

```python
elif isinstance(op, ExecApproval):
    # 全部的把戏就在这里：一个对"这一轮仍在等待的问题"的答复，
    # 走的是和其它一切完全相同的那条队列。
    if not sess.resolve_approval(op.call_id, op.decision):
        sess.emit(sub.id, ErrorEvent(f"no pending approval for {op.call_id}"))
```

```python
def resolve_approval(self, call_id: str, decision: str) -> bool:
    future = self.pending_approvals.get(call_id)
    if future is None or future.done():
        return False
    future.set_result(decision)      # <-- 上面那个 await 在这一刻恢复
    return True
```

前端想怎么渲染这个问题都行 —— TUI 对话框、JSON 事件、HTTP 响应 ——
因为它只是**一个事件加一条提交**。

**第 5 步**：获批就关掉沙箱重跑。

```python
if decision in (DENIED, ABORT):
    return "command not run: the user declined the escalation"
if decision == APPROVED_FOR_SESSION:
    self.approved.add(cmd)
retried = await asyncio.to_thread(run_command, cmd, cwd, sandboxed=False)
```

**第 6 步**：记住。`approved_for_session` 是让这套东西真正可用的关键。

一次需要联网的构建不是一个问题，而是**每次尝试各一个问题**。
同一个问题被问六遍之后，用户就不再读它了 —— 我们又回到了一开始要避免的地方。

---

## `never` 策略下，失败信息本身就是产品

```python
if self.approval_policy == NEVER:
    return (
        f"Process exited with code {result.exit_code}\n"
        "The sandbox blocked this command and approvals are disabled.\n"
        f"Output:\n{result.output}"
    )
```

**把失败的原因告诉模型，才是这几行的重点。**

- 只给 `Permission denied` → 模型会重试、会换一种写法、会绕圈子。
- 给「沙箱挡住了它，而且审批被禁用」→ 模型知道这条路**根本走不通**，
  于是它去找别的办法，或者如实报告自己进行不下去。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `assess_command_safety` | `AutoApprove` / `AskUser` / `Reject` |
| `Session._exec_with_approval` | 上面那六步 |
| `_ask` / `resolve_approval` | 挂在一轮里的 future，由一个 Op 来兑现 |
| `approved` | 会话级的审批缓存 |

---

## 试一下

```bash
python s08_approval/code.py "往我的 home 目录里写一个文件"
```

它会先在沙箱里试、被拒、然后**问你**：

```
! the sandbox blocked this command
  command: echo test > ~/scratch.txt
  allow? [y/N/always]
```

三种答案都试一次，看模型收到的东西怎么变。

再试试没人可问的模式：

```bash
python s08_approval/code.py --policy never "往我的 home 目录里写一个文件"
```

**观察重点**：`never` 模式下不会弹窗，而模型收到的是
`The sandbox blocked this command and approvals are disabled.` ——
然后看它接下来怎么办。好的模型会换个位置写，或者直接告诉你它做不到。

---

## 对应真实源码

- `codex-rs/core/src/safety.rs` —— `SafetyCheck`
- `codex-rs/core/src/tools/approvals.rs`、`tools/sandboxing.rs`
- `codex-rs/protocol/src/protocol.rs` —— `AskForApproval`、`ReviewDecision`

---

## 接下来

现在会问了。但「问」这件事本身还有个问题：

`git status` 被沙箱挡了吗？没有，它只读。`ls` 呢？也没有。
可是 `untrusted` 策略下，它们每一条都要弹一次窗 —— 因为 harness **不知道哪些命令是安全的**。

[s09](../s09_exec_policy/) 给它一份规则文件，让它**不问也能决定** ——
并且让一个组织能说出「`git push --force` 永远不行」，而模型没法跟它讲道理。
