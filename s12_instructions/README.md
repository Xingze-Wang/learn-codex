# s12: Instructions — always-applicable is loaded, sometimes-applicable is advertised

[English](README.md) · [中文](README.zh.md)

[s11](../s11_compaction/) → `s12` → [s13](../s13_mcp/) → ... → [s15](../s15_harness/)

> *"Instructions that always apply are loaded. Instructions that sometimes apply are advertised."*
>
> **Harness layer**: knowledge — what the agent should know, and when.

---

## The problem

For eleven chapters `BASE_INSTRUCTIONS` has been one hard-coded string. Reality is messier.

**Problem one: how does a repository tell the agent what the rules are here?**

"This project uses pnpm, not npm." "Tests run with `make test`, not `pytest`." "Don't touch
vendor/." You should not have to say these again in every conversation.

And in a monorepo it gets worse: the root says Python, but the service in `services/api/` is Go.
**Which one wins?**

**Problem two: how do you give the agent a 2000-token procedure?**

Say a release process: update the changelog, tag, run the publish script, notify the channel.
Put it in the system prompt? Then it costs 2000 tokens on **every request**, while 99% of
conversations have nothing to do with releases. Leave it out? Then the agent does not know the
process exists.

---

## First: instructions arrive on different channels

What you send the model is not one string. There are at least three channels, with **different
authority**:

| Channel | What goes there | Who may write it |
|---|---|---|
| the `instructions` field | the base prompt for this model family | only Codex itself |
| a `developer` role message | permissions, environment — **harness facts** | only the harness |
| a `user` role message | AGENTS.md, the skills index | project files, the user |

**Why do harness facts go on the developer channel?** Because they outrank anything in a project
file.

A repository's `AGENTS.md` can ask for many things, but it **cannot grant itself** write access
outside the workspace — that is settled by [s07](../s07_sandbox/)'s kernel and by the
`<permissions>` block on the developer channel.

```python
def developer_item(text: str) -> dict[str, Any]:
    """Harness facts go on the developer channel; they outrank user text."""
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    }
```

---

## The solution (problem one): AGENTS.md is discovered, not configured

Four rules:

```
1. walk up from cwd until a project-root marker (.git) is found
2. collect every AGENTS.md from that root down to cwd, inclusive
3. concatenate in that order -- nearest file last, so it wins
4. never walk past the project root
```

The demo shows it:

```
at the repo root:            inside services/api:
  ~/AGENTS.md                  ~/AGENTS.md
  AGENTS.md                    AGENTS.md
                               services/api/AGENTS.md
```

**The layering is the whole design**: a monorepo's root file sets house style,
`services/api/AGENTS.md` overrides it for that service, and **neither file has to know the other
exists**.

Rule 4 is what keeps it sane:

```python
def find_project_root(cwd, markers=PROJECT_ROOT_MARKERS) -> Path:
    """Nearest ancestor holding a marker; cwd itself if there is none.

    Without this bound, a session started in `/Users/me/code/x` would pick up
    an AGENTS.md sitting in `/Users/me` and apply someone's unrelated notes to
    every project on the machine."""
    cwd = Path(cwd).resolve()
    for candidate in (cwd, *cwd.parents):
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    return cwd
```

The collection order is equally plain — **root first, cwd last**, because later overrides
earlier:

```python
for directory in reversed(directories):  # root first, cwd last
    override = directory / AGENTS_OVERRIDE_FILENAME
    target = override if override.is_file() else directory / AGENTS_FILENAME
    if target.is_file():
        docs.append(AgentsDoc(target, _read(target)))
```

(`AGENTS.override.md` is a local override for "I personally do not follow this shared rule", and
is not committed.)

---

## The solution (problem two): skills use the inverted trick

A skill is a directory with a `SKILL.md` whose YAML frontmatter carries a name and description:

```markdown
---
name: "release"
description: "Cut a release: changelog, tag, publish."
---

# Release procedure
... 2000 tokens of procedure ...
```

**Only the name and description enter the prompt.** Not one word of the body.

```
skill file on disk: 2600 chars
what enters the prompt: 181 chars

the body is read only if the agent runs `cat` on that path.
```

And the parser really does read only the frontmatter:

```python
def parse_skill(path: Path, scope: str) -> Skill | None:
    """Frontmatter only. The body is never parsed here -- that is the point."""
    ...
    if not description:
        return None  # a skill the model cannot judge is worse than no skill
```

That last line is worth pausing on: **a skill with no description is dropped entirely.** The
model uses that one line to decide whether the skill is relevant. Without it, the skill is either
never used or used wrongly.

So: **a hundred skills cost a hundred lines of context, not a hundred documents.**

---

## Two problems, one calculation

```
AGENTS.md: always applies      -> always loaded
skills:    sometimes applies   -> advertised, fetched with the shell when needed
```

Two ends of the same trade-off, with one test: **is this relevant on every turn?** If yes, load
it. If no, advertise it.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `find_project_root` | Bounded marker walk |
| `discover_agents_docs` | User file first, then root → cwd |
| `parse_skill` | Frontmatter only |
| `build_prompt` | The four sources, on the right channels |

---

## What changed

|  | Before this chapter | After it |
|---|---|---|
| The prompt | one hard-coded string | four sources on three channels |
| Project rules | you repeat them every conversation | `AGENTS.md`, discovered automatically |
| A monorepo | one set of rules for everything | root → cwd, nearest file wins |
| A 2000-token procedure | in the prompt on every request, or absent | one line advertised, body fetched on demand |

---

## Try it

**No API key needed.** First the layering:

```bash
python s12_instructions/code.py --demo
```

It builds a fake monorepo (Python rules at the root, Go rules in `services/api`), discovers from
both directories, and shows a skill's "size on disk vs size in the prompt".

**What to watch**: the two discovery lists — from `services/api` there is one more file than from
the root, and it is **last** (so it wins).

Then look at your **real** setup:

```bash
python s12_instructions/code.py --show .
```

It reads your `~/.codex/skills/` and the current project's `AGENTS.md`, and prints the assembled
prompt — including which channel each part goes on.

---

## Real source

- `codex-rs/core/src/agents_md.rs` — the discovery algorithm, in its doc comment
- `codex-rs/core/src/skills.rs`, `codex-rs/skills/src/parser.rs`
- `codex-rs/core/src/context/` — one file per injected block

---

## Next

Knowledge can be loaded on demand now. **Tools cannot.**

A team needs their own issue tracker, deploy API, internal search. Codex should not ship those —
a coding agent has no business bundling a Jira client.

[s13](../s13_mcp/) is how external tools arrive, and the three harness problems they bring:
**names collide, a slow server stalls startup, and tools are themselves context.**
