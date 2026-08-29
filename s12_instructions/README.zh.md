# s12: Instructions —— 总是适用的加载，有时适用的挂牌子

[English](README.md) · [中文](README.zh.md)

[s11](../s11_compaction/) → `s12` → [s13](../s13_mcp/) → ... → [s15](../s15_harness/)

> *"总是适用的指令就加载进来；有时适用的指令只挂个牌子。"*
>
> **Harness 层**：知识 —— agent 该知道什么，什么时候知道。

---

## 问题

十一章了，`BASE_INSTRUCTIONS` 一直是一个硬编码的字符串。真实情况要复杂得多。

**问题一：一个仓库怎么告诉 agent「这里的规矩」？**

「这个项目用 pnpm 不用 npm」「测试跑 `make test` 不是 `pytest`」「别动 vendor/」——
这些东西不该每次对话都由你重新说一遍。

而且在 monorepo 里更麻烦：根目录的规矩是 Python，但 `services/api/` 那个服务是 Go 写的。
**谁覆盖谁？**

**问题二：一份 2000 token 的操作手册，怎么给 agent？**

比如「发版流程」：改 changelog、打 tag、跑发布脚本、通知频道。
写进系统提示词？那它在**每一次请求**里都要花 2000 token —— 而 99% 的对话跟发版毫无关系。
不写？那 agent 就不知道有这个流程。

---

## 先理解：一条指令有几种「通道」

发给模型的东西不只有一个字符串。它至少有三条通道，**优先级不同**：

| 通道 | 放什么 | 谁能写 |
|---|---|---|
| `instructions` 字段 | 这个模型家族的基础提示词 | 只有 Codex 自己 |
| `developer` 角色的消息 | 权限、环境 —— **harness 的事实** | 只有 harness |
| `user` 角色的消息 | AGENTS.md、skills 索引 | 项目文件、用户 |

**为什么 harness 的事实要走 developer 通道？** 因为它们的优先级高于任何项目文件里的东西。

一个仓库的 `AGENTS.md` 可以提很多要求，但它**没法给自己授予**「工作区之外的写权限」——
那件事由 [s07](../s07_sandbox/) 的内核和 developer 通道里的 `<permissions>` 说了算。

```python
def developer_item(text: str) -> dict[str, Any]:
    """harness 的事实走 developer 通道；它们的优先级高于用户文本。"""
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    }
```

---

## 解决方案（问题一）：AGENTS.md 是被发现的，不是被配置的

规则只有四条：

```
1. 从 cwd 往上走，直到找到项目根标记（.git）
2. 从那个根一路向下收集到 cwd（含）的每一个 AGENTS.md
3. 按这个顺序拼接 —— 最近的文件在最后，所以它赢
4. 绝不走过项目根
```

跑 demo 看效果：

```
在仓库根：                     在 services/api 里：
  ~/AGENTS.md                    ~/AGENTS.md
  AGENTS.md                      AGENTS.md
                                 services/api/AGENTS.md
```

**分层就是全部设计**：monorepo 根文件定下整体风格，`services/api/AGENTS.md` 为那个服务覆盖它，
而两个文件**谁都不需要知道对方存在**。

第 4 条是让它不失控的关键：

```python
def find_project_root(cwd, markers=PROJECT_ROOT_MARKERS) -> Path:
    """最近的、带标记的祖先目录；一个都没有就是 cwd 本身。

    没有这个边界，一个在 `/Users/me/code/x` 里启动的会话会捡起 `/Users/me` 下的
    一份 AGENTS.md，把某人不相干的笔记套到这台机器上的每一个项目上。"""
    cwd = Path(cwd).resolve()
    for candidate in (cwd, *cwd.parents):
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    return cwd
```

收集的顺序也很直白 —— **根在前，cwd 在后**，因为后面的覆盖前面的：

```python
for directory in reversed(directories):  # root first, cwd last
    override = directory / AGENTS_OVERRIDE_FILENAME
    target = override if override.is_file() else directory / AGENTS_FILENAME
    if target.is_file():
        docs.append(AgentsDoc(target, _read(target)))
```

（`AGENTS.override.md` 是本地覆盖，用于「我个人不想守这条共享规矩」，且不进 git。）

---

## 解决方案（问题二）：skills 用的是反过来的招

一个 skill 就是一个目录，里面一个 `SKILL.md`，头部是 YAML frontmatter：

```markdown
---
name: "release"
description: "发一个版本：changelog、打 tag、发布。"
---

# 发布流程
... 两千 token 的操作细节 ...
```

**只有 name 和 description 进入提示词。** 正文一个字都不进。

```
skill 文件在磁盘上：2600 字符
进入提示词的：181 字符

正文只有在 agent 真的对那条路径跑 `cat` 时才会被读。
```

解析器也确实只读 frontmatter：

```python
def parse_skill(path: Path, scope: str) -> Skill | None:
    """只解析 frontmatter。正文从不解析 —— 这正是重点。"""
    ...
    if not description:
        return None  # 一个模型无法判断的 skill，比没有这个 skill 更糟
```

最后那一行值得停一下：**没有 description 的 skill 会被直接丢掉。**
因为模型是靠这一行决定「这个 skill 跟当前任务有没有关系」的。
没有它，这个 skill 要么永远不被用，要么被乱用。

于是：**一百个 skill 花掉的是一百行上下文，不是一百份文档。**

---

## 两个问题，同一笔账

```
AGENTS.md：总是适用    -> 总是加载
skills：  有时适用    -> 挂个牌子，需要时用 shell 去取
```

这是同一个权衡的两端。判断标准就一个：
**这条信息是不是每一轮都相关？** 是就加载，不是就挂牌子。

---

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `find_project_root` | 有边界的标记回溯 |
| `discover_agents_docs` | 先用户级文件，再从根到 cwd |
| `parse_skill` | 只解析 frontmatter |
| `build_prompt` | 四个来源，各走各的通道 |

---

## 试一下

**不需要 API key。** 先看分层：

```bash
python s12_instructions/code.py --demo
```

它会造一个假的 monorepo（根是 Python 规矩，`services/api` 是 Go 规矩），
分别从两个目录发现一次，然后展示一个 skill 的「磁盘大小 vs 入 prompt 大小」。

**观察重点**：两次发现的列表 —— 在 `services/api` 里比在根目录多了一个文件，
而且它在**最后**（所以它赢）。

然后看你**真实**的配置：

```bash
python s12_instructions/code.py --show .
```

它会读你的 `~/.codex/skills/` 和当前项目的 `AGENTS.md`，
把组装出来的 prompt 打印出来 —— 包括每一项走的是哪条通道。

---

## 对应真实源码

- `codex-rs/core/src/agents_md.rs` —— 发现算法就写在它的文档注释里
- `codex-rs/core/src/skills.rs`、`codex-rs/skills/src/parser.rs`
- `codex-rs/core/src/context/` —— 每个注入块一个文件

---

## 接下来

知识可以按需加载了。**工具还不行。**

一个团队要用他们自己的工单系统、部署 API、内部搜索。这些东西 Codex 不该自带 ——
一个编码 agent 没有理由内置一个 Jira 客户端。

[s13](../s13_mcp/) 讲外部工具怎么接进来，以及它们带来的三个 harness 问题：
**名字会撞、慢的会拖垮启动、工具本身就是上下文。**
