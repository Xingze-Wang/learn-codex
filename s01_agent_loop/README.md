# s01: Agent Loop — one loop, one shell

[English](README.md) · [中文](README.zh.md)

`s01` → [s02](../s02_protocol/) → s03 → ... → [s15](../s15_harness/)

> *"One loop, one shell."*
>
> **Harness layer**: the loop — the first wire between the model and the real world.

---

## The problem

You want the model to do something for you: *"see which Python files are in this directory, then
run the tests."*

The model can write `ls *.py` and `pytest`. But then it stops — it cannot run them, and it
cannot see the output.

So you run them yourself and paste the results back. It reads them and says *"three tests fail,
let me look at the first one"*, and writes another command. You run that one too, and paste
again. And again.

**Every round trip, you are the middleman.** Replacing that middleman with thirty lines of code
is this chapter.

---

## First: how a model "asks" for a command to run

A model cannot execute anything. It can only emit text.

But the OpenAI API has a convention for this, called **function calling**: you attach a list of
tools to your request — what functions exist, what arguments each takes. If the model wants to
use one, what it emits is not a sentence but a structured item:

```json
{"type": "function_call", "name": "exec_command",
 "arguments": "{\"cmd\": \"ls *.py\"}", "call_id": "call_abc"}
```

That is the model saying: **"please run `ls *.py` for me, and give me the result back under the
label `call_abc`."**

Your program — the harness — actually runs it, then appends the result as a new message:

```json
{"type": "function_call_output", "call_id": "call_abc", "output": "app.py\ntest_app.py\n"}
```

`call_id` is the key that pairs a request with its result. A single reply can ask for several
commands at once, and the labels are how they stay matched up.

**Two words to remember, because all fourteen later chapters use them:**

- Everything the model produces — a message, its reasoning, a function call — is one **item**.
- The whole conversation is a list of items. We call it `history`.

---

## The solution

A `while True`: keep going while the model asks for tools, stop when it does not.

```
  +-----------+   send the history    +-------+
  | history[] | --------------------> | model |
  +-----------+                       +---+---+
       ^                                  |
       |                                  v
       |                     is there a function_call in it?
       |                       /                       \
       |                     yes                        no
       |                      |                          |
       |                run the command             the turn ends
       |                      |
       +--- append function_call_output ---+
```

| Signal | Meaning | What the loop does |
|---|---|---|
| reply contains a `function_call` | the model wants a command run | run it → append the output → send again |
| reply contains none | the model is done talking | leave the loop, hand the last message to the user |

---

## How it works

Step by step.

**Step 1**: put the user's words into `history` as the first item.

```python
self.history.append({
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": user_text}],
})
```

Note it is not a bare string but a structure with a `type`. Everything in `history` looks like
this.

**Step 2**: send the history and the tool list to the model.

```python
for event in self.client.stream(
    instructions=BASE_INSTRUCTIONS,      # the system prompt
    input_items=list(self.history),      # the whole conversation
    tools=[EXEC_COMMAND_TOOL],           # the tool list
):
```

`stream` means the reply arrives in pieces rather than all at once, so the user watches it type
instead of staring at a blank screen for ten seconds. Each piece is an **event**.

**Step 3**: handle the events. There are only three kinds.

```python
if isinstance(event, OutputTextDelta):
    print(event.delta, end="", flush=True)      # a fragment of text, print it
elif isinstance(event, OutputItemDone):
    self.history.append(event.item)             # one complete item is finished
    if event.item.get("type") == "function_call":
        calls.append(event.item)                # remember it; we run it below
elif isinstance(event, Completed):
    self.tokens += event.input_tokens + event.output_tokens
```

The middle line is the important one: **every item the model produced goes back into history**,
whether it is a message, its reasoning, or a tool call. Why *every* item matters comes below.

**Step 4**: if there were no function calls, the turn is over.

```python
if not calls:
    return last_message
```

That is the entire exit condition. No turn limit, no "is the task complete?" check — **the model
not asking for a tool is the end.**

**Step 5**: otherwise, run them.

```python
for call in calls:
    output = self._dispatch(call, echo=echo)
    self.history.append({
        "type": "function_call_output",
        "call_id": call["call_id"],
        "output": output,
    })
```

The `call_id` goes back unchanged, so the model knows which of its requests this answers.

**Step 6**: go back to step 2. `history` is now three items longer — the user's words, the
model's call, the command's output — so the next request lets the model see what the command
actually printed.

Assembled, that is all of it:

```python
def run_turn(self, user_text: str) -> str:
    self.history.append(user_item(user_text))
    while True:
        calls = []
        for event in self.client.stream(
            instructions=BASE_INSTRUCTIONS,
            input_items=list(self.history),
            tools=[EXEC_COMMAND_TOOL],
        ):
            if isinstance(event, OutputItemDone):
                self.history.append(event.item)
                if event.item.get("type") == "function_call":
                    calls.append(event.item)

        if not calls:
            return last_message

        for call in calls:
            self.history.append({
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": self._dispatch(call),
            })
```

Thirty lines and you have a working agent. **All fourteen later chapters are built around this
loop, and none of them change it.**

---

## The two fields that make it Codex

Two fields in the request body differ from how most tutorials write this.

```python
request = {
    "model": self.model,
    "instructions": instructions,
    "input": input_items,
    "tools": tools,
    "tool_choice": "auto",
    "store": False,                                  # <--
    "stream": True,
    "include": ["reasoning.encrypted_content"],      # <--
}
```

### `store: false` — the server remembers nothing

Many APIs let you send only the new message and keep the earlier conversation server-side.
Codex switches that off: **every request re-sends the entire conversation.**

That sounds wasteful. What it buys is the premise of half this repo:

**The harness owns the history.** It is an ordinary list in your process memory. So you can:

- write it to disk and continue tomorrow ([s10](../s10_rollout/));
- replace its middle with a summary when it gets too long ([s11](../s11_compaction/));
- cut it at turn three and copy out two different futures ([s10](../s10_rollout/)'s fork).

If the history were "an id on someone else's server", you could do none of those.

### `include: ["reasoning.encrypted_content"]` — the encrypted thinking

Reasoning models think before they answer. You are not allowed to read that thinking, but it has
to be back in the model's hands on the next request, or it loses its own train of thought
mid-task.

So the API returns it encrypted and Codex echoes it back verbatim. Which is why step 3's line
reads the way it does:

```python
elif isinstance(event, OutputItemDone):
    self.history.append(event.item)      # don't parse it, don't rebuild it, just keep it
```

The only edit is dropping `id` — with `store: false` the server has no memory of those ids:

```python
raw.pop("id", None)
raw.pop("status", None)
```

---

## One tool, deliberately

The tool list has exactly one entry:

```python
EXEC_COMMAND_TOOL = {
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
            "workdir": {"type": "string", "description": "Working directory."},
        },
        "required": ["cmd"],
        "additionalProperties": False,
    },
}
```

No `read_file`, no `list_directory`, no `search`. `cat`, `ls` and `rg` already exist on that
machine and the model already knows them. And **every tool you do not define is a schema you do
not send on every single request.**

Codex added exactly one more file tool later — `apply_patch` ([s05](../s05_apply_patch/)) — and
it exists for one reason: *writing* is the thing a shell one-liner is genuinely bad at.

---

## The loop must never raise

Nothing in `_dispatch` throws:

```python
try:
    args = json.loads(call.get("arguments") or "{}")
except json.JSONDecodeError as exc:
    return f"invalid arguments: {exc}"
```

Malformed arguments, a missing binary, a non-zero exit — all of them become text inside a
`function_call_output`.

Why? Because **the model reads its own mistake on the next turn and fixes it.** Raising here
would end the whole session over something the model could have handled itself.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `ResponsesClient` | The live path: one streaming request, SSE events turned into three Python objects |
| `OutputTextDelta` / `OutputItemDone` / `Completed` | Those three events (codex calls this enum `ResponseEvent`) |
| `exec_command` | Run one command, capture output, cap its size |
| `Session.run_turn` | The loop above |

---

## Try it

> **Safety note**: this code executes shell commands the model wrote, with **no protection at
> all**. Run it in a scratch directory. [s07](../s07_sandbox/) adds the sandbox.

Setup:

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...
```

Run:

```bash
python s01_agent_loop/code.py "how many python files are under this directory?"
python s01_agent_loop/code.py            # interactive
```

Try these:

1. `Create a hello.py that prints Hello, World!`
2. `Which file in this repo is the largest?`
3. `What git branch am I on?`

**What to watch**: count how many commands it runs before answering. Question 2 will usually
`ls` first and then `du` — that is the loop going around twice. And notice exactly when it
stops: the moment it stops asking for commands.

---

## Real source

- `codex-rs/core/src/client.rs` — request construction, SSE handling
- `codex-rs/core/src/session/turn.rs` — `run_turn`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` — the real `exec_command` schema

---

## Next

This loop has one flaw: once it starts, all you can do is wait.

It goes the wrong way and you want to redirect it — there is no door. You want it to stop —
no door. It wants to ask you *"this command looks risky, may I?"* — also no door, because a
function can only return once.

[s02](../s02_protocol/) puts the loop behind two queues. **Interrupting, steering and approving
all become possible at once** — and it turns out they are the same thing.
