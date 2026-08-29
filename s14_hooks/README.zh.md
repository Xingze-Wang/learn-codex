# s14: Hooks —— 把别人的程序放到 agent 的路径上

[English](README.md) · [中文](README.zh.md)

[s13](../s13_mcp/) → `s14` → [s15](../s15_harness/)

> *"hook 返回的一切都只是建议，除了 `deny`。"*
>
> **Harness 层**：扩展点 —— 不 fork 项目也能改变行为。

---

## 问题

到目前为止，harness 的策略全都写死在代码里：沙箱怎么配、什么时候问用户、注入什么上下文。

但每个团队的规矩不一样：

- 「agent 永远不许直接 push，推送走 CI」
- 「每次会话开始时，把我们的代码规范塞进去」
- 「删除操作必须让人确认，哪怕沙箱允许」

[s09](../s09_exec_policy/) 的规则文件能表达第一条，但表达不了第二、三条 ——
它只能对**命令**做前缀匹配，没法运行任意逻辑。

而让每个团队去 fork Codex 显然不现实。

---

## 先理解：hook 就是一个程序

Codex 不发明配置语言。它的做法是：**跑一个你写的程序，用 stdin/stdout 传 JSON。**

发给你的程序（stdin）：

```json
{"session_id": "...", "cwd": "/repo", "hook_event_name": "PreToolUse",
 "tool_name": "exec_command", "tool_input": {"cmd": "git push"}}
```

你的程序打印出来（stdout）：

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "推送走 CI，不走 agent"}}
```

就这样。**用什么语言写都行** —— Python、bash、Go、一个编译好的二进制。
只要它能读 stdin、写 stdout。

一个完整的 hook 可以短到这样：

```python
import json, sys
payload = json.load(sys.stdin)
cmd = (payload.get("tool_input") or {}).get("cmd", "")
if "git push" in cmd:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "推送走 CI，不走 agent",
    }}))
```

---

## 解决方案

十一个事件，按**能做什么**分成两类：

| 事件 | 能做什么 |
|---|---|
| `SessionStart`、`UserPromptSubmit`、`SubagentStart` | 返回 `additionalContext`（**往对话里写东西**） |
| `PreToolUse` | 返回 `allow` / `deny` / `ask`（**决定要不要发生**） |
| `PostToolUse`、`Stop`、`SessionEnd`、`PreCompact`、`Interrupt`、`SubagentStop`、`PermissionRequest` | 观察，或补充上下文 |

在配置文件 `~/.codex/hooks.json` 里声明：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^exec_command$",
        "hooks": [
          {"type": "command", "command": "python3 ~/.codex/hooks/guard.py", "timeout": 3}
        ]
      }
    ]
  }
}
```

`matcher` 是一个对**工具名**做匹配的正则。没有 matcher 就是「所有工具都跑」。

---

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

`code.py` 里的 `turn_with_hooks` 把模型换成桩之后，**原样跑这个序列** ——
所以不用 API key 也能看清每个事件的位置。

---

## 工作原理：失败模式才是设计

这一章真正的内容不是「怎么跑一个子进程」，而是**别人的程序出问题时会怎样**。

**`deny` 是唯一能拦住 agent 的结果。** 其余一切 —— 崩溃、挂死、stdout 上的垃圾 ——
都降级为「没有这个 hook 照跑，并把这件事说出来」。

下面四条，每一条都对应一种真实的坏 hook。

**一、什么都不打印的 hook = 没有意见。**

```python
stdout = stdout.strip()
if not stdout:
    return None, "", "", ""
try:
    payload = json.loads(stdout)
except json.JSONDecodeError:
    return None, "", "", ""      # 打印了畸形东西 = 同样没有意见
```

**为什么解析失败不能当成拒绝？** 因为那意味着某人脚本里的一个笔误
（多打了一个 `print("debugging")`）会**静默地废掉他的 agent**，而且他很难查出原因。

**二、超时是强制的。**

```python
except subprocess.TimeoutExpired:
    outcome.warnings.append(f"hook timed out after {hook.timeout}s: {hook.command}")
    return None, "", "", ""
```

hook 是别人写的子进程，而且它在**每一次工具调用之前**都会跑。
一个忘了设超时的 hook，能让整个 agent 卡死在一次网络请求上。

**三、注入的上下文必须有上限。**

```python
context = str(specific.get("additionalContext") or "")[:limit]
```

`additionalContextLimit` 默认 2000 字符。没有它，一个话多的 hook 会**在每一次请求里**
悄悄吃掉 [s11](../s11_compaction/) 拼命想保护的上下文窗口 —— 而且永远吃下去。

**四、matcher 写错时收紧，不放宽。**

```python
try:
    return re.search(self.matcher, subject) is not None
except re.error:
    return False
```

非法正则**匹配不到任何东西**。一个本意是**收窄**规则的 matcher 里的笔误
（`^exec_comand$`，少了个 m），应该静默地什么都不做 —— 而不是反过来对一切生效。

---

## 第一个 deny 说了算

```python
if decision == DENY:
    # 第一个 deny 说了算，其余跳过：这个工具反正不会跑了，
    # 再去问剩下的 hook 只是噪音。
    outcome.decision = DENY
    outcome.reason = reason or f"blocked by hook: {hook.command}"
    return outcome
```

还有一个不需要 JSON 的快捷方式：**退出码 2 = 拒绝**，stderr 就是理由。

```python
if proc.returncode == 2:
    return DENY, proc.stderr.strip() or "blocked by hook", "", ""
```

所以最短的 hook 可以是一行 shell：`grep -q "git push" && echo "推送走 CI" >&2 && exit 2`。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `HookConfig.load` | 解析 `hooks.json`，并报告它解析不了的部分 |
| `MatcherGroup.matches` | 对工具名做正则，失败时收紧 |
| `HookRunner.run` | 启动、喂 JSON、超时、收集 |
| `parse_hook_output` | 宽容地解析那个线上格式 |
| `turn_with_hooks` | 每个事件触发的位置 |

---

## 试一下

**不需要 API key：**

```bash
python s14_hooks/code.py --demo
```

它会临时造一份 `hooks.json`，里面**故意混进了三个坏 hook**：一个超时的、一个退出码 7 的、
一个打印非 JSON 的。然后跑一整轮：

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

**观察重点**：最后那两行警告。三个坏 hook 一个都没有让会话停下来 ——
它们只是被记了一笔，然后 agent 继续干活。这就是「除了 `deny`，一切都只是建议」的意思。

然后读你**真实**的配置：

```bash
python s14_hooks/code.py --show
```

---

## 对应真实源码

- `codex-rs/hooks/` —— `engine/dispatcher.rs`、`engine/command_runner.rs`、`engine/output_parser.rs`、`schema.rs`
- `codex-rs/core/src/hook_runtime.rs`

---

## 接下来

十四个机制，十四个独立文件。每一个都单独演示过。

[s15](../s15_harness/) 把它们放进**同一个进程**，让一轮真实的对话从头到尾穿过它们 ——
并且回答一个问题：**围绕一次 `exec_command`，这些检查该按什么顺序排？**
