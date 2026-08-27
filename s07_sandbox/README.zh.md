# s07：沙箱

[English](README.md) · [中文](README.zh.md)

[s06](../s06_unified_exec/) → `s07` → [s08](../s08_approval/)

> *"去问内核，别去猜命令字符串。"*

---

到目前为止每一章都在用用户的全部权限跑命令。Codex 不这样。默认情况下，一条命令可以读这台机器、
只能写工作区里面、完全够不到网络——**由操作系统强制执行**，而不是靠检查命令长什么样。

```
macOS    /usr/bin/sandbox-exec -p <SBPL 策略> -DWRITABLE_ROOT_0=... -- cmd
Linux    Landlock（文件系统） + seccomp（网络系统调用）
其它     没有可用沙箱 -> harness 只能改成问用户
```

三种策略，和用户在 TUI 里看到的是同样三个词：

| 策略 | 读 | 写 | 网络 |
|---|---|---|---|
| `read-only` | 任意位置 | 哪都不行 | 否 |
| `workspace-write` | 任意位置 | cwd、`$TMPDIR`、`/tmp` | 否 |
| `danger-full-access` | 任意位置 | 任意位置 | 是 |

即使在 `read-only` 下读也是不受限的，这一点常让人意外。这是刻意的：一个读不了这台机器的 agent
干不了活；而真正保护机密的是**网络拒绝**——能读到但发不出去的数据，就是留在原地的数据。

在 macOS 上跑 `--demo` 会得到下面这些，而且是真的强制执行，不是模拟：

```
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

## 两个细节决定这套东西成不成立

**真实路径。** `/var/folders/...` 是 `/private/var/folders/...` 的软链，而 seatbelt 匹配的是解析后的路径。

```python
# 软链路径必须解析，否则内核那一侧的检查永远匹配不上。
real = os.path.realpath(root)
```

漏掉这一步，沙箱**看上去**是工作的——它起来了、命令也跑了——但那个可写根什么都没授权，
于是每一次写都失败。这种失效比"没有沙箱"更糟，因为它产出的是一份看起来很自信、
行为却和它自己的描述相反的策略。

**路径作为参数传入，绝不做字符串插值。**

```python
key = f"WRITABLE_ROOT_{index}"
clauses.append(f'(subpath (param "{key}"))')
params.append(f"-D{key}={root}")
```

否则一个名字就叫 `foo") (allow file-write* (subpath "/` 的目录，能直接把策略改写掉。
这就是换了身衣服的 SQL 注入，修法也一样：**把数据当数据传**。

## "被拒绝"只是一个猜测

内核返回的是 `EPERM`。它不会说"这是沙箱干的"。

```python
DENIAL_MARKERS = ("operation not permitted", "permission denied", "read-only file system", ...)

def is_likely_sandbox_denied(output) -> bool:
    """漏判只赔一次重试。误判会把一条本来不需要出沙箱的命令
    放到沙箱外面重跑，所以这里刻意保持保守。"""
```

阈值由这个不对称性决定。漏判一次拒绝，模型看到一个费解的错误然后换个方式——烦人而已。
误判一次，harness 就会去请用户批准在沙箱外跑一条其实只是普通 bug 的命令——
一次毫无必要的安全弹窗，而这正是训练用户"闭眼点同意"的方式。**保守取胜。**

注意 demo 里**没有**被标出来的那一项：`curl -s` 在 `read-only` 下静默失败，stderr 里没有任何标记，
于是启发式保持沉默。这是这套方法诚实的边界，也正是 s08 从不把"没被判为拒绝"当作"肯定没问题"的原因。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `SandboxPolicy` | 模式、可写根、网络 |
| `effective_writable_roots` | cwd + tmp 豁免，并做路径解析 |
| `build_seatbelt_policy` | SBPL 文本 加 `-D` 参数 |
| `run_sandboxed` | 在 `sandbox-exec` 下启动 |
| `is_likely_sandbox_denied` | 触发 s08 的那个启发式 |

## 跑起来

```bash
python s07_sandbox/code.py --demo         # 四种策略下的同一组探针
python s07_sandbox/code.py --policy       # 打印生成的 SBPL
python s07_sandbox/code.py --run "ls"     # 在 workspace-write 下跑一条命令
```

demo 只在 macOS 上真正强制执行。Linux 上 Codex 用的是 Landlock + seccomp；策略形状仍会正确打印。

## 对应真实源码

- `codex-rs/sandboxing/src/seatbelt.rs`、`seatbelt_base_policy.sbpl`
- `codex-rs/sandboxing/src/landlock.rs`
- `codex-rs/protocol/src/protocol.rs` —— `SandboxPolicy`

## 下一章

沙箱说了"不"。[s08](../s08_approval/) 讲接下来发生什么。
