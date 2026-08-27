# s05：apply_patch

[English](README.md) · [中文](README.zh.md)

[s04](../s04_tool_registry/) → `s05` → [s06](../s06_unified_exec/)

> *"一份 patch 是对文件当前内容的一个断言。断言不成立，就拒绝。"*

---

Codex 用一个工具改文件，它的载荷是一份 patch：

```
*** Begin Patch
*** Update File: src/app.py
@@ def handler():
-    return None
+    return build_response()
*** End Patch
```

不是整文件覆写，也不是带行号的 diff。这两种替代方案会在同一个地方失败——
当模型对文件的认知已经过期时：

- **整文件覆写**的 token 成本正比于文件大小，而且会**静默删掉**模型没能复述出来的一切。
  一个 900 行的文件凭记忆重写一遍，丢掉的是那个没人提过的函数。
- **带行号的 diff** 在文件哪怕只错位一行时，也会干干净净地打到错误的位置上，
  而 patch 本身没有任何信息能发现这件事。

带上下文的 patch **自带校验**。`-    return None` 说的是"这一行此刻就在那里"。
如果它不在，patch 就失败、模型就会被告知，而不是把别人的工作覆盖掉。

## 文法被执行了两次

一次在模型侧——工具自带那份 Lark 文法（s04），解码器根本吐不出文法之外的东西；
一次在这里的解析器。三种 hunk：

```
*** Add File: path        后跟若干 +行
*** Delete File: path
*** Update File: path     可选的 *** Move to: path，然后是若干 chunk
```

一个 chunk = 可选的 `@@ 上下文` 行，加上若干以 `+`、`-` 或空格开头的行。

有一个解析细节不是装饰：

```python
elif line == "":
    # 一个纯空行，其实是上下文行前面那个空格在传输中被裁掉了。
    # 模型天天这么干。
    current.old_lines.append("")
    current.new_lines.append("")
```

## 定位 chunk：三步递减的宽容

```python
for normalize in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
    for i in range(search_start, last + 1):
        if all(normalize(lines[i + k]) == normalize(pattern[k]) for k in range(len(pattern))):
            return i
```

先精确匹配，再忽略行尾空白，最后忽略首尾空白。每一步都比上一步松，命中即停——
所以一个文件里同时存在"精确匹配"和"缩进被改过的匹配"时，永远取前者。

再松一点（模糊匹配、相似度打分）就会开始**打到错误的位置**，那比失败更糟：
被拒绝的 patch 只赔一轮，打错的 patch 赔进去的是一场 debug。

`*** End of File` 会把搜索锚定在文件末尾而不是开头，用于"往文件尾部追加、而那几行在前面也出现过"
这种常见情况。

## 要么全成，要么全不动

```python
# 第一遍：不碰文件系统，先把每个结果算出来，
# 这样第 3 个 hunk 失败时，前两个不会已经半落地了。
```

整份 patch 先在内存里解完——每个文件读一遍、每个 chunk 定位一遍、每个结果算一遍——**然后才写盘**。
一份改四个文件、在第四个上失败的 patch，会让工作区保持原样。
半落地的修改是所有结局里最糟的一种：模型下一次 `git diff` 看到的是一个既不是它也不是用户想要的状态。

## diff 会被发出去

```python
def unified_diff(self) -> str:
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{self.path}", tofile=f"b/{label}"))
```

harness 手上同时有改前和改后，所以它能把一份真正的 diff 作为事件发出去。
TUI 负责渲染，`--json` 负责输出，s15 把它们累积成一份 turn diff。
模型完全不需要再跑一次 `git diff` 来展示自己干了什么。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `parse_patch` | 文本 → `AddFile` / `DeleteFile` / `UpdateFile` |
| `seek_sequence` | 三步定位搜索 |
| `apply_patch` | 先全部校验，再全部写入 |
| `FileChange.unified_diff` | 事件流里搬运的东西 |

## 跑起来

```bash
python s05_apply_patch/code.py --demo
python s05_apply_patch/code.py --apply /path/to/workdir < patch.txt
```

## 对应真实源码

- `codex-rs/apply-patch/src/parser.rs`、`seek_sequence.rs`、`file_update.rs`
- `codex-rs/core/src/tools/handlers/apply_patch.lark` —— 交给模型的那份文法

## 下一章

"改"解决了。[s06](../s06_unified_exec/) 解决另一半：一个能活过单次工具调用的 shell。
