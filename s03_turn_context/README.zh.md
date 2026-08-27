# s03：TurnContext 与 world state

[English](README.md) · [中文](README.zh.md)

[s02](../s02_protocol/) → `s03` → [s04](../s04_tool_registry/)

> *"一轮开始时把设置冻住，然后只把变化告诉模型。"*

---

会话活得比一轮长，但工具需要的东西几乎没有一样是会话级的。cwd、模型、审批策略、沙箱策略——
每一样都是在一轮开始时定下的，而且**不能在工具跑到一半时被换掉**。

```python
@dataclass(frozen=True)
class TurnContext:
    cwd: str
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    approval_policy: str = "on-request"
    sandbox_mode: str = "workspace-write"
    network_access: bool = False
    shell: str = ...
```

它是 frozen 的，并且显式传给每一个工具：

```python
# 工具读的是这一轮的 cwd，不是进程的 cwd。
cwd = args.get("workdir") or ctx.cwd
```

另一种写法——工具去读 `os.getcwd()` 或者一个可变的 `self.cwd`——会在用户于一轮进行中改设置的那一刻
崩掉（而 s02 恰恰让这件事成为可能）。一个在 `read-only` 下启动的工具必须在 `read-only` 下结束，
哪怕用户两秒前刚切到了 `workspace-write`。

## 怎么告诉模型它在哪

模型看不见 `TurnContext`，只能被"告知"。Codex 用对话里一条普通消息来告知：

```xml
<environment_context>
  <cwd>/Users/you/repo</cwd>
  <shell>zsh</shell>
  <current_date>2026-05-23</current_date>
  <network enabled="false" />
</environment_context>
```

朴素做法是每一轮都注入一遍。Codex 维护的是一份 **world state**：每一轮重新渲染各个 section，
和上次发出去的做比较，**只有渲染结果变了才注入**。

```python
def updates(self, ctx: TurnContext) -> list[str]:
    changed = []
    for name, render in SECTIONS.items():
        text = render(ctx)
        if self.last.get(name) != text:
            self.last[name] = text
            changed.append(text)
    return changed
```

```
第 1 轮  cwd=/repo  read-only   -> 注入 <environment_context> + <permissions>
第 2 轮  cwd=/repo  read-only   -> 什么都不注入
第 3 轮  cwd=/other read-only   -> 只注入 <environment_context>
```

"做差异"而不是"反复重复"，有两个理由。一个显然是 token。另一个是：**对话里出现的一个块本身就是信号**。
当 `<environment_context>` 在第 7 轮突然出现，它的出现本身就意味着有东西变了，模型会据此对待它；
而如果它三十轮都一模一样，模型学会的只是跳过这一段。

## 权限同样是上下文

同一套机制也负责搬运策略：

```xml
<permissions>
  <sandbox_mode>workspace-write</sandbox_mode>
  沙箱允许读取文件、允许编辑 cwd 下的文件……
  <approval_policy>on-request</approval_policy>
  当一条命令需要跑在沙箱之外时，你可以申请提权……
</permissions>
```

真正的强制执行在 s07 和 s08——不管模型怎么想，内核都会挡住那次写入。
这个块存在的意义是：让模型不必用一整轮去撞一次边界才知道边界在哪，以及让它知道"提权"这条路存在。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `TurnContext` | 冻结的每轮设置 |
| `render_environment` / `render_permissions` | 各渲染一个 section |
| `WorldState.updates` | 与上次发出去的做差异 |
| `Session.run_turn` | 解析一次上下文、注入变化，然后进循环 |

## 跑起来

```bash
python s03_turn_context/code.py --render          # 只打印 section，不调 API
python s03_turn_context/code.py "这个目录里有什么？"
python s03_turn_context/code.py --cwd /tmp "那这里呢？"
```

## 对应真实源码

- `codex-rs/core/src/session/turn_context.rs`
- `codex-rs/core/src/context/world_state/environment.rs` —— 渲染与变更检测
- `codex-rs/core/src/context/world_state/permissions.rs`

## 下一章

上下文定住了，工具列表还没有。[s04](../s04_tool_registry/) 讲它是怎么按轮组装出来的。
