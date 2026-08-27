# s12: Instructions — base prompt, AGENTS.md, and skills

[English](README.md) · [中文](README.zh.md)

[s11](../s11_compaction/) → `s12` → [s13](../s13_mcp/)

> *"Instructions that always apply are loaded. Instructions that sometimes apply are advertised."*

---

Eleven chapters passed one hard-coded `BASE_INSTRUCTIONS`. A real session assembles four
sources, and the *channel* each one uses is as deliberate as the order:

```
instructions field   the base prompt for this model family
developer message    permissions, environment -- harness facts (s03)
user message         AGENTS.md, concatenated root -> cwd
user message         the skills index: names and one-line descriptions only
```

Harness facts go on the developer channel because they outrank anything in a project file. A
repository's `AGENTS.md` can ask for many things; it cannot grant itself write access outside
the workspace.

## AGENTS.md is discovered, not configured

```
1. walk up from cwd until a project-root marker (.git) is found
2. collect every AGENTS.md from that root down to cwd, inclusive
3. concatenate in that order -- nearest file last, so it wins
4. never walk past the project root
```

```
at the repo root:            inside services/api:
  ~/AGENTS.md                  ~/AGENTS.md
  AGENTS.md                    AGENTS.md
                               services/api/AGENTS.md
```

The layering is the whole design: a monorepo's root file sets house style,
`services/api/AGENTS.md` overrides it for that service, and neither file has to know the other
exists.

Rule 4 is what keeps it sane:

```python
"""Nearest ancestor holding a marker; cwd itself if there is none.

Without this bound, a session started in `/Users/me/code/x` would pick up
an AGENTS.md sitting in `/Users/me` and apply someone's unrelated notes to
every project on the machine."""
```

## Skills use the opposite trick

A skill is a directory with a `SKILL.md` whose YAML frontmatter carries a name and a
description:

```markdown
---
name: "release"
description: "Cut a release: changelog, tag, publish."
---

# Release procedure
... 2000 tokens of procedure ...
```

Only the name and description enter the prompt:

```
skill file on disk: 2600 chars
what enters the prompt: 181 chars

the body is read only if the agent runs `cat` on that path.
```

A hundred skills cost a hundred lines of context, not a hundred documents. The body is fetched
with the shell, when and only when the model decides the skill applies — which is a decision it
can make from one line of description.

```python
if not description:
    return None  # a skill the model cannot judge is worse than no skill
```

That is the same economics as AGENTS.md, inverted. Always-applicable instructions are always
loaded; sometimes-applicable instructions are advertised and fetched.

## In `code.py`

| Piece | Job |
|---|---|
| `find_project_root` | Marker walk, bounded |
| `discover_agents_docs` | User file, then root → cwd |
| `parse_skill` | Frontmatter only; the body is never parsed |
| `build_prompt` | The four sources, on the right channels |

## Run it

```bash
python s12_instructions/code.py --demo          # a fake monorepo, showing the layering
python s12_instructions/code.py --show .        # your real ~/.codex skills and AGENTS.md
```

## Real source

- `codex-rs/core/src/agents_md.rs` — the discovery algorithm, in its doc comment
- `codex-rs/core/src/skills.rs`, `codex-rs/skills/src/parser.rs`
- `codex-rs/core/src/context/` — one file per injected block

## Next

Knowledge can be loaded on demand. [s13](../s13_mcp/) does the same for tools.
