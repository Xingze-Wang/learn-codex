# s15：Harness

[English](README.md) · [中文](README.zh.md)

[s14](../s14_hooks/) → `s15`

> *"当这些机制能组合起来时，你得到的就是一个 harness。"*

---

十四章，十四个机制，每一个都单独演示过。这一章把它们放进同一个进程，
让一轮真实的对话从头到尾穿过它们。

它是唯一一个不重复自己依赖的章节：

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

这是论点，不是偷懒。它们本来就是接口很窄的可分离模块——如果不是，这份 import 列表本身就不成立。

## 检查的顺序就是设计

围绕一次 `exec_command`：

```
PreToolUse hook          s14   别人的策略，排第一
exec policy 规则         s09   逐段的 allow / prompt / forbidden
安全评估                  s08   自动批准、询问，还是拒绝
在沙箱里执行              s07   由内核强制，不是字符串检查
被拒 -> 询问 -> 重试      s08   只在真的被拒时才提权
PostToolUse hook         s14
记入 rollout             s10   让这一轮活过这个进程
```

hook 排第一，是因为用户的一次 `deny` 应该零成本——不启动沙箱、不评估策略、不多一次模型往返。
exec policy 排在安全评估前面，是因为 `forbidden` 根本不是一个会去问谁的问题。
而沙箱排在人前面，是因为 s07 的全部意义就在于：大多数命令根本不需要人。

围绕一次 `apply_patch`：patch 被解析、对着磁盘校验（s05）、要么全成要么全不动，
它的 unified diff 会累积成一份 `TurnDiff`，在一轮结束时发出。

包在这两者外面的：一个 turn context（s03）、一个工具注册表（s04）、MCP 工具（s13）、
token 记账与自动压缩（s11）、由 AGENTS.md 和 skills 组装出的 instructions（s12）——
全部由 submission/event 两条队列驱动（s02）。

## 两个前端，一条事件流

```bash
python s15_harness/code.py "把那个挂掉的测试修好"
python s15_harness/code.py --json "把那个挂掉的测试修好"
```

```json
{"type":"thread.started","thread_id":"ca2507e1-..."}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"echo hi","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"echo hi","aggregated_output":"hi\n","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"all good"}}
{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":10,"reasoning_output_tokens":0}}
```

注意：这**不是**把内部事件流直接序列化。`ThreadEventWriter` 把
`ExecCommandBegin` / `ExecCommandEnd` / `AgentMessage` 翻译成一套更粗粒度的公开词汇——
`thread.started`、`turn.started`、`item.started`、`item.completed`、`turn.completed`——
并且把 delta 全部丢掉。

**这次翻译才是有意思的部分。** 内部的 `EventMsg` 名字是随 harness 演进而变的实现细节；
`item.completed` 则是别的程序会去解析的**契约**。headless 前端正是"从前者变成后者"的那个位置，
所以这套映射住在渲染器里，而不是 session 里。这也是为什么完成事件里要把整条命令再带一遍：
一个只读 `item.completed` 的消费者，不应该被迫去和 `item.started` 做关联。

两个渲染器谁都不特殊。人用的那个会弹审批；JSON 那个直接回 `denied` 然后继续，
因为对面根本没有人，而**永远挂住**恰恰是一个 headless runner 唯一不能做的事。

## Dry run

```bash
python s15_harness/code.py --dry-run
```

```
session      6b856ab0-...
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

一切都接好了，什么都还没调用。这就是一个 harness 在动手之前应该能对自己给出的报告：
有哪些工具、这台机器上真正可用的沙箱是什么、会话会写到哪、规则怎么说。

## 这不是什么

`codex-rs` 是一个大型生产系统，而这里大约是 7000 行 Python。这里没有、值得去真实源码里读的：

- **写进你真实的 `~/.codex`。** 这个 harness 写的是 `~/.learn-codex`，
  所以它不会出现在你真正的 `codex resume` 列表里。想改就设 `CODEX_HOME=~/.codex`。
- **把一切都流式化。** 真实 Codex 会流式输出推理摘要、工具调用参数的增量、patch 应用进度。
  这里只有文本是流式的。
- **app-server。** 给编辑器和桌面端用的 JSON-RPC 前端。
- **子 agent 与 review 模式。** 拥有独立 history 的子线程，把结构化结果返回给父级
  （`codex-rs/core/src/tasks/review.rs`、`codex_delegate.rs`）。
- **Windows。** 第三套沙箱实现，有自己的模型。
- **重试、限流、模型路由、遥测。** 这些部分很无聊，直到它们成为唯一重要的东西。

## 跑起来

```bash
python s15_harness/code.py --dry-run
python s15_harness/code.py "这个仓库是干嘛的？"
python s15_harness/code.py --json "统计这里有多少行 python"
python s15_harness/code.py            # 交互：插话、/interrupt、/compact、/quit
```

## 对应真实源码

- `codex-rs/core/src/session/turn.rs` —— `run_turn`
- `codex-rs/core/src/tools/router.rs` —— 分发
- `codex-rs/exec/src/event_processor_with_jsonl_output.rs` —— `--json`

## 接下来去哪

把这个仓库开在旁边，去读 `codex-rs/core/src/session/`。
那里的每一个文件都能在这十五章里找到对应物，而**两者之间的差别**，才是真正值得研究的部分。
