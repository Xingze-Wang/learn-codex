# s04: The tool registry

[English](README.md) · [中文](README.zh.md)

[s03](../s03_turn_context/) → `s04` → [s05](../s05_apply_patch/)

> *"Assemble the tools per turn. Dispatch by name. Never raise."*

---

s01 hard-coded one tool and one `if name == ...`. Three things make that untenable.

**The model varies.** Every model record carries a `shell_type`. A model trained on
`exec_command` never sees a `shell` tool, and vice versa:

```python
if config.shell_type == "shell":
    registry.register(RegisteredTool(SHELL, handle_shell, supports_parallel=False))
else:
    registry.register(RegisteredTool(EXEC_COMMAND, handle_exec_command, supports_parallel=False))
```

**The config varies.** `web_search`, `update_plan`, `apply_patch`, MCP servers (s13) — each is
present only when enabled. `registry.specs()` is literally what goes into the request body.

**The call shape varies.** Not every tool takes JSON.

## Freeform tools

`apply_patch` is a `custom` tool with a Lark grammar attached:

```python
{
    "type": "custom",
    "name": "apply_patch",
    "description": "... This is a FREEFORM tool, so do not wrap the patch in JSON.",
    "format": {"type": "grammar", "syntax": "lark", "definition": APPLY_PATCH_LARK},
}
```

The model emits the patch text directly, and the decoder is constrained by that grammar, so a
syntactically invalid patch is not merely rejected downstream — it cannot be generated. The call
arrives as `custom_tool_call.input` (a string) rather than `function_call.arguments` (JSON), and
`build_tool_call` is the one place that knows the difference:

```python
if item.get("type") == "custom_tool_call":
    return ToolCall(item.get("name", ""), item.get("call_id", ""), item.get("input", ""))
```

Wrapping a 200-line patch in a JSON string field would mean escaping every newline and quote in
it, and one escaping mistake by the model costs a whole turn.

## Failures are messages

```python
def dispatch(registry, call, ctx):
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

There is a subtler version of the same rule in the loop. If a call cannot even be parsed, the
harness still has to answer it:

```python
except ToolError as exc:
    # The model emitted something unparseable. Answer the call
    # anyway -- an unanswered call_id breaks the next request.
    rejected += 1
    self.history.append({"type": "function_call_output", "call_id": ..., "output": f"error: {exc}"})
```

A `function_call` with no matching `function_call_output` is a malformed conversation. The next
request fails, and the failure has nothing to do with the model's actual mistake.

## Output has a budget

```python
def truncate_output(text, max_tokens=MAX_OUTPUT_TOKENS):
    """Keep the head and the tail; the middle is where logs repeat themselves."""
```

Head and tail, marker in the middle. A build log's first lines say what ran and its last lines
say how it failed; the 40,000 lines between them are the same warning repeated. Without this,
one `make` eats the context window that s11 exists to protect.

## Parallelism is per tool

```python
assert reg.supports_parallel("update_plan") is True
assert reg.supports_parallel("exec_command") is False
assert reg.supports_parallel("apply_patch") is False
```

Two patches racing on one file is a merge conflict with extra steps. Two shell commands racing
in one working directory is worse. Read-only tools can overlap; anything that mutates the
workspace is serialized.

The registry here records the flag but the loop still executes calls one at a time — the real
router uses it to batch the calls that may overlap
(`ToolRouter::tool_supports_parallel`). Recording it separately is the part worth copying:
whether a tool is safe to run concurrently is a property of the tool, not of the call site.

## In `code.py`

| Piece | Job |
|---|---|
| `function_tool` / `freeform_tool` | The two spec shapes |
| `ToolRegistry` | name → handler, and `specs()` for the request |
| `ToolsConfig` / `build_registry` | Per-turn assembly |
| `build_tool_call` | `function_call` vs `custom_tool_call` |
| `dispatch` | Name lookup, error containment |
| `truncate_output` | Head/tail token budget |

## Run it

```bash
python s04_tool_registry/code.py --list          # the exact JSON sent as `tools`
python s04_tool_registry/code.py "how many lines of python are here?"
```

## Real source

- `codex-rs/core/src/tools/registry.rs`, `router.rs`
- `codex-rs/tools/src/tool_spec.rs` — `ToolSpec::{Function, Freeform, WebSearch, Namespace}`
- `codex-rs/core/src/tools/handlers/apply_patch_spec.rs` — the grammar tool

## Next

`apply_patch` has been a stub for two chapters. [s05](../s05_apply_patch/) implements it.
