# s03: TurnContext and world state

[English](README.md) · [中文](README.zh.md)

[s02](../s02_protocol/) → `s03` → [s04](../s04_tool_registry/)

> *"Freeze the settings when the turn starts. Tell the model only what changed."*

---

A session outlives a turn, but almost nothing a tool needs is session-wide. The cwd, the model,
the approval policy, the sandbox policy — each is decided when a turn begins, and none of them
may change under a running tool.

```python
@dataclass(frozen=True)
class TurnContext:
    cwd: str
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    approval_policy: str = "on-request"
    sandbox_mode: str = "workspace-write"
    network_access: bool = False
    shell: str = ...
```

Frozen, and passed explicitly to every tool:

```python
# The tool reads the turn's cwd, not the process's.
cwd = args.get("workdir") or ctx.cwd
```

The alternative — tools reading `os.getcwd()` or a mutable `self.cwd` — breaks the moment the
user changes a setting while a turn is in flight (which s02 made possible). A tool that started
under `read-only` must finish under `read-only`, even if the user switched to `workspace-write`
two seconds ago.

## Telling the model where it is

The model cannot see `TurnContext`. It has to be told, and Codex tells it with an ordinary
message in the conversation:

```xml
<environment_context>
  <cwd>/Users/you/repo</cwd>
  <shell>zsh</shell>
  <current_date>2026-05-23</current_date>
  <network enabled="false" />
</environment_context>
```

The naive version injects that on every turn. Codex keeps a **world state**: each section is
re-rendered every turn, compared against what was last sent, and injected only when the
rendering changed.

```python
def updates(self, ctx: TurnContext) -> list[str]:
    changed = []
    for name, render in SECTIONS.items():
        text = render(ctx)
        if self.last.get(name) != text:
            self.last[name] = text
            changed.append(text)
    return changed
```

```
turn 1  cwd=/repo  read-only   -> inject <environment_context> + <permissions>
turn 2  cwd=/repo  read-only   -> inject nothing
turn 3  cwd=/other read-only   -> inject <environment_context> only
```

Diffing rather than repeating matters for two reasons. Tokens, obviously. But also: a block that
appears in the conversation is a signal. When `<environment_context>` shows up at turn 7, its
presence *means* something changed — and the model treats it accordingly, instead of learning to
skip a paragraph that has been identical for thirty turns.

## Permissions are context too

The same mechanism carries the policy:

```xml
<permissions>
  <sandbox_mode>workspace-write</sandbox_mode>
  The sandbox permits reading files and editing files under cwd...
  <approval_policy>on-request</approval_policy>
  You may request escalated permissions when a command needs to run outside the sandbox...
</permissions>
```

The enforcement is in s07 and s08 — the kernel blocks the write whatever the model believes.
This block exists so the model does not waste a turn discovering the boundary the hard way, and
so it knows escalation is available at all.

## In `code.py`

| Piece | Job |
|---|---|
| `TurnContext` | Frozen per-turn settings |
| `render_environment` / `render_permissions` | One section each |
| `WorldState.updates` | Diff against what was last sent |
| `Session.run_turn` | Resolve the context once, inject changes, then loop |

## Run it

```bash
python s03_turn_context/code.py --render          # print the sections, no API call
python s03_turn_context/code.py "what is in this directory?"
python s03_turn_context/code.py --cwd /tmp "and here?"
```

## Real source

- `codex-rs/core/src/session/turn_context.rs`
- `codex-rs/core/src/context/world_state/environment.rs` — render + change detection
- `codex-rs/core/src/context/world_state/permissions.rs`

## Next

The context is fixed; the tool list is not. [s04](../s04_tool_registry/) assembles it per turn.
