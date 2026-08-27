# s14：Hooks

[English](README.md) · [中文](README.zh.md)

[s13](../s13_mcp/) → `s14` → [s15](../s15_harness/)

> *"hook 返回的一切都只是建议，除了 `deny`。"*

---

到目前为止 harness 是写死的：策略住在代码里，只有人去改它才会变。
Hooks 让用户或组织把自己的程序放到 agent 的路径上，而不必 fork 任何东西。

一个 hook 就是一个程序。Codex 往它的 stdin 写 JSON，从它的 stdout 读 JSON：

```json
{"session_id": "...", "cwd": "/repo", "hook_event_name": "PreToolUse",
 "tool_name": "exec_command", "tool_input": {"cmd": "git push"}}
```

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "推送走 CI，不走 agent"}}
```

十一个事件，其中有用的分成两类：

| 事件 | 能做什么 |
|---|---|
| `SessionStart`、`UserPromptSubmit`、`SubagentStart` | 返回 `additionalContext` |
| `PreToolUse` | 返回 `allow` / `deny` / `ask` |
| `PostToolUse`、`Stop`、`SessionEnd`、`PreCompact`、`Interrupt`、`SubagentStop`、`PermissionRequest` | 观察，或补充上下文 |

第一类往对话里写东西，第二类决定某件事**到底发不发生**。

## 失败模式就是这套设计

`deny` 是唯一能拦住 agent 的 hook 结果。其余一切——崩溃、挂死、stdout 上的垃圾——
都降级为"没有这个 hook 照跑，并把这件事说出来"：

```
[context injected at session start] House rule: never edit files under vendor/.
$ pytest -q
[exec_command denied by hook] pushes go through CI, not the agent
[exec_command escalated to the user by hook] recursive delete
$ *** Begin Patch ...

warnings collected along the way (nothing aborted the session):
  PreToolUse: hook timed out after 0.3s: sleep 5
  PreToolUse: hook exited 7: exit 7
```

三个具体选择：

```python
if not stdout:
    return None, "", "", ""     # 没有意见，不是错误
```

什么都不打印的 hook 没有意见。打印了畸形东西的 hook 同样没有意见——
把解析失败当成拒绝，意味着某人脚本里的一个笔误会**静默地**废掉他的 agent。

```python
except subprocess.TimeoutExpired:
    outcome.warnings.append(f"hook timed out after {hook.timeout}s: {hook.command}")
```

超时是强制的：hook 是别人写的子进程，而且它在**每一次**工具调用之前都会跑。

```python
context = str(specific.get("additionalContext") or "")[:limit]
```

注入的上下文受 `additionalContextLimit` 限制。没有它，一个话多的 hook 会悄悄吃掉
s11 拼命想保护的上下文窗口——而且是在每一次请求里，永远。

## Matcher 失败时收紧，不放宽

```python
try:
    return re.search(self.matcher, subject) is not None
except re.error:
    return False
```

非法正则匹配不到任何东西。一个本意是**收窄**规则的 matcher 里的笔误，绝不能反过来把它**放宽**——
`^exec_comand$` 应该静默地什么都不做，而不是对一切生效。

## 第一个 deny 说了算

```python
if decision == DENY:
    # 第一个 deny 说了算，其余跳过：这个工具反正不会跑了，
    # 再去问剩下的 hook 只是噪音。
    return outcome
```

## 它们在一轮里的位置

```
SessionStart      -> additionalContext 进入对话
UserPromptSubmit  -> 可以注入，也可以拦掉这个 prompt
  PreToolUse      -> 逐次调用的 allow / deny / ask
  （工具执行）
  PostToolUse     -> 观察
Stop              -> systemMessage
SessionEnd
```

`code.py` 里的 `turn_with_hooks` 把模型换成桩之后，原样跑这个序列，
所以不用 API key 也能看清每个事件的位置。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `HookConfig.load` | 解析 `hooks.json`，并报告它解析不了的部分 |
| `MatcherGroup.matches` | 对工具名做正则，失败时收紧 |
| `HookRunner.run` | 启动、喂 JSON、超时、收集 |
| `parse_hook_output` | 宽容地解析那个线上格式 |
| `turn_with_hooks` | 每个事件触发的位置 |

## 跑起来

```bash
python s14_hooks/code.py --demo        # 造一份 hooks.json 并把每个事件都打一遍
python s14_hooks/code.py --show        # 读你真实的 ~/.codex/hooks.json
```

## 对应真实源码

- `codex-rs/hooks/` —— `engine/dispatcher.rs`、`engine/command_runner.rs`、`engine/output_parser.rs`、`schema.rs`
- `codex-rs/core/src/hook_runtime.rs`

## 下一章

十四个机制，十四个文件。[s15](../s15_harness/) 把它们放进同一个进程里跑。
