# s03: TurnContext 与 world state —— 冻住设置，只讲变化

[English](README.md) · [中文](README.zh.md)

[s02](../s02_protocol/) → `s03` → [s04](../s04_tool_registry/) → ... → [s15](../s15_harness/)

> *"一轮开始时把设置冻住，然后只把变化告诉模型。"*
>
> **Harness 层**：上下文注入 —— 模型怎么知道自己在哪。

---

## 问题

模型看不见你的机器。它不知道自己在哪个目录、shell 是 zsh 还是 bash、今天几号、能不能联网、
能不能写文件。

不告诉它，它就只能**靠试**：跑一条命令，撞一个 `Operation not permitted`，猜一下原因，再试一条。
一轮下来什么都没做成，全花在摸边界上了。

于是你想：那就每轮都把这些信息塞进去。可是第二个问题马上来了 ——

**同样一段 `<environment_context>` 连续贴三十轮，模型就学会跳过它了。** 它变成了背景噪音。
而且每轮都在花 token。

还有第三个问题：用户完全可以在 agent 干到一半时去改设置（一个好的 agent 就该允许这个，见 [s02](../s02_protocol/)）。
一个在「只读」下启动的工具，跑到一半设置变成了「可写」，它该按哪个来？

---

## 一「轮」是什么

一轮（turn）= 从用户说一句话开始，到模型不再要求工具为止。中间可能跑了十条命令、
发了五次模型请求 —— 这些都在同一轮里。

一个会话（session）里有很多轮。

关键区别在于：**几乎没有任何工具需要的东西是「会话级」的。**

- 在哪个目录跑？—— 这一轮的事。
- 用哪个模型、能不能写文件？—— 这一轮的事。

所以 Codex 把这些打包成一个 `TurnContext`，在一轮**开始时**定下来，**中途不许改**。

---

## 解决方案

两件事，各解决上面一个问题。

**一、冻结**：一轮开始时把设置拍成快照，工具读快照，不读全局变量。

```python
@dataclass(frozen=True)      # frozen=True：造出来就改不了
class TurnContext:
    cwd: str
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    approval_policy: str = "on-request"
    sandbox_mode: str = "workspace-write"
    network_access: bool = False
    shell: str = ...
```

**二、只讲变化**：把要告诉模型的东西切成几个 section，每轮重新渲染一遍，
**只有渲染结果和上次不一样，才注入。**

```
第 1 轮  cwd=/repo  read-only   -> 注入 <environment_context> + <permissions>
第 2 轮  cwd=/repo  read-only   -> 什么都不注入
第 3 轮  cwd=/other read-only   -> 只注入 <environment_context>
```

---

## 工作原理

**第 1 步**：一轮开始时解析出这一轮的设置，并存下来给后面的轮次用。

```python
def run_turn(self, text: str, *, echo: bool = True, **overrides: Any) -> str:
    # 这一轮的设置在这里定死。这行以下的任何东西都不许再改它 --
    # 中途一个 `cd` 不会移动这一轮的 cwd。
    ctx = self.defaults.with_overrides(**overrides)
    self.defaults = ctx           # 会话级默认值也跟着更新，给下一轮用
    self.turns.append(ctx)
```

`frozen=True` 让「不许改」变成语言层面的保证，而不是一句注释。想要个不一样的？造一个新的：

```python
def with_overrides(self, **kwargs: Any) -> TurnContext:
    return replace(self, **{k: v for k, v in kwargs.items() if v is not None})
```

**第 2 步**：把这一轮的设置渲染成模型能读的文本。

```python
def render_environment(ctx: TurnContext, *, today: str | None = None) -> str:
    date = today or dt.date.today().isoformat()
    return (
        "<environment_context>\n"
        f"  <cwd>{ctx.cwd}</cwd>\n"
        f"  <shell>{ctx.shell}</shell>\n"
        f"  <current_date>{date}</current_date>\n"
        f'  <network enabled="{str(ctx.network_access).lower()}" />\n'
        "</environment_context>"
    )
```

跑一下 `--render` 就能看到真实输出：

```xml
<environment_context>
  <cwd>/Users/you/learn-codex</cwd>
  <shell>zsh</shell>
  <current_date>2026-08-29</current_date>
  <network enabled="false" />
</environment_context>
```

**第 3 步**：和上次发出去的比一比，只把变了的拿出来。

```python
class WorldState:
    """记住上次告诉过模型什么，所以只在变化时再说一遍。"""

    def __init__(self) -> None:
        self.last: dict[str, str] = {}

    def updates(self, ctx: TurnContext) -> list[str]:
        changed = []
        for name, render in SECTIONS.items():
            text = render(ctx)
            if self.last.get(name) != text:      # 只比字符串，不比字段
                self.last[name] = text
                changed.append(text)
        return changed
```

比字符串而不是比字段，是因为**真正重要的是「模型看到的东西变没变」**，不是内部状态变没变。

**第 4 步**：把变化作为普通用户消息塞进 history，然后才是用户这一轮真正说的话。

```python
for section in self.world.updates(ctx):
    self.history.append(user_item(section))
self.history.append(user_item(text))
```

它们就是普通消息 —— 没有特殊通道，没有特殊权限。

**第 5 步**：工具读这一轮的 `ctx`，不读进程状态。

```python
cmd = args.get("cmd", "")
# 工具读的是这一轮的 cwd，不是进程的 cwd。
cwd = args.get("workdir") or ctx.cwd
```

如果这里写成 `os.getcwd()` 或者一个可变的 `self.cwd`，那么用户在一轮进行中改设置的那一刻，
这个工具的行为就变了 —— 而它自己完全不知道。

---

## 为什么「只讲变化」不只是省 token

省 token 是显然的好处。但还有一个更重要的：

**对话里出现的一个块，本身就是一个信号。**

当 `<environment_context>` 在第 7 轮突然出现，它的出现本身就意味着「有东西变了」，
模型会认真读它。而如果它三十轮都一模一样，模型学会的只是**跳过这一段**。

---

## 权限也是上下文

同一套机制也搬运策略：

```xml
<permissions>
  <sandbox_mode>workspace-write</sandbox_mode>
  沙箱允许读取文件、允许编辑 cwd 下的文件。编辑其它位置需要审批。
  <approval_policy>on-request</approval_policy>
  当一条命令需要跑在沙箱之外时，你可以申请提权。
  <network_access>false</network_access>
</permissions>
```

**这段文字不负责强制执行。** 真正拦住写操作的是内核（[s07](../s07_sandbox/)）和审批流程
（[s08](../s08_approval/)），不管模型信不信这段话。

那它为什么存在？两个理由：

1. 让模型不必用一整轮去撞一次边界，才知道边界在哪。
2. 让模型知道「提权」这条路存在 —— 否则它撞了墙只会放弃。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `TurnContext` | 冻结的每轮设置 |
| `render_environment` / `render_permissions` | 各渲染一个 section |
| `WorldState.updates` | 和上次发出去的做差异 |
| `Session.run_turn` | 解析一次上下文、注入变化，然后进循环 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 设置放哪 | 全局变量，工具随时读 | 冻结的 `TurnContext`，显式传入 |
| 中途改设置 | 正在跑的工具悄悄换了行为 | 这一轮守住它开始时的设置 |
| 模型对环境的了解 | 一无所知，只能靠撞墙 | 一个 `<environment_context>` 块 |
| 这个块发几次 | — | 只在渲染结果变化时 |

---

## 试一下

先看看会注入什么（不调 API）：

```bash
python s03_turn_context/code.py --render
```

然后真跑：

```bash
python s03_turn_context/code.py "这个目录里有什么？"
python s03_turn_context/code.py --cwd /tmp "那这里呢？"
```

**观察重点**：进交互模式，连问两个关于当前目录的问题。第一次会注入两个块，
第二次一个都不注入 —— 因为什么都没变。这正是测试里断言的东西：

```python
assert injected_after_two == 2      # 第一轮注入两个，第二轮零个
assert injected_after_three == 3    # 第三轮换了目录，多一个
```

---

## 对应真实源码

- `codex-rs/core/src/session/turn_context.rs`
- `codex-rs/core/src/context/world_state/environment.rs` —— 渲染与变更检测
- `codex-rs/core/src/context/world_state/permissions.rs`

---

## 接下来

上下文定住了。但工具清单还是写死的一个 `exec_command`。

真实的 Codex 要在每一轮临时组装工具清单：这个模型认 `exec_command` 还是 `shell`？
`apply_patch` 开了吗？接了几个 MCP server？

[s04](../s04_tool_registry/) 讲这份清单是怎么按轮拼出来的 —— 以及**为什么工具出错时绝对不能抛异常**。
