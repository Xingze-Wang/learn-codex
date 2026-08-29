# learn-codex

**用重建的方式读懂 [openai/codex](https://github.com/openai/codex)：十五个机制，十五个能跑的文件。**

[English](README.md) · [中文](README-zh.md)

> **没写过代码？** 先看 **[开始之前](PRIMER-zh.md)** —— 二十分钟，
> 之后这里每一段代码你都能读懂。它不预设任何前置知识。

---

Codex 的 agent loop 大约三十行。把对话发过去，执行返回的 `function_call`，再发一次，
没有返回就停。你一个下午就能写完，这个仓库也确实这么做了，见 [s01](s01_agent_loop/)。

其余的一切——`codex-rs` 里剩下的 99%——回答的是循环本身不回答的三个问题：

```
这东西被允许做什么？            沙箱、审批、exec policy、hooks
它知道什么，又忘了什么？        instructions、AGENTS.md、skills、压缩、rollout
谁在看着，怎么打断它？          submission/event 协议，以及跑在它上面的四个前端
```

这就是 harness **本身**。Codex 是一个好标本，因为它的答案格外清晰可读：
用真实的操作系统沙箱而不是黑名单，用只追加的日志而不是隐藏状态，
用一道协议缝而不是把 UI 焊进循环里。

这里每一章拿走其中一个机制，讲清它为什么存在、没有它会坏在哪，
然后用一个独立可运行的 Python 文件把它实现出来。每一章都注明它来自哪些 Rust 文件。

---

## 章节

| | 章节 | 一句话 |
|---|---|---|
| [s01](s01_agent_loop/) | Agent Loop | *"一个循环，一个 shell。"* |
| [s02](s02_protocol/) | Submission / Event 协议 | *"调用方不调用 agent，它提交 Op，然后读 Event。"* |
| [s03](s03_turn_context/) | TurnContext 与 world state | *"一轮开始时把设置冻住，然后只把变化告诉模型。"* |
| [s04](s04_tool_registry/) | 工具注册表 | *"按轮组装，按名字分发，永远不抛异常。"* |
| [s05](s05_apply_patch/) | apply_patch | *"一份 patch 是对文件当前内容的断言。断言不成立就拒绝。"* |
| [s06](s06_unified_exec/) | Unified exec | *"提前带着 session id 返回，而不是拖到超时。"* |
| [s07](s07_sandbox/) | 沙箱 | *"去问内核，别去猜命令字符串。"* |
| [s08](s08_approval/) | 审批与提权 | *"先在沙箱里跑。只有沙箱说不，才去问人。"* |
| [s09](s09_exec_policy/) | Exec policy | *"审批疲劳本身就是一种安全失效。"* |
| [s10](s10_rollout/) | Rollout | *"只追加，不重写。resume 和 fork 就顺手白拿了。"* |
| [s11](s11_compaction/) | 上下文压缩 | *"在那次会失败的请求之前压缩，而不是之后。"* |
| [s12](s12_instructions/) | Instructions、AGENTS.md、skills | *"总是适用的指令就加载；有时适用的只挂个牌子。"* |
| [s13](s13_mcp/) | MCP | *"一个编码 agent 没有理由自带一个 Jira 客户端。"* |
| [s14](s14_hooks/) | Hooks | *"hook 返回的一切都只是建议，除了 `deny`。"* |
| [s15](s15_harness/) | Harness | *"当这些机制能组合起来时，你得到的就是一个 harness。"* |

## 那个循环，供参考

```python
while True:
    calls = []
    for event in client.stream(instructions=..., input_items=list(history), tools=tools):
        if isinstance(event, OutputItemDone):
            history.append(event.item)                      # message、reasoning、call 一视同仁
            if event.item.get("type") == "function_call":
                calls.append(event.item)

    if not calls:
        return                                              # 本轮结束

    for call in calls:
        history.append({"type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": dispatch(call)})
```

十四章围绕它展开，没有一章去改它。

## 快速开始

```bash
git clone <本仓库> && cd learn-codex
pip install -r requirements.txt
cp .env.example .env      # 然后填进 OPENAI_API_KEY，或者直接 export
```

有七章根本不需要 API key——它们是纯机制，现在就能跑
（外加 s15 的 dry run：把一切接好，一个都不调用）：

```bash
python s05_apply_patch/code.py --demo        # 解析并应用一份 patch
python s06_unified_exec/code.py --demo       # 一个能对话的活 PTY 会话
python s07_sandbox/code.py --demo            # 真实的 seatbelt 强制执行（macOS）
python s09_exec_policy/code.py               # 规则引擎跑一批样例命令
python s10_rollout/code.py --demo            # 记录、列表、resume、fork
python s13_mcp/code.py --demo                # 真的 MCP 客户端 + 服务端
python s14_hooks/code.py --demo              # hook 在一轮里逐个触发
python s15_harness/code.py --dry-run         # 整个 harness 接好，但一个都不调用
```

其中三章会读你**真实的** Codex 安装：

```bash
python s10_rollout/code.py --list ~/.codex   # 你真实的会话
python s12_instructions/code.py --show .     # 你真实的 AGENTS.md 和 skills
python s14_hooks/code.py --show              # 你真实的 hooks.json
```

有 key 之后，每一章都能真跑：

```bash
export OPENAI_API_KEY=...
python s01_agent_loop/code.py "统计当前目录下有多少个 python 文件"
python s15_harness/code.py "这个仓库是干嘛的？"
```

## 测试

```bash
python -m pytest tests -q
```

143 个测试，不需要 API key，不需要联网。测试是文档的另一半：
每一个都点名了一件"会坏掉的具体事情"。`tests/test_s07_sandbox.py` 跑的是真沙箱，
`tests/test_s13_mcp.py` 起的是真 MCP server。

## Codex 和 Claude Code 到底差在哪

大多数人来到这里之前都用过其中一个，所以值得把公开讨论反复落到的那几个差别说清楚。
反复出现、而且这个仓库也认同的一句话是：**Codex 在内核里强制，Claude Code 在 harness 里强制。**

| | Codex | Claude Code |
|---|---|---|
| 默认强制点 | 默认就开着操作系统沙箱（seatbelt / Landlock + seccomp）；只有内核拒绝时才问用户（[s07](s07_sandbox/)、[s08](s08_approval/)） | 主要层是 harness 里评估的权限规则，底下可以再叠操作系统沙箱 |
| 不问也能决定 | 一份 Starlark 规则文件：`prefix_rule(pattern=[...], decision="allow"\|"prompt"\|"forbidden")`（[s09](s09_exec_policy/)） | settings 里的 allow/deny 规则，按工具和参数匹配 |
| 改文件 | 一个 freeform 的 `apply_patch`，文法在解码阶段就被强制（[s05](s05_apply_patch/)） | 带 old-string/new-string 参数的 `Edit` / `Write` 类型化工具 |
| 跑命令 | `exec_command` 打开一个活过本次调用的 PTY 会话，用 `write_stdin` 继续（[s06](s06_unified_exec/)） | `Bash`，支持后台执行与输出轮询 |
| 项目指令 | `AGENTS.md`，从项目根拼接到 cwd（[s12](s12_instructions/)） | `CLAUDE.md`，支持 import |
| 会话状态 | 只追加的 JSONL rollout，支持 resume 与 fork（[s10](s10_rollout/)） | transcript 加 `/resume` |
| 对外表面 | 一个 Op/Event 内核后面挂四个前端——TUI、`exec --json`、app-server、MCP server（[s02](s02_protocol/)、[s15](s15_harness/)） | CLI、Agent SDK、hooks |

正在**收敛**而不是发散的部分：**MCP**、**hooks**（JSON 线上格式几乎一模一样——
`hookSpecificOutput.permissionDecision`）、**上下文压缩**、**skills**、**plan/todo 工具**，两边都有。

Codex 那一列来自开源的 `codex-rs`，也就是本仓库每一章引用的东西。
Claude Code 那一列来自它的公开文档和可观察行为；它不开源，所以**那一列的证据强度更弱**，请据此看待。

别人是怎么讲这件事的，可作背景阅读：
[Inside the Agent Harness](https://medium.com/jonathans-musings/inside-the-agent-harness-how-codex-and-claude-code-actually-work-63593e26c176) ·
[awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering) ·
[The Anatomy of an Agent Harness](https://www.langchain.com/blog/the-anatomy-of-an-agent-harness) ·
[Top Agent Harnesses](https://aimultiple.com/agent-harness)

## 怎么读

每一章都是同一个形状，而且**不预设你懂任何前置知识**：

```
问题        一件会坏掉的具体事情，用大白话讲
先理解 X    在看代码之前你需要的那一个概念
            （PTY 是什么、操作系统沙箱是什么、JSON-RPC 是什么……）
解决方案    一张图，加一张「信号 -> 含义 -> 动作」的表
工作原理    第 1 步…第 N 步，每步几行代码加一句「为什么」
试一下      确切的命令，以及跑起来该看哪里
接下来      这一章留下的痛点，也就是下一章
```

各章是独立的。每个 `code.py` 都能单独跑，并且自带它需要的那份内核，
所以你可以不读 s08 直接打开 s09。

s15 是例外：它 import 其它章节，因为组合就是它的主题。

如果不打算从头读到尾，建议按主题挑：

- **它凭什么算 agent** —— s01、s02、s04
- **它凭什么安全** —— s07、s08、s09
- **它凭什么活得下来** —— s10、s11
- **它凭什么可扩展** —— s12、s13、s14

遇到不认识的词，[开始之前](PRIMER-zh.md) 里有一张全部术语的对照表。

## 项目结构

```
learn-codex/
  s01_agent_loop/
    README.md            # 英文
    README.zh.md         # 中文
    code.py              # 独立、可运行
  ...
  s15_harness/           # import 其它章节
  tests/                 # 143 个离线测试
```

## 关于范围，说实话

`codex-rs` 是一个用 Rust 写的大型生产系统，这里是约 7000 行 Python 加约 1800 行测试。
它是一份**阅读辅助**，不是重新实现。凡是简化的地方，章节里都会说明；
凡是承重的细节——沙箱根路径的 `realpath`、`store: false` 与加密 reasoning、
没被回答的 `call_id`、PTY 子进程退出时那个 `EIO`——都保留了下来，
因为**恰恰是这些细节决定了机制成不成立**。

每一章结尾都列出了它所依据的 `codex-rs` 路径。下一步就去读那些。

## 致谢

本仓库的结构——一章一个机制、一句格言、一个可运行文件——沿用自
[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)。

Codex © OpenAI，Apache-2.0。本仓库是对其公开源码的独立学习笔记。
