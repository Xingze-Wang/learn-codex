# 开始之前：你需要懂的全部东西

[English](PRIMER.md) · [中文](PRIMER-zh.md)

← 回到 [README](README-zh.md)

---

这一页是给**没写过代码**、但想搞懂 AI agent 到底是怎么运作的人写的。

读完它，你能看懂后面十五章里的每一段代码在说什么。它大概二十分钟。

如果你已经会写 Python，跳过它，直接去 [s01](s01_agent_loop/README.zh.md)。

---

## 先说一件让你安心的事

这个仓库讲的东西，**你已经在手动做了**。

你问 ChatGPT：「帮我看看这个文件夹里有什么。」它回答：「你可以运行 `ls`。」
你打开终端，敲 `ls`，把结果复制粘贴回去。它接着说下一步。

**你就是那个「循环」。** 你在充当模型和你电脑之间的那根线。

这十五章讲的，全部是：**怎么用代码替代你这根线，以及替代之后会冒出哪些新麻烦。**

每一章都是这个形状：

> 「如果你手动做这件事，第 X 步会很烦 / 会出错 / 会有危险。所以代码要这样处理。」

所以你读的时候，**先问自己「我手动做会怎样」**，答案往往就在那里。

---

## 一、什么是「程序」和「终端」

**程序**就是一个文本文件，里面写着一步一步的指令。计算机从上往下照做。

**终端**（Terminal）是那个黑色窗口，你在里面打字，让计算机做事。你敲：

```bash
ls
```

按回车，它列出当前文件夹里的东西。就这样。

本仓库里的每一章都是一个可以运行的程序文件，叫 `code.py`。你这样运行它：

```bash
python3 s05_apply_patch/code.py --demo
```

翻译成人话：**「Python 啊，请执行 `s05_apply_patch` 文件夹里那个 `code.py` 文件，
并且告诉它我要看 demo。」**

`--demo` 这种东西叫**参数**，就是你顺便递给程序的一张纸条。

---

## 二、Python 长什么样

Python 是一种编程语言。它刻意写得像英文。你只需要认识六样东西。

### 1. 变量 —— 给一个东西起名字

```python
cmd = "ls -la"
```

读作：**「从现在起，`cmd` 这个名字指的就是 `ls -la` 这段文字。」**

引号里的东西叫**字符串**（string），就是「一段文字」。

### 2. 列表 —— 一排东西，有顺序

```python
history = []                       # 一个空列表
history.append("你好")              # 往后面加一个
history.append("我在")              # 再加一个
# 现在 history 是 ["你好", "我在"]
```

`append` 就是「追加到末尾」。

**这个仓库里最重要的东西就是一个列表**，它叫 `history`（历史），
里面按顺序装着整段对话。

### 3. 字典 —— 带标签的一堆东西

```python
call = {"name": "exec_command", "cmd": "ls"}
```

读作：**「这个东西有两个标签：`name` 是 `exec_command`，`cmd` 是 `ls`。」**

取出来：

```python
call["name"]        # 得到 "exec_command"
```

冒号左边叫**键**（key），右边叫**值**（value）。

字典可以套字典、套列表，套多少层都行。你后面会看到很多。

### 4. 函数 —— 一段起了名字、可以反复用的活儿

```python
def exec_command(cmd, workdir=None):
    ...做事...
    return "命令跑完了"
```

- `def` = define，「我要定义一个函数」
- `exec_command` = 这个函数叫什么
- 括号里是它需要你给的东西（叫**参数**）
- `workdir=None` 表示这个参数**可以不给**，不给就是 `None`（空）
- `return` = 把结果交回去

用它：

```python
result = exec_command("ls")
```

### 5. if —— 如果

```python
if not calls:
    return last_message
```

读作：**「如果 `calls` 是空的，那就把 `last_message` 交回去，本函数结束。」**

（在 Python 里，空列表 `[]`、空字符串 `""`、数字 `0` 都算「假」。
所以 `not calls` 就是「calls 里没东西」。）

### 6. for 和 while —— 重复

```python
for call in calls:          # 把 calls 里的东西一个一个拿出来，每个都做一遍
    print(call)
```

```python
while True:                 # 一直重复，直到里面出现 return 或 break
    ...
```

**`while True` 是这整个仓库的心脏。** s01 那三十行代码就是一个 `while True`。

### 就这些

还有两样你会看到但**不必真懂**的：

```python
@dataclass(frozen=True)     # 「这是一个装数据的盒子，而且造好之后不许改」
class TurnContext:
    cwd: str                # 有个叫 cwd 的格子，里面放文字
    model: str
```

把它当成「一个规定好了有哪些格子的字典」就行。`frozen=True` 是「封死，不许改」。

```python
def foo(x: str) -> bool:    # 冒号后面那些是"类型标注"
```

`x: str` = 「x 应该是一段文字」，`-> bool` = 「这个函数会交回一个是/否」。
**它们纯粹是写给人看的注释**，删掉程序照样跑。看不懂就跳过。

---

## 三、什么是 JSON

**JSON 就是「把字典写成一段文字」的标准写法。**

Python 里的字典：

```python
{"name": "exec_command", "cmd": "ls"}
```

写成 JSON（就是一段文字）：

```json
{"name": "exec_command", "cmd": "ls"}
```

……长得一模一样。这不是巧合，JSON 就是照着这种写法定的。

**为什么需要它？** 因为你的程序和另一台电脑上的程序要交换数据，
而网线上只能传文字。所以：

```
你的字典  --转成文字-->  网络  --转回字典-->  对方的程序
          (json.dumps)          (json.loads)
```

- `json.dumps(x)` = dump string，把东西**变成**文字
- `json.loads(s)` = load string，把文字**变回**东西

你会在代码里反复看到这两个。

---

## 四、什么是「调用 API」

**就是给别人的电脑打个电话，问一件事，等它回答。**

```python
self._client.responses.create(
    model="gpt-5.5",
    input=input_items,
    tools=tools,
)
```

读作：**「喂，OpenAI 吗？我用 gpt-5.5 这个模型，
这是我们之前聊的全部内容，这是它能用的工具清单。请回答。」**

对方回一段文字，你的程序读它。**这就是全部。**

「模型」在这里始终是**别人机器上的一个服务**。这个仓库里的所有代码，
都是围着「怎么问它、怎么处理它的回答」转。

### 「流式」是什么意思

对方回答可能要十秒。有两种拿法：

- **等它说完再一次性给你** —— 你盯着空屏幕十秒
- **它说一个字就给你一个字** —— 你看着它打字

第二种叫**流式**（streaming）。代码里长这样：

```python
for event in self.client.stream(...):     # 一小块一小块地拿
```

每一小块叫一个**事件**（event）。

---

## 五、什么是「进程」

你在终端里敲 `ls` 回车，计算机就**启动了一个进程**：
一个正在运行的程序实例。它跑完，就结束了。

每个进程有三根管子和一个成绩：

| | 是什么 |
|---|---|
| **stdin**（标准输入） | 别人塞给它的东西 |
| **stdout**（标准输出） | 它正常打印出来的东西 |
| **stderr**（标准错误） | 它抱怨、报错的东西 |
| **退出码**（exit code） | 一个数字：**0 = 成功**，其它都是失败 |

在 Python 里启动一个进程：

```python
proc = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True)
proc.returncode     # 退出码
proc.stdout         # 它打印了什么
```

**这四样东西撑起了这个仓库的一大半。**
[s06](s06_unified_exec/README.zh.md) 讲进程怎么活得更久，
[s07](s07_sandbox/README.zh.md) 讲怎么限制进程能干什么，
[s13](s13_mcp/README.zh.md) 讲怎么用 stdin/stdout 和另一个进程对话，
[s14](s14_hooks/README.zh.md) 讲怎么让**你写的**进程插一脚。

---

## 六、什么是 async / await

只有三章（[s02](s02_protocol/README.zh.md)、[s08](s08_approval/README.zh.md)、
[s15](s15_harness/README.zh.md)）用到它。你只需要懂这一件事：

**普通的函数：叫它，它跑，跑完还给你。这中间你什么都干不了。**

```python
result = run_turn("帮我修这个 bug")     # 卡在这里，五分钟
```

**async 函数：它可以在中间「停一下，让别人先做点事」。**

```python
result = await run_turn("帮我修这个 bug")   # 停在这里，但别人能插进来
```

`await` 这个词的意思是：**「我在这儿等，但你们别管我，该干嘛干嘛。」**

为什么需要它？因为 agent 跑到一半的时候，你可能想：

- 打断它（「停」）
- 插一句话（「不对，是另一个文件」）
- 回答它的提问（「这条命令危险，能跑吗？」）

如果它是普通函数，这三件事**一件都做不到** —— 函数没跑完，你插不进去。
这就是 [s02](s02_protocol/README.zh.md) 那一章的全部内容。

看到 `async def` 就读成「这个函数可以中途让位」，
看到 `await` 就读成「在这儿等，但不挡别人的路」。够用了。

---

## 七、这个仓库反复出现的五个词

这五个词是全仓库的骨架。记住它们，剩下的都好办。

| 词 | 大白话 |
|---|---|
| **item**（项） | 对话里的一小块东西。可能是一句话、一次工具调用、一段思考 |
| **history**（历史） | 一个列表，按顺序装着所有 item。**整段对话就是它** |
| **turn**（一轮） | 从你说一句话开始，到 agent 不再干活为止。中间可能跑了十条命令 |
| **tool**（工具） | agent 能做的一个动作。这个仓库里主要就两个：跑命令、改文件 |
| **event**（事件） | agent 报告「我正在做什么」的一条消息。界面靠它画东西 |

一句话串起来：

> 你说一句话 → 它进 **history** → 开始一 **turn** →
> 模型要求用某个 **tool** → 程序执行 → 结果进 **history** → 再问模型 →
> 一路上不断发出 **event** 让界面显示 → 模型不再要求工具 → 这 **turn** 结束。

**这就是 [s01](s01_agent_loop/README.zh.md)。** 后面十四章都在给这条链路的某个环节打补丁。

---

## 八、术语表

按你会遇到的顺序排。不用背，看到了回来查。

| 词 | 意思 |
|---|---|
| 字符串 string | 一段文字 |
| 列表 list | 一排有顺序的东西 |
| 字典 dict | 一堆带标签的东西 |
| 函数 function | 一段起了名字、可以反复用的活儿 |
| 参数 argument | 你调用函数时递给它的东西 |
| 返回 return | 函数把结果交回去 |
| JSON | 把字典写成文字的标准写法 |
| API | 别人电脑上的服务，你打电话问它 |
| 流式 streaming | 回答一点一点地来，而不是一次性给完 |
| 进程 process | 一个正在运行的程序 |
| stdin / stdout / stderr | 进程的输入管 / 输出管 / 抱怨管 |
| 退出码 exit code | 进程结束时的成绩，0 = 成功 |
| shell | 解释你在终端里敲的命令的那个程序（bash、zsh） |
| 内核 kernel | 操作系统最核心的那一层，管着谁能碰什么 |
| 沙箱 sandbox | 内核给一个进程套上的一层限制 |
| PTY | 一个假终端，让程序以为自己接在真终端上（[s06](s06_unified_exec/README.zh.md)） |
| token | 模型数东西的单位，大概相当于半个到一个英文单词 |
| 上下文窗口 context window | 模型一次最多能读多少 token |
| patch | 一份「把这几行换成那几行」的说明书（[s05](s05_apply_patch/README.zh.md)） |
| schema | 一份「这个东西应该长什么样」的说明（[s04](s04_tool_registry/README.zh.md)） |
| JSON-RPC | 用 JSON 打电话的一种简单格式（[s13](s13_mcp/README.zh.md)） |
| MCP | 一个约定，让别人写的程序给 agent 提供工具（[s13](s13_mcp/README.zh.md)） |
| hook | 你写的一个小程序，被塞进 agent 的执行路径里（[s14](s14_hooks/README.zh.md)） |
| 事件循环 event loop | Python 给异步代码用的调度器，决定哪个暂停中的 `await` 下一个跑 |
| 正则 regex | 一种描述文本模式的小语言，比如 `^exec_command$` |
| harness | 围绕模型的那一整套代码。**这个仓库讲的就是它** |

---

## 九、三条阅读路线

**A. 完全没写过代码**

先跑起来，看它真的动。每章只读三节：**问题** → **解决方案** → **试一下**。
跳过「工作原理」里的代码。

从这四章开始，它们不需要 API key，跑起来就有东西看：

```bash
python3 s05_apply_patch/code.py --demo      # 看一份 patch 怎么改文件
python3 s06_unified_exec/code.py --demo     # 看一个 Python 会话被开起来、对话、关掉
python3 s07_sandbox/code.py --demo          # 看操作系统真的拦住一次写操作
python3 s13_mcp/code.py --demo              # 看两个程序用 JSON 互相说话
```

**B. 会写代码，但没做过 agent**

从 [s01](s01_agent_loop/README.zh.md) 顺着读到 [s15](s15_harness/README.zh.md)。
每章的「工作原理」都值得逐行看。

**C. 想去读真正的 Rust 源码**

先读每章的「问题」和「解决方案」建立地图，
然后按每章末尾的「对应真实源码」去 `codex-rs` 里对照。

---

准备好了就去 [s01: 一个循环 + 一个 shell](s01_agent_loop/README.zh.md)。
