# Look-up page

[English](PRIMER.md) · [中文](PRIMER-zh.md)

← back to the [README](README.md)

---

> **This is not a "read this first".**
>
> Every word in the fifteen chapters is explained where it first appears. You do not need to
> learn anything before starting — go straight to [s01](s01_agent_loop/README.md).
>
> This page is only a **place to look things up** when something stops you mid-read. You can
> also never open it.

---

## First, something reassuring

Everything this repo teaches, **you already do by hand.**

You ask ChatGPT: "what's in this folder?" It answers: "you could run `ls`." You open a terminal,
type `ls`, copy the output, paste it back. It tells you the next step.

**You are the loop.** You are the wire between the model and your computer.

All fifteen chapters are about one thing: **replacing that wire with code, and the new problems
that appear once you do.**

Every chapter has this shape:

> "If you did this by hand, step X would be annoying / wrong / dangerous. So the code handles it
> like this."

So when you read, **ask yourself "what would I do by hand?"** first. The answer is usually right
there.

---

## What a "program" and a "terminal" are

A **program** is a text file with step-by-step instructions. The computer does them top to
bottom.

A **terminal** is that black window where you type things to make the computer do them. You
type:

```bash
ls
```

press Enter, and it lists what is in the current folder. That is it.

Every chapter here is a runnable program file called `code.py`. You run one like this:

```bash
python3 s05_apply_patch/code.py --demo
```

In plain words: **"Python, please run the file `code.py` inside the folder `s05_apply_patch`, and
tell it I want the demo."**

Something like `--demo` is called an **argument** — a note you hand the program on the way in.

---

## What Python looks like

> Every code block in every chapter is annotated line by line, so **you do not need to learn
> Python first.** What follows is just to give you a feel; come back if something stops you.

### Variables — giving something a name

```python
cmd = "ls -la"
```

Read it as: **"from now on, the name `cmd` means the text `ls -la`."**

The thing in quotes is a **string** — "a piece of text".

### Lists — several things, in order

```python
history = []                       # an empty list
history.append("hello")            # put one on the end
history.append("hi there")         # and another
# history is now ["hello", "hi there"]
```

`append` means "add to the end".

**The single most important thing in this repo is a list**, called `history`, holding the whole
conversation in order.

### Dictionaries — a bundle of labelled things

```python
call = {"name": "exec_command", "cmd": "ls"}
```

Read it as: **"this thing has two labels: `name` is `exec_command`, `cmd` is `ls`."**

Getting one back out:

```python
call["name"]        # gives you "exec_command"
```

The left side of the colon is a **key**, the right side a **value**.

Dictionaries can hold dictionaries, and lists, nested as deep as you like. You will see plenty.

### Functions — a named piece of work you can reuse

```python
def exec_command(cmd, workdir=None):
    ...do things...
    return "the command finished"
```

- `def` = define — "I am defining a function"
- `exec_command` = what it is called
- the parentheses hold what you must give it (its **arguments**)
- `workdir=None` means that one is **optional**; leave it out and it is `None` (nothing)
- `return` = hand the result back

Using it:

```python
result = exec_command("ls")
```

### if

```python
if not calls:
    return last_message
```

Read it as: **"if `calls` is empty, hand back `last_message` and stop here."**

(In Python an empty list `[]`, an empty string `""` and the number `0` all count as false. So
`not calls` means "there is nothing in calls".)

### for and while — repeating

```python
for call in calls:          # take the things in calls one at a time, do this to each
    print(call)
```

```python
while True:                 # repeat forever, until a return or break inside stops it
    ...
```

`while True` shows up in s01, annotated line by line where it appears. You do not have to
remember it.

### Two more

Two more you will see but do **not** need to truly understand:

```python
@dataclass(frozen=True)     # "this is a box that holds data, and cannot be changed once built"
class TurnContext:
    cwd: str                # it has a slot called cwd, holding text
    model: str
```

Think of it as "a dictionary whose slots are fixed in advance". `frozen=True` means "sealed, no
edits".

```python
def foo(x: str) -> bool:    # the bits after the colons are "type annotations"
```

`x: str` = "x should be text", `-> bool` = "this hands back a yes/no". **They are notes for
humans**; delete them and the program runs exactly the same. Skip them if they are noise.

---

## What JSON is

**JSON is the standard way of writing a dictionary down as text.**

A dictionary in Python:

```python
{"name": "exec_command", "cmd": "ls"}
```

The same thing as JSON (which is just text):

```json
{"name": "exec_command", "cmd": "ls"}
```

…almost identical. But avoid one misunderstanding: **JSON is not "a Python thing".**
It came from JavaScript, has no relation to Python, and the two just happen to have landed on
the same braces-and-colons notation.

This matters because later you will watch a program written in Rust talk to one written in
Python using JSON ([s13](s13_mcp/README.md)). They can, precisely because **JSON belongs to no
language.**

**Why is it needed?** Because your program and a program on another computer need to exchange
data, and only text travels over a network. So:

```
your dictionary --turn into text-->  network  --turn back into a dictionary--> their program
                 (json.dumps)                  (json.loads)
```

- `json.dumps(x)` = dump string — turn a thing **into** text
- `json.loads(s)` = load string — turn text **back into** a thing

You will see both constantly.

---

## What "calling an API" means

**It means phoning someone else's computer, asking a question, and waiting for the answer.**

```python
self._client.responses.create(
    model="gpt-5.5",
    input=input_items,
    tools=tools,
)
```

Read it as: **"Hello OpenAI. Use the model gpt-5.5. Here is everything we have said so far. Here
is the list of tools it may use. Please answer."**

They send back some text, and your program reads it. **That is the whole thing.**

"The model" is always **a service on someone else's machine**. Every line of code in this repo is
about how to ask it things and what to do with its answers.

### What "streaming" means

The answer might take ten seconds. There are two ways to receive it:

- **wait for the whole thing** — you stare at a blank screen for ten seconds
- **get each piece as it is written** — you watch it type

The second is **streaming**. In code it looks like:

```python
for event in self.client.stream(...):     # take it a piece at a time
```

Each piece is called an **event**.

---

## What a "process" is

Type `ls` and press Enter, and the computer **starts a process**: one running instance of a
program. It finishes, and it is gone.

Every process has three pipes and one grade:

| | What it is |
|---|---|
| **stdin** (standard input) | what someone feeds into it |
| **stdout** (standard output) | what it prints normally |
| **stderr** (standard error) | what it complains about |
| **exit code** | a number when it ends: **0 = success**, anything else = failure |

Starting one from Python:

```python
proc = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True)
proc.returncode     # the exit code
proc.stdout         # what it printed
```

**Those four things hold up half this repo.**
[s06](s06_unified_exec/README.md) is about making a process live longer,
[s07](s07_sandbox/README.md) about limiting what a process may do,
[s13](s13_mcp/README.md) about talking to another process through stdin/stdout,
[s14](s14_hooks/README.md) about letting **a process you wrote** join in.

---

## What async / await means

Only three chapters use it ([s02](s02_protocol/README.md),
[s08](s08_approval/README.md), [s15](s15_harness/README.md)). You need one idea:

**An ordinary function: you call it, it runs, it hands back. You can do nothing in between.**

```python
result = run_turn("fix this bug")     # stuck here, for five minutes
```

**An async function can pause in the middle and let other things happen.**

```python
result = await run_turn("fix this bug")   # paused here, but others can get in
```

The word `await` means: **"I am waiting here — carry on without me."**

Why is that needed? Because halfway through an agent run you may want to:

- stop it ("halt")
- correct it ("no, the other file")
- answer its question ("this command looks risky, may I?")

With an ordinary function, **none of those are possible** — the function has not returned, so
there is no way in. That is the entire subject of [s02](s02_protocol/README.md).

Read `async def` as "this function can step aside mid-run", and `await` as "wait here without
blocking anyone". That is enough.

---

## The five words this repo keeps using

These five are the skeleton. Know them and the rest follows.

| Word | In plain terms |
|---|---|
| **item** | one small piece of the conversation: a sentence, a tool call, a piece of thinking |
| **history** | a list holding every item in order. **The conversation *is* this list** |
| **turn** | from you saying something to the agent stopping. It may run ten commands in between |
| **tool** | one action the agent can take. Here, mostly two: run a command, edit a file |
| **event** | a message saying "here is what I am doing now". The interface draws from these |

Strung together:

> You say something → it goes into **history** → a **turn** starts →
> the model asks for a **tool** → the program runs it → the result goes into **history** →
> ask the model again → **events** stream out so the interface can show it →
> the model stops asking for tools → the **turn** ends.

**That is [s01](s01_agent_loop/README.md).** The other fourteen chapters each patch one link in
that chain.

---

## Glossary

In roughly the order you will meet them. Do not memorize; come back and look.

| Word | Meaning |
|---|---|
| string | a piece of text |
| list | several things, in order |
| dict | a bundle of labelled things |
| function | a named piece of work you can reuse |
| argument | what you hand a function when you call it |
| return | a function handing its result back |
| JSON | the standard way to write a dictionary as text |
| API | a service on someone else's computer that you phone |
| streaming | receiving an answer piece by piece rather than all at once |
| process | one running program |
| stdin / stdout / stderr | a process's input pipe / output pipe / complaints pipe |
| exit code | the grade a process ends with; 0 = success |
| shell | the program that interprets what you type in a terminal (bash, zsh) |
| kernel | the innermost layer of the operating system; it decides who may touch what |
| sandbox | restrictions the kernel wraps around a process |
| PTY | a fake terminal, so a program believes it is attached to a real one ([s06](s06_unified_exec/README.md)) |
| token | the unit the model counts in. In English, roughly half a word to a word; **in Chinese, closer to one or two characters**, so the same passage costs more |
| context window | how many tokens the model can read at once |
| patch | a set of instructions saying "replace these lines with those" ([s05](s05_apply_patch/README.md)) |
| schema | a description of what a thing is supposed to look like ([s04](s04_tool_registry/README.md)) |
| JSON-RPC | a simple format for phoning a program using JSON ([s13](s13_mcp/README.md)) |
| MCP | a convention letting someone else's program provide tools to an agent ([s13](s13_mcp/README.md)) |
| hook | a small program you wrote, wedged into the agent's path ([s14](s14_hooks/README.md)) |
| event loop | Python's scheduler for async code; it decides which paused `await` runs next |
| regex | a small pattern language for matching text, e.g. `^exec_command$` |
| harness | all the code surrounding the model. **This repo is about the harness** |

---

## Three ways to read this

**A. You have never written code**

Run things first and watch them move. Read only three sections per chapter:
**The problem** → **The solution** → **Try it**. Skip the code in "How it works".

Start with these four — no API key, and each one shows you something immediately:

```bash
python3 s05_apply_patch/code.py --demo      # watch a patch edit a file
python3 s06_unified_exec/code.py --demo     # watch a Python session open, talk, and close
python3 s07_sandbox/code.py --demo          # watch the OS actually refuse a write
python3 s13_mcp/code.py --demo              # watch two programs talk to each other in JSON
```

**B. You write code but have not built an agent**

Read [s01](s01_agent_loop/README.md) through [s15](s15_harness/README.md) in order. "How it
works" in each chapter is worth reading line by line.

**C. You want to read the real Rust source**

Read each chapter's "The problem" and "The solution" to build the map, then follow the
"Real source" links at the end of each chapter into `codex-rs`.

---

Whenever you feel like starting: [s01: one loop, one shell](s01_agent_loop/README.md).
