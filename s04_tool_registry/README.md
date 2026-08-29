# s04: The tool registry — assemble per turn, dispatch by name, never raise

[English](README.md) · [中文](README.zh.md)

[s03](../s03_turn_context/) → `s04` → [s05](../s05_apply_patch/) → ... → [s15](../s15_harness/)

> *"Assemble per turn. Dispatch by name. Never raise."*
>
> **Harness layer**: the tool layer — the agent's hands.

---

## The problem

s01 hard-coded the tool: one `EXEC_COMMAND_TOOL`, one `if name == "exec_command"`.

Now things need adding: `apply_patch` (edit files), `update_plan` (keep a plan), and however
many MCP tools ([s13](../s13_mcp/)). So the `if / elif` chain gets longer.

But length is not what breaks it. These three things vary **per turn**:

1. **The model varies.** Some models were trained on a tool called `exec_command`, others on
   `shell`, with different argument shapes. Hand over the wrong one and the model is calling
   something it never learned.
2. **The config varies.** The user turned web search off and connected three MCP servers — the
   list changes with them.
3. **The call shape varies.** Not every tool takes JSON.

The third is the easiest to miss, so start there.

---

## First: two kinds of tool, two kinds of payload

**Kind one: a function tool.** You supply a JSON Schema describing the arguments; the model
gives you back JSON.

```python
{
    "type": "function",
    "name": "exec_command",
    "description": "Runs a command in the workspace shell and returns its output.",
    "parameters": {                          # this is JSON Schema
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to execute."},
        },
        "required": ["cmd"],
        "additionalProperties": False,       # no extra fields allowed
    },
}
```

The model emits `{"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\": \"ls\"}"}`.
Note `arguments` is **a string** with JSON inside it.

**Kind two: a freeform tool**, called `custom` in the API. The model emits raw text with no JSON
wrapper, and what counts as valid text is fixed by a **grammar** (here, Lark syntax):

```python
{
    "type": "custom",
    "name": "apply_patch",
    "description": "... This is a FREEFORM tool, so do not wrap the patch in JSON.",
    "format": {"type": "grammar", "syntax": "lark", "definition": APPLY_PATCH_LARK},
}
```

The model emits `{"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch\n..."}`.
The payload is in `input`, as **a bare string**.

**Why does a patch need kind two?** Because putting a 200-line patch inside a JSON string field
means escaping every newline and quote in it. One escaping mistake by the model costs a whole
turn.

And the grammar binds during *decoding*: a syntactically invalid patch is not "rejected
downstream" — it **cannot be generated**.

---

## The solution

A registry: name → (spec, handler, can-it-run-in-parallel). Assembled per turn from config.

```
        ToolsConfig(shell_type="exec_command", apply_patch=True, ...)
                              │
                              ▼
                    build_registry(config)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      exec_command      apply_patch      update_plan
       (function)        (custom)        (function)
              │               │               │
              └───────────────┼───────────────┘
                              ▼
                   registry.specs()  ──►  the `tools` array in the request
                   registry.get(name) ──►  which handler to run
```

---

## How it works

**Step 1**: the model and the config decide what exists.

```python
def build_registry(config: ToolsConfig) -> ToolRegistry:
    registry = ToolRegistry()

    if config.shell_type == "shell":
        registry.register(RegisteredTool(SHELL, handle_shell, supports_parallel=False))
    else:
        registry.register(RegisteredTool(EXEC_COMMAND, handle_exec_command, supports_parallel=False))

    if config.apply_patch:
        registry.register(RegisteredTool(APPLY_PATCH, handle_apply_patch,
                                         supports_parallel=False, freeform=True))
    if config.plan_tool:
        registry.register(RegisteredTool(UPDATE_PLAN, handle_update_plan))
    return registry
```

`shell_type` is not invented — it is a field on the model record. Run `--list` to see a real
assembled list.

**Step 2**: `registry.specs()` is literally what goes into the request body.

```python
def specs(self) -> list[dict[str, Any]]:
    """Exactly what goes into the request body's `tools` array."""
    return [tool.spec for tool in self._tools.values()]
```

There is no second transformation in between. The JSON you see from `--list` is the JSON the
model receives.

**Step 3**: turn a response item into a call. This is the one place that knows the two shapes
apart.

```python
def build_tool_call(item: dict[str, Any]) -> ToolCall | None:
    if item.get("type") == "function_call":
        try:
            args = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise ToolError(f"invalid JSON arguments: {exc}") from None
        return ToolCall(item.get("name", ""), item.get("call_id", ""), args)

    if item.get("type") == "custom_tool_call":
        return ToolCall(item.get("name", ""), item.get("call_id", ""), item.get("input", ""))

    return None      # not a tool call (an ordinary message, or reasoning)
```

A `dict` payload means a function tool; a `str` payload means a freeform one. Downstream tells
them apart by that.

**Step 4**: dispatch — with errors contained at three levels.

```python
def dispatch(registry: ToolRegistry, call: ToolCall, ctx: ToolContext) -> str:
    tool = registry.get(call.name)
    if tool is None:
        # Not a crash: the model gets told and picks a tool that exists.
        return f"unsupported tool: {call.name}"
    try:
        return tool.handler(call.payload, ctx)
    except ToolError as exc:
        return f"error: {exc}"
    except Exception as exc:  # a bug in a handler must not kill the session
        return f"internal tool error: {type(exc).__name__}: {exc}"
```

Unknown name, handler-reported failure, and a bug inside a handler. All three become text going
back to the model.

---

## The subtler rule: even an unparseable call must be answered

Say the model emits `arguments: "{oops"`. Step 3 raises `ToolError`. Now what?

**You still have to answer that call.**

```python
except ToolError as exc:
    # The model emitted something unparseable. Answer the call
    # anyway -- an unanswered call_id breaks the next request.
    rejected += 1
    self.history.append({
        "type": "function_call_output",
        "call_id": item.get("call_id", ""),
        "output": f"error: {exc}",
    })
    continue

if not calls:
    if rejected:
        continue      # give the model a chance to fix its own call
    return last_message
```

Why? Because a `function_call` in history with no matching `function_call_output` makes the
conversation **malformed**. The next request fails, and the failure has nothing to do with the
model's actual mistake — you would go debug something that is not the problem.

Note that last `if rejected: continue`: answering is not enough. You have to **send another
request** so the model gets a chance to see its error and correct it.

---

## Output has a budget

One `make` can print forty thousand lines. Put all of it into history and the context window
[s11](../s11_compaction/) exists to protect is gone.

```python
def truncate_output(text: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    """Keep the head and the tail; the middle is where logs repeat themselves."""
    budget = max_tokens * CHARS_PER_TOKEN
    if len(text) <= budget:
        return text
    head = text[: budget // 2]
    tail = text[-budget // 2 :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n[... {dropped} characters truncated ...]\n{tail}"
```

Why head and tail? Because a build log's **first lines say what ran** and its **last lines say
how it failed**; the forty thousand in between are one warning repeated.

---

## Parallelism is a property of the tool, not the call site

```python
assert reg.supports_parallel("update_plan") is True
assert reg.supports_parallel("exec_command") is False
assert reg.supports_parallel("apply_patch") is False
```

Two patches racing on one file is a merge conflict with extra steps. Two shell commands racing
in one working directory is worse.

The registry here only *records* the flag; the loop still executes serially. The real router
uses it to batch the calls that may overlap (`ToolRouter::tool_supports_parallel`). Recording it
separately is the part worth copying.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `function_tool` / `freeform_tool` | The two spec shapes |
| `ToolRegistry` | name → handler, and `specs()` for the request |
| `ToolsConfig` / `build_registry` | Per-turn assembly |
| `build_tool_call` | `function_call` vs `custom_tool_call` |
| `dispatch` | Name lookup, error containment |
| `truncate_output` | Head/tail token budget |

---

## Try it

Look at the tool list that actually gets sent:

```bash
python s04_tool_registry/code.py --list
```

**What to watch**: scroll to the `apply_patch` entry. Its `type` is `custom`, not `function`,
and it carries a `format.definition` — that is the complete grammar for the patches in
[s05](../s05_apply_patch/).

Then run it for real:

```bash
python s04_tool_registry/code.py "how many lines of python are here?"
```

---

## Real source

- `codex-rs/core/src/tools/registry.rs`, `router.rs`
- `codex-rs/tools/src/tool_spec.rs` — `ToolSpec::{Function, Freeform, WebSearch, Namespace}`
- `codex-rs/core/src/tools/handlers/apply_patch_spec.rs` — the grammar tool

---

## Next

`apply_patch` has been a stub for two chapters — right now it just prints "would patch: ...".

[s05](../s05_apply_patch/) implements it, and answers one question: **why does Codex use a custom
patch format at all, instead of just letting the model rewrite the whole file?**
