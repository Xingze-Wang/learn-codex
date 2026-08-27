# s12：Instructions —— 基础提示词、AGENTS.md 与 skills

[English](README.md) · [中文](README.zh.md)

[s11](../s11_compaction/) → `s12` → [s13](../s13_mcp/)

> *"总是适用的指令就加载进来；有时适用的指令只挂个牌子。"*

---

十一章都在传一个硬编码的 `BASE_INSTRUCTIONS`。真实会话组装的是四个来源，
而每一个走**哪条通道**和顺序一样是刻意的：

```
instructions 字段   这个模型家族的基础提示词
developer 消息      权限、环境 —— harness 的事实（s03）
user 消息           AGENTS.md，从项目根到 cwd 依次拼接
user 消息           skills 索引：只有名字和一行描述
```

harness 的事实走 developer 通道，因为它们的优先级高于任何项目文件里的东西。
一个仓库的 `AGENTS.md` 可以提很多要求，但它没法给自己授予"工作区之外的写权限"。

## AGENTS.md 是被发现的，不是被配置的

```
1. 从 cwd 往上走，直到找到项目根标记（.git）
2. 从那个根一路向下收集到 cwd（含）的每一个 AGENTS.md
3. 按这个顺序拼接 —— 最近的文件在最后，所以它赢
4. 绝不走过项目根
```

```
在仓库根：                     在 services/api 里：
  ~/AGENTS.md                    ~/AGENTS.md
  AGENTS.md                      AGENTS.md
                                 services/api/AGENTS.md
```

分层就是全部设计：monorepo 根文件定下整体风格，`services/api/AGENTS.md` 为那个服务覆盖它，
而两个文件谁都不需要知道对方存在。

第 4 条是让它不失控的关键：

```python
"""最近的、带标记的祖先目录；一个都没有就是 cwd 本身。

没有这个边界，一个在 `/Users/me/code/x` 里启动的会话会捡起 `/Users/me` 下的
一份 AGENTS.md，把某人不相干的笔记套到这台机器上的每一个项目上。"""
```

## Skills 用的是反过来的招

一个 skill 就是一个目录，里面的 `SKILL.md` 用 YAML frontmatter 带上名字和描述：

```markdown
---
name: "release"
description: "发一个版本：changelog、打 tag、发布。"
---

# 发布流程
... 两千 token 的操作细节 ...
```

进入提示词的只有名字和描述：

```
skill 文件在磁盘上：2600 字符
进入提示词的：181 字符

正文只有在 agent 真的对那条路径跑 `cat` 时才会被读。
```

一百个 skill 花掉的是一百行上下文，不是一百份文档。
正文由 shell 去取，且只在模型判断这个 skill 适用时才取——而这个判断，一行描述就够它做了。

```python
if not description:
    return None  # 一个模型无法判断的 skill，比没有这个 skill 更糟
```

这和 AGENTS.md 是同一笔账，只是反过来：总是适用的指令总是被加载；
有时适用的指令挂个牌子、需要时再取。

## `code.py` 里有什么

| 部件 | 作用 |
|---|---|
| `find_project_root` | 有边界的标记回溯 |
| `discover_agents_docs` | 先用户级文件，再从根到 cwd |
| `parse_skill` | 只解析 frontmatter；正文从不解析 |
| `build_prompt` | 四个来源，各走各的通道 |

## 跑起来

```bash
python s12_instructions/code.py --demo          # 一个假的 monorepo，演示分层
python s12_instructions/code.py --show .        # 你真实的 ~/.codex skills 和 AGENTS.md
```

## 对应真实源码

- `codex-rs/core/src/agents_md.rs` —— 发现算法就写在它的文档注释里
- `codex-rs/core/src/skills.rs`、`codex-rs/skills/src/parser.rs`
- `codex-rs/core/src/context/` —— 每个注入块一个文件

## 下一章

知识可以按需加载。[s13](../s13_mcp/) 对工具做同一件事。
