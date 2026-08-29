# s03: TurnContext and world state — freeze the settings, report only the changes

[English](README.md) · [中文](README.zh.md)

[s02](../s02_protocol/) → `s03` → [s04](../s04_tool_registry/) → ... → [s15](../s15_harness/)

> *"Freeze the settings when the turn starts. Tell the model only what changed."*
>
> **Harness layer**: context injection — how the model learns where it is.

---

## The problem

The model cannot see your machine. It does not know which directory it is in, whether the shell
is zsh or bash, what today's date is, whether it has network, or whether it may write files.

If you do not tell it, it has to **find out by trying**: run a command, hit
`Operation not permitted`, guess why, try another. A whole turn spent feeling for the walls.

So you think: fine, put that information in every turn. And immediately hit the second problem —

**The same `<environment_context>` pasted thirty turns in a row teaches the model to skip it.**
It becomes background noise. And it costs tokens every single turn.

There is a third problem: a user can change a setting while the agent is halfway through working
(a good agent should allow that — see [s02](../s02_protocol/)). A tool that started under "read-only" and finds itself under
"workspace-write" halfway through — which one does it obey?

---

## what a "turn" is

A turn starts when the user says something and ends when the model stops asking for tools. In
between it may have run ten commands and made five model requests — all one turn.

A session contains many turns.

The distinction matters because: **almost nothing a tool needs is session-wide.**

- Which directory does this run in? — a property of this turn.
- Which model, may it write files? — properties of this turn.

So Codex packs them into a `TurnContext`, decided when the turn **starts** and forbidden to
change **mid-turn**.

---

## The solution

Two things, one for each problem above.

**One — freeze it**: snapshot the settings when the turn starts; tools read the snapshot, not a
global.

```python
@dataclass(frozen=True)      # frozen=True: once built, it cannot be modified
class TurnContext:
    cwd: str
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    approval_policy: str = "on-request"
    sandbox_mode: str = "workspace-write"
    network_access: bool = False
    shell: str = ...
```

**Two — report only changes**: split what the model needs into sections, re-render them each
turn, and **inject only the ones whose rendering changed.**

```
turn 1  cwd=/repo  read-only   -> inject <environment_context> + <permissions>
turn 2  cwd=/repo  read-only   -> inject nothing
turn 3  cwd=/other read-only   -> inject <environment_context> only
```

---

## How it works

**Step 1**: resolve this turn's settings once, and keep them for later turns.

```python
def run_turn(self, text: str, *, echo: bool = True, **overrides: Any) -> str:
    # The turn's settings are resolved once, here. Nothing below this line
    # may change them -- a mid-turn `cd` does not move the turn's cwd.
    ctx = self.defaults.with_overrides(**overrides)
    self.defaults = ctx           # thread settings persist to later turns
    self.turns.append(ctx)
```

`frozen=True` makes "cannot change" a guarantee from the language rather than a comment. Want a
different one? Build a new one:

```python
def with_overrides(self, **kwargs: Any) -> TurnContext:
    return replace(self, **{k: v for k, v in kwargs.items() if v is not None})
```

**Step 2**: render the settings into text the model can read.

```python
def render_environment(ctx: TurnContext, *, today: str | None = None) -> str:
    date = today or dt.date.today().isoformat()
    return (
        "<environment_context>\n"
        f"  <cwd>{ctx.cwd}</cwd>\n"
        f"  <shell>{ctx.shell}</shell>\n"
        f"  <current_date>{date}</current_date>\n"
        f'  <network enabled="{str(ctx.network_access).lower()}" />\n'
        "</environment_context>"
    )
```

`--render` prints the real thing:

```xml
<environment_context>
  <cwd>/Users/you/learn-codex</cwd>
  <shell>zsh</shell>
  <current_date>2026-08-29</current_date>
  <network enabled="false" />
</environment_context>
```

**Step 3**: compare against what was last sent, and take only what changed.

```python
class WorldState:
    """Remembers what the model was last told, so it is only told again on change."""

    def __init__(self) -> None:
        self.last: dict[str, str] = {}

    def updates(self, ctx: TurnContext) -> list[str]:
        changed = []
        for name, render in SECTIONS.items():
            text = render(ctx)
            if self.last.get(name) != text:      # compare the string, not the fields
                self.last[name] = text
                changed.append(text)
        return changed
```

Comparing the rendered string rather than the fields is deliberate: **what matters is whether
what the model sees changed**, not whether some internal state did.

**Step 4**: put the changes into history as ordinary user messages, ahead of what the user
actually said.

```python
for section in self.world.updates(ctx):
    self.history.append(user_item(section))
self.history.append(user_item(text))
```

They are ordinary messages — no special channel, no special authority.

**Step 5**: tools read this turn's `ctx`, not process state.

```python
cmd = args.get("cmd", "")
# The tool reads the turn's cwd, not the process's.
cwd = args.get("workdir") or ctx.cwd
```

Write `os.getcwd()` or a mutable `self.cwd` here and the tool's behavior changes the moment the
user changes a setting mid-turn — without the tool knowing.

---

## Why "only changes" is about more than tokens

Saving tokens is the obvious win. The other one matters more:

**A block appearing in the conversation is itself a signal.**

When `<environment_context>` shows up at turn 7, its presence *means* something changed, and the
model reads it. If it were identical for thirty turns, what the model would learn is to **skip
that paragraph**.

---

## Permissions are context too

The same mechanism carries the policy:

```xml
<permissions>
  <sandbox_mode>workspace-write</sandbox_mode>
  The sandbox permits reading files and editing files under cwd. Editing files elsewhere requires approval.
  <approval_policy>on-request</approval_policy>
  You may request escalated permissions when a command needs to run outside the sandbox.
  <network_access>false</network_access>
</permissions>
```

**This text does not enforce anything.** What actually blocks a write is the kernel
([s07](../s07_sandbox/)) and the approval flow ([s08](../s08_approval/)), regardless of what the
model believes.

So why does it exist? Two reasons:

1. So the model does not burn a whole turn discovering the boundary the hard way.
2. So the model knows escalation is even an option — otherwise it hits a wall and gives up.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `TurnContext` | Frozen per-turn settings |
| `render_environment` / `render_permissions` | One section each |
| `WorldState.updates` | Diff against what was last sent |
| `Session.run_turn` | Resolve the context once, inject changes, then loop |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| Settings | globals a tool reads whenever | a frozen `TurnContext` passed in |
| Changing settings mid-turn | the running tool silently changes behavior | the turn keeps the settings it started with |
| What the model knows about its environment | nothing; it finds out by failing | an `<environment_context>` block |
| How often that block is sent | n/a | only when its rendering changes |

---

## Try it

See what would be injected, with no API call:

```bash
python s03_turn_context/code.py --render
```

Then run it for real:

```bash
python s03_turn_context/code.py "what is in this directory?"
python s03_turn_context/code.py --cwd /tmp "and here?"
```

**What to watch**: in interactive mode, ask two questions about the current directory in a row.
The first injects two blocks; the second injects nothing, because nothing changed. That is
exactly what the test asserts:

```python
assert injected_after_two == 2      # two on turn one, zero on turn two
assert injected_after_three == 3    # turn three moved cwd, so one more
```

---

## Real source

- `codex-rs/core/src/session/turn_context.rs`
- `codex-rs/core/src/context/world_state/environment.rs` — render and change detection
- `codex-rs/core/src/context/world_state/permissions.rs`

---

## Next

The context is settled. But the tool list is still one hard-coded `exec_command`.

Real Codex assembles the list per turn: does this model speak `exec_command` or `shell`? Is
`apply_patch` enabled? How many MCP servers are connected?

[s04](../s04_tool_registry/) is how that list gets built — and **why a failing tool must never
raise.**
