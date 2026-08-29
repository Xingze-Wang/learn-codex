# s07: 沙箱 —— 去问内核，别去猜命令字符串

[English](README.md) · [中文](README.zh.md)

[s06](../s06_unified_exec/) → `s07` → [s08](../s08_approval/) → ... → [s15](../s15_harness/)

> *"去问内核，别去猜命令字符串。"*
>
> **Harness 层**：边界 —— 由操作系统执行，不由代码判断。

---

## 问题

前六章的每一条命令都跑在你的完整权限之下。模型写什么，你的机器就执行什么。

第一反应通常是：**「那我检查一下命令字符串，危险的就拦下来。」**

这条路走不通，原因不是「不够仔细」，而是**根本做不到**：

```bash
rm -rf /                          # 拦得住
rm -rf $HOME/../../              # 还行
$(echo cm0gLXJmIC8K | base64 -d) # 运行时才知道是什么
python3 -c "import os; os.remove(...)"   # 根本不是 rm
make                              # 谁知道 Makefile 里写了什么
```

**问题不在于你的黑名单不够长，而在于「一条命令会做什么」这件事，在它跑起来之前是不可判定的。**

所以 Codex 不判断命令，它**改变命令能做到什么**。

---

## 先理解：操作系统沙箱是什么

现代操作系统都提供一种能力：**在启动一个进程之前，给它套上一层限制，
这层限制由内核强制，进程自己解不掉，它的子进程也继承。**

| 平台 | 机制 | 怎么用 |
|---|---|---|
| macOS | Seatbelt | `/usr/bin/sandbox-exec -p '<策略>' -- 你的命令` |
| Linux | Landlock（文件） + seccomp（系统调用） | 进程启动时自己声明限制 |
| 其它 | 没有 | harness 只能改成问用户（[s08](../s08_approval/)） |

macOS 上那个策略用一种叫 **SBPL** 的 Lisp 风格小语言写。看一眼就懂：

```lisp
(version 1)

; 默认全禁
(deny default)

; 然后一条条开口子
(allow process-exec)
(allow process-fork)
(allow file-read*)                                   ; 读，随便读
(allow file-write* (subpath (param "WRITABLE_ROOT_0")))   ; 写，只在这个目录下
```

`(deny default)` 是关键：**先全禁，再开口子。** 反过来（先全开、再禁危险的）就又回到了
「列举所有危险」那条走不通的路上。

跑 `python s07_sandbox/code.py --policy` 能看到完整生成的策略。

---

## 解决方案

三种策略，就是用户在界面上看到的那三个词：

| 策略 | 读 | 写 | 网络 |
|---|---|---|---|
| `read-only` | 任意位置 | 哪都不行 | 否 |
| `workspace-write` | 任意位置 | cwd、`$TMPDIR`、`/tmp` | 否 |
| `danger-full-access` | 任意位置 | 任意位置 | 是 |

**「读」为什么在所有策略下都不限制？** 这一点常让人意外，但它是刻意的：

- 一个读不了这台机器的 agent，干不了活。
- 真正保护机密的是**网络拒绝**：能读到但**发不出去**的数据，就是留在原地的数据。

---

## 工作原理

**第 1 步**：算出这一轮到底哪些目录可写。

```python
def effective_writable_roots(self, cwd: str) -> list[str]:
    if self.mode == DANGER_FULL_ACCESS:
        return ["/"]
    if self.mode == READ_ONLY:
        return []

    roots = [cwd, *self.writable_roots]
    if not self.exclude_tmpdir and os.environ.get("TMPDIR"):
        roots.append(os.environ["TMPDIR"])
    if not self.exclude_slash_tmp:
        roots.append("/tmp")
    # 软链路径必须解析，否则内核那一侧的检查永远匹配不上。
    resolved = []
    for root in roots:
        real = os.path.realpath(root)
        if real not in resolved:
            resolved.append(real)
    return resolved
```

**最后那个 `realpath` 是这一章最容易漏、后果最严重的一行。**

macOS 上 `/var/folders/...` 其实是 `/private/var/folders/...` 的软链，
而 seatbelt 匹配的是**解析后的路径**。

漏掉它会怎样？沙箱**看上去**是工作的 —— 它启动了、命令也跑了 —— 但那个可写根什么都没授权，
于是**每一次写都失败**。这种失效比「没有沙箱」更糟：
你得到的是一份看起来很自信、行为却和描述相反的策略。

**第 2 步**：把策略拼出来，但**路径永远作为参数传入**。

```python
key = f"WRITABLE_ROOT_{index}"
clauses.append(f'(subpath (param "{key}"))')      # 策略文本里只有一个占位符名
params.append(f"-D{key}={root}")                  # 真实路径走命令行参数
```

为什么不直接把路径拼进策略字符串？因为一个名字就叫

```
foo") (allow file-write* (subpath "/
```

的目录，能把你的策略**改写成允许写整个磁盘**。

**这就是换了身衣服的 SQL 注入**，修法也一样：把数据当数据传。

**第 3 步**：在沙箱下启动。

```python
def build_command(cmd: str, policy: SandboxPolicy, cwd: str) -> list[str]:
    inner = ["/bin/bash", "-lc", cmd]
    if policy.mode == DANGER_FULL_ACCESS or platform_sandbox() != "seatbelt":
        return inner
    text, params = build_seatbelt_policy(policy, cwd)
    return [SANDBOX_EXEC, "-p", text, *params, "--", *inner]
```

注意 `platform_sandbox() != "seatbelt"` 那个分支：**这台机器没有沙箱时，这里不假装有。**
它返回不带沙箱的命令，然后由 [s08](../s08_approval/) 去决定「没有沙箱的情况下还跑不跑」。

**第 4 步**：判断一次失败是不是沙箱造成的 —— 而这只是一个**猜测**。

```python
DENIAL_MARKERS = ("operation not permitted", "permission denied",
                  "read-only file system", ...)

def is_likely_sandbox_denied(output) -> bool:
    """漏判只赔一次重试。误判会把一条本来不需要出沙箱的命令
    放到沙箱外面重跑，所以这里刻意保持保守。"""
    if not output.sandboxed or output.exit_code == 0:
        return False
    haystack = output.aggregated.lower()
    return any(marker in haystack for marker in DENIAL_MARKERS)
```

内核返回的是 `EPERM`，它**不会说「这是沙箱干的」**。所以只能看错误文本猜。

阈值由一个不对称性决定：

- **漏判一次拒绝** → 模型看到一个费解的错误，换个方式再来。烦人而已。
- **误判一次** → harness 去请用户批准**在沙箱外**跑一条其实只是普通 bug 的命令。
  一次毫无必要的安全弹窗 —— 而这正是训练用户「闭眼点同意」的方式（[s09](../s09_exec_policy/) 会展开）。

**保守取胜。**

---

## 跑一下就知道是真的

```bash
python s07_sandbox/code.py --demo
```

```
platform sandbox: seatbelt

== read-only ==
  read a file                  allowed
  write inside the workspace   blocked (looks like a sandbox denial)
  write outside the workspace  blocked (looks like a sandbox denial)
  reach the network            blocked

== workspace-write ==
  read a file                  allowed
  write inside the workspace   allowed
  write outside the workspace  blocked (looks like a sandbox denial)
  reach the network            blocked
```

这不是模拟，是内核真的在拦。

**观察重点**：`read-only` 那一组，「写工作区」也是 blocked —— 这就是 read-only 的含义。
再看 `workspace-write` 那一组，同一条命令变成 allowed 了，而「写工作区外面」仍然 blocked。

还有一个诚实的边角：`reach the network` 那一行**没有**被标成 `(looks like a sandbox denial)`。
因为 `curl -s` 静默失败，stderr 里没有任何标记，启发式就沉默了。
**这是这套方法真实的边界**，也正是 [s08](../s08_approval/) 从不把「没被判为拒绝」当作
「肯定没问题」的原因。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `SandboxPolicy` | 模式、可写根、网络 |
| `effective_writable_roots` | cwd + tmp 豁免，并做路径解析 |
| `build_seatbelt_policy` | SBPL 文本 加 `-D` 参数 |
| `run_sandboxed` | 在 `sandbox-exec` 下启动 |
| `is_likely_sandbox_denied` | 触发 [s08](../s08_approval/) 的那个启发式 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 权限 | 你的全部权限 | 内核允许这个进程的那些 |
| 写文件 | 磁盘任意位置 | 只有可写根里面 |
| 网络 | 通的 | 默认断 |
| 怎么判断危险 | 检查命令字符串（做不到） | 不判断 —— 改变这条命令**能做到什么** |

---

## 试一下

**不需要 API key：**

```bash
python s07_sandbox/code.py --demo         # 四种策略下的同一组探针
python s07_sandbox/code.py --policy       # 打印生成的 SBPL，读一读
python s07_sandbox/code.py --run "echo hi > /etc/hosts"   # 亲手撞一次墙
```

demo 只在 macOS 上真正强制执行。Linux 上 Codex 用 Landlock + seccomp
（`codex-rs/sandboxing/src/landlock.rs`）；策略形状仍会正确打印。

---

## 对应真实源码

- `codex-rs/sandboxing/src/seatbelt.rs`、`seatbelt_base_policy.sbpl`
- `codex-rs/sandboxing/src/landlock.rs`
- `codex-rs/protocol/src/protocol.rs` —— `SandboxPolicy`

---

## 接下来

沙箱说了「不」。然后呢？

模型想写 `~/.npmrc`，这是个合理需求。命令失败了，退出码非零。**现在谁来决定要不要放它出去？**

[s08](../s08_approval/) 是那六步：评估 → 沙箱里跑 → 被拒？→ 问用户 →
获批后关掉沙箱重跑 → 记住，别再问第二遍。
