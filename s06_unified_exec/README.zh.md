# s06: Unified exec —— 活过单次工具调用的 shell

[English](README.md) · [中文](README.zh.md)

[s05](../s05_apply_patch/) → `s06` → [s07](../s07_sandbox/) → ... → [s15](../s15_harness/)

> *"提前带着 session id 返回，而不是拖到超时才返回。"*
>
> **Harness 层**：执行 —— 进程活多久，由谁说了算。

---

## 问题

s01 的做法是：`subprocess.run(["/bin/bash", "-lc", cmd])`，等它退出，拿输出。

四个非常普通的场景会让它当场失效：

```
cd build && make      -> cd 生效了，但下一次调用又是一个全新的 bash，目录回去了
python3               -> 一个永不退出的 REPL，于是这次调用永远不返回
npm run dev           -> 一个必须一直跑着、同时还要继续干别的活的服务
ssh host              -> 一个等着你输密码的提示符
```

前两个尤其致命：**第一个静默地做错事，第二个直接把 agent 挂死。**

---

## 先理解：PTY 是什么，为什么不用管道

`subprocess` 默认用**管道**（pipe）把子进程的输出接过来。管道足够跑 `ls`，但交互式程序会因为它
表现异常。

原因是：**很多程序会检查「我的输出是不是接在一个终端上」，然后改变行为。**

- `python3` 对着管道**不打印提示符** `>>>`（它以为在被脚本调用）
- `git` 的分页行为不同
- 进度条、颜色统统消失

更糟的是输入侧：一个程序想从管道读输入，而这个管道**永远不关闭**，它就会**无声地挂住**。

**PTY**（pseudo-terminal，伪终端）解决这个：它在内核里造一对假的终端设备，
子进程那一端看起来就是一个真终端。

```python
master_fd, slave_fd = pty.openpty()      # 造一对：主端给我们，从端给子进程
process = subprocess.Popen(
    [self.shell, "-lc", cmd],
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    start_new_session=True,
)
os.close(slave_fd)                        # 从端交给子进程之后，我们这边就不留了
```

代价是 PTY 会**回显**：你写进去的东西会出现在输出里。所以 demo 里 `print(6 * 7)`
会紧挨着 `42` 一起出现 —— 那不是 bug，那是终端本来的样子。

---

## 解决方案

不要「跑完再返回」，改成「**跑一会儿，到点就带着句柄返回**」。

```
  exec_command(cmd, yield_time_ms=10000)
                │
                ├── 命令在窗口内跑完了 ──► 返回输出 + 退出码，会话回收
                │
                └── 还没跑完 ──────────► 返回目前的输出 + 一个 session id
                                            │
                                            ▼
                          write_stdin(session_id, "print(6*7)\n")
                                            │
                                            ▼
                                    返回新的输出（进程还活着）
```

模型不需要猜命令有没有跑完 —— **响应头的一行字直接说了**。

---

## 响应头就是协议

命令跑完了：

```
Chunk ID: 8f21ac
Wall time: 0.0031 seconds
Process exited with code 0
Output:
hello
```

命令还活着：

```
Chunk ID: 44b0e1
Wall time: 1.0007 seconds
Process running with session ID 3
Output:
>>>
```

`Process running with session ID 3` 同时就是模型继续下去所需要的**句柄**。
它下一步会调 `write_stdin(3, "...")`。

---

## 工作原理

**第 1 步**：在 PTY 下启动，并且给它自己的进程组。

```python
process = subprocess.Popen(
    [self.shell, "-lc", cmd],
    cwd=cwd or os.getcwd(),
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    start_new_session=True,  # 自己的进程组，这样能干掉整棵树
    close_fds=True,
)
```

`start_new_session=True` 那行很重要。没有它，你后面杀 shell 时，
它的子进程 —— 那个 `make`、那个 dev server、那个 `ssh` —— **会在会话结束后继续跑**。

**第 2 步**：读，但要能停下来。这是这一章最微妙的一段。

```python
while time.monotonic() < deadline:
    if not selector.select(timeout=0.05):
        if self.process.poll() is not None:
            break                                        # 出口 A：进程死了
        if time.monotonic() - last_output > IDLE_QUIET_MS / 1000:
            break                                        # 出口 B：输出安静了
        continue
    ...读一块数据...
```

三个出口：**进程退出**、**输出安静了 120ms**、**yield 窗口到了**。

出口 B 是交互手感的关键。一个已经打完 `>>>` 提示符、正在等你输入的 REPL **没有更多话要说了**。
为了发现这件事去死等满 10 秒，会让每一次交互都变得没法用。

**第 3 步**：PTY 的一个坑 —— 子进程退出不是 EOF，是 `EIO`。

```python
except OSError as exc:
    if exc.errno in (errno.EIO, errno.EBADF):
        self.pty_closed = True  # 子进程关掉了 pty：它没了
        break
    raise
```

在普通管道上，子进程退出你会读到 EOF（空字节串）。在 PTY 上，你会读到一个 **`EIO` 错误**。
把它当异常往外抛，就会把**每一条正常跑完的命令**都报成崩溃。

**第 4 步**：pty 关闭 ≠ 进程已被回收。这两件事差几毫秒。

```python
exit_code = session.process.poll()
if exit_code is None and session.pty_closed:
    # pty 关了，说明命令确实跑完了；等一下退出状态，
    # 而不是把一个已经结束的东西报成还活着的会话。
    # 机器负载高时这个窗口宽到会出问题。
    try:
        exit_code = session.process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        exit_code = None
```

> 这一段是被一个**偶发失败的测试**逼出来的：满负载跑整个测试套件时，
> `exec_command("true")` 偶尔会返回一个 session id，仿佛 `true` 还在跑。
> 只读 `poll()` 是不够的。

**第 5 步**：输出有上限，而且要**告诉模型上限生效了**。

```python
class HeadTailBuffer:
    """留头留尾，扔掉中间。"""
```

```
Chunk ID: 9fb9b5
Process exited with code 0
Original token count: 50000          <-- 本来会是多少
Output:
xxxxxxxxxx...
[... 199900 characters truncated ...]
```

`Original token count` 比截断本身更重要：**模型能看到自己要了一个巨大的东西**，
于是下一条命令会收窄范围（`| head -50`），而不是以为自己看全了。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `ProcessManager.exec_command` | 在 PTY 下启动、读到 yield、返回结果或交回一个会话 |
| `ProcessManager.write_stdin` | 与活着的会话对话 |
| `ExecSession.read_available` | 那三个出口 |
| `HeadTailBuffer` | 有界输出，并记录丢弃量 |
| `ExecResult.render` | 上面那个响应头 |

---

## 试一下

这一章**不需要 API key**：

```bash
python s06_unified_exec/code.py --demo
```

它会依次演示：跑完的命令、没跑完的命令（拿到 session id）、
往活会话里写 stdin、会话记得上一条的变量、关掉它、以及输出超预算。

**观察重点**：看第三、四段。第三段 `print(6 * 7)` 得到 `42`；
第四段先 `x = 'kept'` 再 `print(x)` 得到 `kept` —— **同一个 Python 进程，变量还在**。
这就是 s01 那种一次性 `subprocess.run` 永远做不到的事。

想自己开一个玩玩：

```bash
python s06_unified_exec/code.py --repl
> open python3 -i -q -u
> import os; os.getcwd()
> quit
```

---

## 对应真实源码

- `codex-rs/core/src/unified_exec/` —— `mod.rs`、`process.rs`、`process_manager.rs`
- `codex-rs/core/src/tools/context.rs` —— `response_header`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` —— `yield_time_ms`、`max_output_tokens`

---

## 接下来

现在 agent 能跑任何命令、能开长期会话、能改文件了。

**而这一切都跑在你的完整权限之下。** 它可以写 `~/.ssh/`，可以 `curl` 把任何东西发出去，
可以 `rm -rf` 任何目录 —— 只要模型觉得这是个好主意。

[s07](../s07_sandbox/) 把这些权限收回去，**而且是让操作系统内核来收**。
