# s01: Agent Loop — one loop, one shell, no server-side memory

[English](README.md) · [中文](README.zh.md)

`s01` → [s02](../s02_protocol/) → ... → [s15](../s15_harness/)

> *"One loop, one shell."*
>
> The model decides. The harness executes and hands back what happened.

---

Codex is a `while True` around one model call:

```
+-----------+   input items    +-------+   function_call   +-------+
| history[] | ---------------> | model | ----------------> | shell |
+-----------+                  +-------+                   +---+---+
     ^                             |                           |
     |      function_call_output   | no function_call          |
     +-----------------------------+------<--------------------+
                                   v
                              turn complete
```

Send the conversation. If the response contains a `function_call`, run it, append a
`function_call_output`, and send again. If it does not, the turn is over. Everything in the
next fourteen chapters is built around this loop — none of it changes the loop.

## The request is the whole contract

```python
request = {
    "model": self.model,
    "instructions": instructions,
    "input": input_items,
    "tools": tools,
    "tool_choice": "auto",
    "parallel_tool_calls": False,
    "store": False,
    "stream": True,
    "include": ["reasoning.encrypted_content"],
}
```

Two fields deserve attention.

**`store: false`.** The server remembers nothing between requests. Codex re-sends the entire
conversation every time. That sounds wasteful until you notice what it buys: the harness owns
the history. It can replay it (s10), rewrite it (s11), fork it, or inspect it, because the
history is a list in the client's memory rather than a handle to state on someone else's
machine.

**`include: ["reasoning.encrypted_content"]`.** Reasoning models produce reasoning items the
client is not allowed to read. With `store: false` they still have to come back on the next
request or the model loses its own train of thought mid-task. So they are returned encrypted,
and Codex echoes them back verbatim:

```python
elif isinstance(event, OutputItemDone):
    # Every item the model produced goes back into history --
    # messages, reasoning, function calls alike.
    self.history.append(event.item)
```

Items are round-tripped, not reconstructed. Codex strips `id` fields before resending them
(with `store: false` the server has no memory of those ids) and otherwise leaves them alone.

## One tool, deliberately

The tool list has one entry:

```python
EXEC_COMMAND_TOOL = {
    "type": "function",
    "name": "exec_command",
    "parameters": {"properties": {"cmd": {...}, "workdir": {...}}, "required": ["cmd"]},
}
```

There is no `read_file`, no `list_directory`, no `search`. `cat`, `ls`, and `rg` already exist,
the model already knows them, and every tool you do not define is a tool schema you do not send
on every request. Codex adds exactly one more file tool later (`apply_patch`, s05) and it exists
because *writing* is where a shell one-liner is genuinely worse.

## What the loop must not do

Nothing in `_dispatch` raises:

```python
try:
    args = json.loads(call.get("arguments") or "{}")
except json.JSONDecodeError as exc:
    return f"invalid arguments: {exc}"
```

A malformed call, a missing binary, a non-zero exit — all of them become text in a
`function_call_output`. The model reads its own mistake on the next turn and corrects it. An
exception here would end the session over something the model could have fixed itself.

## In `code.py`

| Piece | Job |
|---|---|
| `ResponsesClient` | The live path: one streaming Responses request |
| `OutputTextDelta` / `OutputItemDone` / `Completed` | Normalized stream events (codex calls this `ResponseEvent`) |
| `exec_command` | Run one command, capture output, cap its size |
| `Session.run_turn` | The loop |

## Run it

```bash
export OPENAI_API_KEY=...
python s01_agent_loop/code.py "count the python files under ."
python s01_agent_loop/code.py            # interactive
```

## Real source

- `codex-rs/core/src/client.rs` — request construction, SSE handling
- `codex-rs/core/src/session/turn.rs` — `run_turn`
- `codex-rs/core/src/tools/handlers/shell_spec.rs` — the real `exec_command` schema

## Next

The loop works, but only for a caller willing to block until it finishes. [s02](../s02_protocol/)
wraps it in two queues, and interruption, steering, and approvals all become possible.
