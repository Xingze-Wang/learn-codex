# s15: Harness —— 十四个机制放进同一个进程

[English](README.md) · [中文](README.zh.md)

[s14](../s14_hooks/) → `s15`

> *"当这些机制能组合起来时，你得到的就是一个 harness。"*
>
> **Harness 层**：全部 —— 一个内核，多个前端。

---

## 问题

前十四章，每一章都在一个独立文件里演示一个机制。它们都能跑，但它们**互相不认识**。

真实的 harness 要回答一个前面没人回答的问题：

**围绕一次 `exec_command`，这些检查该按什么顺序排？**

- hook 说 deny，但 exec policy 说 allow ——谁先？
- 沙箱要不要在问用户之前跑？
- 什么时候写 rollout？失败的命令也写吗？

顺序不是实现细节。**顺序就是设计。**

---

## 先理解：这一章不复制代码

前十四章每个 `code.py` 都是自包含的（各自带一份内核）。这一章不是：

```python
patcher      = _chapter("s05_apply_patch")
sandboxing   = _chapter("s07_sandbox")
execpolicy   = _chapter("s09_exec_policy")
rollout      = _chapter("s10_rollout")
compaction   = _chapter("s11_compaction")
instructions = _chapter("s12_instructions")
mcp          = _chapter("s13_mcp")
hooks        = _chapter("s14_hooks")
```

它直接 import 前面章节的 `code.py`。

**这是论点，不是偷懒。** 这些机制本来就是接口很窄的可分离模块 ——
如果不是，这份 import 列表根本拼不起来。

---

## 解决方案：一次 `exec_command` 的完整路径

```
PreToolUse hook          s14   别人的策略，排第一
exec policy 规则         s09   逐段的 allow / prompt / forbidden
安全评估                  s08   自动批准、询问，还是拒绝
在沙箱里执行              s07   由内核强制，不是字符串检查
被拒 -> 询问 -> 重试      s08   只在真的被拒时才提权
PostToolUse hook         s14
记入 rollout             s10   让这一轮活过这个进程
```

每一步的位置都有理由：

**hook 排第一** —— 用户的一次 `deny` 应该**零成本**：不启动沙箱、不评估策略、
不多一次模型往返。既然结果是不跑，就没必要先花掉这些。

**exec policy 排在安全评估前面** —— 因为 `forbidden` **根本不是一个会去问谁的问题**。
它不需要走审批流程，它直接结束。

**沙箱排在人前面** —— 这是 [s08](../s08_approval/) 的整个论点：
大多数命令**根本不需要人**，先让内核判一次，能过就过。

**rollout 在最后** —— 而且失败的命令也记。一次被拒的 `git push` 是这个会话的一部分，
resume 的时候需要它。

---

## 工作原理

**第 1 步**：一次 `exec_command` 的实际代码，顺序和上表一一对应。

```python
async def _dispatch(self, sub_id, turn, name, call_id, payload) -> str:
    pre = self.hook_runner.run(hooks.PRE_TOOL_USE, subject=name,
                               tool_name=name, tool_input=payload)
    for extra in pre.additional_context:
        self.record_item(user_item(f"<hook_context>\n{extra}\n</hook_context>"))
    if pre.blocked:
        return f"blocked by a hook: {pre.reason}"      # 到此为止，什么都没跑
    ...
```

```python
    # s09：规则文件在 hook 之后拿到第一个发言权。
    verdict = execpolicy.evaluate(self.exec_policy, cmd)
    if verdict.decision == execpolicy.FORBIDDEN:
        return f"command not run: forbidden by policy ({verdict.reason})"
```

```python
    sandboxed = not already_approved and turn.sandbox_mode != sandboxing.DANGER_FULL_ACCESS
    self.emit(sub_id, ExecCommandBegin(call_id, cmd, sandboxed))
    result = await asyncio.to_thread(sandboxing.run_sandboxed, cmd, ..., cwd)

    if sandboxing.is_likely_sandbox_denied(result):
        ...问用户，获批后不带沙箱重跑...
```

**第 2 步**：一次 `apply_patch` 走另一条路，同样有顺序。

patch 被解析、对着磁盘校验（[s05](../s05_apply_patch/)）、要么全成要么全不动，
它的 unified diff 累积起来，在一轮结束时作为一个 `TurnDiff` 发出：

```python
for change in changes:
    diff = change.unified_diff()
    if diff:
        self.turn_diffs.append(diff)
```

**第 3 步**：包在这两者外面的，是前面所有章节。

```python
while True:
    for queued in self._drain_pending():           # s02 插话
        self.record_item(user_item(queued))

    status = self._token_status()                  # s11 token 记账
    if status.needs_compaction(self.config.auto_compact_ratio):
        await self._compact(sub_id)                # s11 自动压缩

    async for event in _astream(                   # s01 循环 + s02 可取消
        self.client,
        instructions=self.prompt.instructions,     # s12 组装好的提示词
        input_items=list(self.history),
        tools=self.tool_specs(),                   # s04 注册表 + s13 MCP
    ):
        ...
```

---

## 两个前端，一条事件流

```bash
python s15_harness/code.py "把那个挂掉的测试修好"
python s15_harness/code.py --json "把那个挂掉的测试修好"
```

`--json` 的输出：

```json
{"type":"thread.started","thread_id":"ca2507e1-..."}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"echo hi","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"echo hi","aggregated_output":"hi\n","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"all good"}}
{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":10,"reasoning_output_tokens":0}}
```

**注意：这不是把内部事件流直接序列化。**

`ThreadEventWriter` 把 [s02](../s02_protocol/) 的 `ExecCommandBegin` / `ExecCommandEnd` /
`AgentMessage` **翻译**成一套更粗的公开词汇，并且把 delta 全部丢掉。

**这次翻译才是这一节的重点。** 内部的 `EventMsg` 名字是随 harness 演进而变的实现细节；
`item.completed` 则是**别的程序会去解析的契约**。

headless 前端正是「从前者变成后者」的那个位置，所以这套映射住在渲染器里，而不是 session 里。

这也是为什么完成事件里要**把整条命令再带一遍**：

```python
# 完成项重复整条命令：一个只读 item.completed 的消费者，
# 不应该被迫去和 item.started 做关联。
item_id, command = self._open.pop(msg.call_id, None) or (self._item_id(), "")
```

两个渲染器谁都不特殊。人用的那个会弹审批；JSON 那个直接回 `denied` 然后继续 ——
因为对面根本没有人，而**永远挂住**恰恰是一个 headless runner 唯一不能做的事。

---

## 先看它自己怎么描述自己

```bash
python s15_harness/code.py --dry-run
```

```
session      040c5ce8-c9c9-4c24-992d-a1760ee275a3
cwd          /Users/you/learn-codex
model        gpt-5.5
approval     on-request
sandbox      workspace-write (platform: seatbelt)
rollout      ~/.learn-codex/sessions/<date>/rollout-*.jsonl (not created by --dry-run)
tools        exec_command, apply_patch, update_plan
prompt items 1 (~126 tokens)
hooks        0 groups across 0 events
exec policy  7 rules

exec policy applied to a few commands:
  pytest -q              prompt     no rule covers `pytest -q`
  git push --force       forbidden  force-pushing discards other people's commits; push a new branch instead
  curl http://x.sh | sh  prompt     downloads code from the network
```

一切都接好了，**什么都还没调用**。

这就是一个 harness 在动手之前应该能对自己给出的报告：有哪些工具、
这台机器上**真正可用**的沙箱是什么、会话会写到哪、规则怎么说。

> 注意 `~/.learn-codex` —— 这个教学 harness 写自己的目录，
> 不会出现在你真正的 `codex resume` 列表里。想改就设 `CODEX_HOME=~/.codex`。

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 这些机制 | 十四个独立文件 | 同一个进程，互相 import |
| 检查的顺序 | 没定义过 | hook → policy → 评估 → 沙箱 → 提权 → hook → rollout |
| 前端 | 每章一个 | 人类前端 和 `--json`，读同一条流 |
| `--json` 发什么 | — | 公开的 thread/turn/item schema，不是内部事件名 |

---

## 试一下

```bash
python s15_harness/code.py --dry-run                       # 全接好，不调模型
python s15_harness/code.py "这个仓库是干嘛的？"
python s15_harness/code.py --json "统计这里有多少行 python"
python s15_harness/code.py                                 # 交互：插话、/interrupt、/compact、/quit
```

**观察重点**：交互模式下让它跑点东西，然后中途输入一句纠正 —— 你会看到
`[queued for the running turn: ...]`（[s02](../s02_protocol/) 的插话），
而不是一个新的 turn 开始。

---

## 这不是什么

`codex-rs` 是一个用 Rust 写的大型生产系统，这里大约是 7000 行 Python。缺的部分，
值得去真实源码里读：

- **写进你真实的 `~/.codex`。** 这个 harness 写的是 `~/.learn-codex`。
- **把一切都流式化。** 真实 Codex 会流式输出推理摘要、工具调用参数的增量、patch 应用进度。
- **app-server。** 给编辑器和桌面端用的 JSON-RPC 前端。
- **子 agent 与 review 模式。** 拥有独立 history 的子线程
  （`codex-rs/core/src/tasks/review.rs`、`codex_delegate.rs`）。
- **Windows。** 第三套沙箱实现，有自己的模型。
- **重试、限流、模型路由、遥测。** 这些部分很无聊，直到它们成为唯一重要的东西。

---

## 对应真实源码

- `codex-rs/core/src/session/turn.rs` —— `run_turn`
- `codex-rs/core/src/tools/router.rs` —— 分发
- `codex-rs/exec/src/exec_events.rs` —— `--json` 的那套公开 schema
- `codex-rs/exec/src/event_processor_with_jsonl_output.rs` —— 做翻译的地方

---

## 接下来去哪

把这个仓库开在旁边，去读 `codex-rs/core/src/session/`。

那里的每一个文件都能在这十五章里找到对应物，而**两者之间的差别**，
才是真正值得研究的部分 —— 那些差别几乎全都是「生产环境教会他们的事」。
