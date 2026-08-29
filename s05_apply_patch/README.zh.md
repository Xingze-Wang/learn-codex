# s05: apply_patch —— 一份 patch 是对文件当前内容的断言

[English](README.md) · [中文](README.zh.md)

[s04](../s04_tool_registry/) → `s05` → [s06](../s06_unified_exec/) → ... → [s15](../s15_harness/)

> *"一份 patch 是对文件当前内容的一个断言。断言不成立，就拒绝。"*
>
> **Harness 层**：写入 —— 唯一一个不用 shell 做的操作。

---

## 问题

模型要改一个 900 行文件里的三行。它怎么把这个意图表达出来？

看起来有三条路，前两条都会在同一个地方翻车。

**路子一：让模型重新输出整个文件。**

- 900 行的 token 全花一遍，只为了改 3 行。
- 更糟的是：模型是**凭记忆**重写的。它没提到的那个函数，就这么没了 ——
  而且**没有任何报错**。你下次跑测试才会发现。

**路子二：带行号的 diff（「把第 412 行换成……」）。**

- 只要文件在那之前被改动过、错位了一行，这个 patch 就会**干干净净地打到错误的位置上**。
- 而 patch 里没有任何信息能让程序发现这件事。

两条路的共同病根：**它们都没有携带「模型以为文件现在长什么样」这个信息。**

---

## 先理解：带上下文的 patch 自带校验

第三条路，也是 Codex 走的路：

```
*** Begin Patch
*** Update File: src/app.py
@@ def handler():
-    return None
+    return build_response()
*** End Patch
```

读一下这段东西在说什么：

- `@@ def handler():` —— 「去找 `def handler():` 这一行附近」（可选，用来缩小范围）
- `-    return None` —— 「**这一行现在就在那里**，把它删掉」
- `+    return build_response()` —— 「换成这一行」

关键在那个 `-` 开头的行：**它是一个断言。**

如果 `return None` 不在那儿（文件被别人改过了、模型记错了），
**patch 就失败，模型收到一条错误信息** —— 而不是把别人的工作覆盖掉。

顺带一提：这也让 patch 的成本正比于**改动量**，而不是文件大小。

---

## 解决方案

文法只有三种 hunk（改动块）：

```
*** Add File: path        后跟若干以 + 开头的行 = 新文件的全部内容
*** Delete File: path     没有别的了
*** Update File: path     可选的 *** Move to: path，然后是若干 chunk
```

一个 chunk = 可选的 `@@ 上下文行`，加上若干以 `+`（加）、`-`（删）、` `（保持不变）开头的行。

这份文法被**执行了两次**：

1. **在模型侧** —— 工具自带这份 Lark 文法（[s04](../s04_tool_registry/)），
   解码器根本吐不出文法之外的东西。
2. **在这里** —— 解析器再验一遍，因为你不能假设上游一定守规矩。

---

## 工作原理

分三步：解析 → 定位 → 写入。

### 第 1 步：解析

逐行扫描，遇到 `*** Xxx File:` 就开一个新 hunk。update hunk 里再按 `@@` 切成若干 chunk：

```python
if line.startswith("+"):
    current.new_lines.append(line[1:])            # 新增
elif line.startswith("-"):
    current.old_lines.append(line[1:])            # 删除
elif line.startswith(" "):
    current.old_lines.append(line[1:])            # 上下文：两边都要
    current.new_lines.append(line[1:])
```

上下文行同时进 `old_lines` 和 `new_lines`，因为它「改动前后都在」。

有一个细节不是装饰：

```python
elif line == "":
    # 一个纯空行，其实是上下文行前面那个空格在传输中被裁掉了。
    # 模型天天这么干。
    current.old_lines.append("")
    current.new_lines.append("")
```

一个空的上下文行本该是 `" "`（一个空格）。但空格常常在各种环节被 strip 掉。
不处理这种情况，任何跨越空行的 patch 都会挂。

### 第 2 步：定位 —— 三步递减的宽容

拿着 `old_lines` 去文件里找它在哪：

```python
for normalize in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
    for i in range(search_start, last + 1):
        if all(normalize(lines[i + k]) == normalize(pattern[k]) for k in range(len(pattern))):
            return i
```

三轮，一轮比一轮松：

1. **完全一致**
2. **忽略行尾空白**（编辑器删了尾部空格）
3. **忽略首尾空白**（缩进被改过）

**命中即停**，所以一个文件里如果同时存在「精确匹配」和「缩进不同的匹配」，永远取前者。

**为什么不再松一点？** 模糊匹配、相似度打分会开始**打到错误的位置**。
被拒绝的 patch 只赔一轮；打错位置的 patch 赔进去的是一场 debug —— 而且你未必知道要 debug。

`*** End of File` 会把搜索锚定在文件末尾：

```python
search_start = len(lines) - len(pattern) if eof else start
```

用于「往文件尾部追加，而那几行在前面也出现过」这种常见情况。

### 第 3 步：写入 —— 要么全成，要么全不动

```python
# 第一遍：不碰文件系统，先把每个结果算出来，
# 这样第 3 个 hunk 失败时，前两个不会已经半落地了。
for hunk in hunks:
    ...
    changes.append(FileChange("update", hunk.path, old_content=old,
                              new_content=_apply_chunks(hunk.path, old, hunk.chunks)))

# 第二遍：写。
for change in changes:
    ...
```

整份 patch 先在内存里解完 —— 每个文件读一遍、每个 chunk 定位一遍、每个结果算一遍 —— **然后才写盘**。

为什么这么在意？**半落地的修改是所有结局里最糟的一种。** 模型下一次 `git diff` 看到的是一个
既不是它也不是用户想要的状态，而它会试图基于这个状态继续推理。

---

## diff 会被发出去

harness 手上同时有改前和改后，所以它能生成一份真正的 diff：

```python
def unified_diff(self) -> str:
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{self.path}", tofile=f"b/{label}"))
```

终端界面渲染它，`--json` 输出它，[s15](../s15_harness/) 把整轮的 diff 攒起来。
**模型完全不需要再跑一次 `git diff` 来展示自己干了什么。**

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `parse_patch` | 文本 → `AddFile` / `DeleteFile` / `UpdateFile` |
| `seek_sequence` | 三步定位搜索 |
| `apply_patch` | 先全部校验，再全部写入 |
| `FileChange.unified_diff` | 事件流里搬运的东西 |

---

## 这一章改变了什么

|  | 这一章之前 | 这一章之后 |
|---|---|---|
| 改文件 | 用一条 shell 命令把整个文件重写一遍 | 一份只描述改动的 patch |
| 代价 | 正比于文件大小 | 正比于改动大小 |
| 模型记错了文件内容 | 静默覆盖掉你的工作 | patch 失败，并说明原因 |
| patch 打到一半失败 | 文件被改了一半 | 一个字节都没写 |
| 展示 diff | 模型自己跑 `git diff` | harness 直接产出 |

---

## 试一下

这一章**不需要 API key**，是纯逻辑：

```bash
python s05_apply_patch/code.py --demo
```

它会建一个临时目录，放一个 `app.py`，然后打一份 patch。你会看到 patch 原文、生成的 diff，
和改完的文件。

**观察重点**：手动制造一次失败。把 demo 里的 patch 复制出来，
把 `-    # TODO: implement` 改成一行文件里**没有**的内容，然后：

```bash
python s05_apply_patch/code.py --apply /tmp/somewhere < your-patch.txt
```

你会看到 `chunk 1 does not match the file (looking for ...)`，而且**文件一个字节都没变**。
这就是这一整章的意义。

---

## 对应真实源码

- `codex-rs/apply-patch/src/parser.rs`、`seek_sequence.rs`、`file_update.rs`
- `codex-rs/core/src/tools/handlers/apply_patch.lark` —— 交给模型的那份文法

---

## 接下来

「改文件」解决了。但「跑命令」还停在 s01 的水平：`/bin/bash -lc CMD`，等它退出。

这个模型在下面这些场景全都会崩：`cd build && make`（cd 丢了）、`python3`（永不退出）、
`npm run dev`（必须一直跑着）、`ssh host`（等你输密码）。

[s06](../s06_unified_exec/) 讲一个**活过单次工具调用**的 shell。
