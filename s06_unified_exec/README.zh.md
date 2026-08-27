# s06：Unified exec —— 活过单次工具调用的 shell

[English](README.md) · [中文](README.zh.md)

[s05](../s05_apply_patch/) → `s06` → [s07](../s07_sandbox/)

> *"提前带着 session id 返回，而不是拖到超时才返回。"*

---

s01 跑 `/bin/bash -lc CMD` 然后等它退出。这个模型在所有有意思的场景上都会崩：

```
cd build && make     -> 下一次调用时那个 cd 已经没了
python3              -> 一个永不退出的 REPL，于是这次调用永远不返回
npm run dev          -> 一个必须一直跑着、同时还要继续干别的活的服务
ssh host             -> 一个等着你回答的提示符
```

Codex 的 `exec_command` 打开的是一个 **PTY 会话**。命令在 `yield_time_ms` 内跑完，
工具就返回它的输出并回收会话；没跑完，工具就**提前**带着一个 session id 返回，
模型接下来用 `write_stdin` 继续和这个活着的进程对话。

## 响应头就是协议

```
Chunk ID: 8f21ac
Wall time: 0.0031 seconds
Process exited with code 0            <- 结束了
Output:
...
```

```
Chunk ID: 44b0e1
Wall time: 1.0007 seconds
Process running with session ID 3     <- 还活着，去跟它说话
Output:
>>>
```

模型不需要猜命令有没有跑完。一行字就说明了它处在哪种状态，
而 `Process running with session ID 3` 同时就是它继续下去所需要的句柄。

## 为什么用 PTY 而不是管道

两个都很实际的理由。交互式程序会检查 stdout 是不是终端并改变行为——`python3` 对着管道不打提示符，
`git` 的分页方式不同，进度条会消失。而一个从"永不关闭的管道"里等输入的进程，只会**无声地挂住**。

代价是 PTY 会回显：你写进去的东西会出现在输出里。这就是 demo 里 `print(6 * 7)` 会紧挨着 `42` 出现的原因。

## 怎么读才不会挂住

```python
while time.monotonic() < deadline:
    if not selector.select(timeout=0.05):
        if self.process.poll() is not None:
            break
        if time.monotonic() - last_output > IDLE_QUIET_MS / 1000:
            break
        continue
```

三个出口：进程死了、输出安静了、yield 窗口到了。**"输出安静"这个出口**才是交互会话手感的关键——
一个已经打完提示符、正在等你输入的 REPL 没有更多话要说了，而为了发现这件事去死等满 10 秒，
会让每一次交互都变得没法用。

```python
except OSError as exc:
    if exc.errno in (errno.EIO, errno.EBADF):
        break  # 子进程关掉了 pty：它没了
```

在 PTY 上，子进程退出表现为读操作返回 `EIO`，而不是 EOF。把它当成错误处理，
就会把每一条正常跑完的命令都报成崩溃。

## 输出有上限，而且要告诉模型

```python
class HeadTailBuffer:
    """留头留尾，扔掉中间。"""
```

```
Chunk ID: 9fb9b5
Wall time: 0.0285 seconds
Process exited with code 0
Original token count: 50000          <- 本来会是多少
Output:
xxxxxxxxxx...
[... 199900 characters truncated ...]
```

`Original token count` 比截断本身更重要：模型能看到自己要了一个巨大的东西，
于是下一条命令会收窄范围，而不是以为自己看全了。

## 进程组

```python
start_new_session=True,  # 自己的进程组，这样能干掉整棵树
```

没有这一行就去杀 shell，它的子进程——那个 `make`、那个 dev server、那个 `ssh`——会在会话结束后继续跑。
对 harness 自己创建的进程组调 `os.killpg`，才能把这条命令启动的一切都清理干净。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `ProcessManager.exec_command` | 在 PTY 下启动、读到 yield、返回结果或交回一个会话 |
| `ProcessManager.write_stdin` | 与活着的会话对话 |
| `ExecSession.read_available` | 截止时间 + 安静 + 退出 |
| `HeadTailBuffer` | 有界输出，并记录丢弃量 |
| `ExecResult.render` | 上面那个响应头 |

## 跑起来

```bash
python s06_unified_exec/code.py --demo
python s06_unified_exec/code.py --repl     # 手动驱动一个活会话
```

## 对应真实源码

- `codex-rs/core/src/unified_exec/` —— `mod.rs`、`process.rs`、`process_manager.rs`
- `codex-rs/core/src/tools/context.rs` —— `response_header`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` —— `yield_time_ms`、`max_output_tokens`

## 下一章

到目前为止的一切都跑在用户的全部权限之下。[s07](../s07_sandbox/) 把这些权限收回去。
