# s11: Compaction

[English](README.md) · [中文](README.zh.md)

[s10](../s10_rollout/) → `s11` → [s12](../s12_instructions/)

> *"Compact before the request that would fail, not after."*

---

Every chapter so far grows `history` forever, and a long session ends the same way every time:
one more request, and the context window is full.

Codex watches the token count and compacts *before* the next request:

```
used 82k / 100k  ->  ask the model to summarize its own work
                 ->  rebuild history as: prefix + recent user turns + summary
                 ->  continue the same turn, no user involvement
```

```python
# Checked here, before the request, not after the failure.
if self.token_status().needs_compaction(self.auto_compact_ratio):
    record = self.compact()
```

Reacting to the error instead would mean discovering the limit at the worst moment — mid-task,
with a request that has to be discarded and rebuilt anyway.

## What survives, and why

| | |
|---|---|
| kept | the session prefix (instructions, environment) — cheap, and the agent is lost without it |
| kept | recent user messages, newest first until a token budget runs out |
| kept | one summary item, marked so a later compaction can recognize it |
| dropped | every tool output, every reasoning item, every intermediate step |

The dropped part is where 90% of the tokens are and almost none of the value: a 4000-line
`pytest` log matters only through the sentence *"three tests fail in test_auth"*. The summary is
written by the model that just did the work, so it knows which sentence that is.

`--explain` shows the arithmetic on a synthetic session:

```
history: 18 items, ~3947 tokens
status: 99% of a 4k window -> needs compaction: True

rebuilt: 4 items, ~274 tokens  (6% of before)
  [user] <environment_context>...
  [user] port the auth module to the new session API...
  [user] also keep the old endpoint working...
  [summary] Another language model started to solve this problem...
```

## Newest-first budgeting

```python
for message in reversed(user_messages):
    if remaining <= 0:
        break
    cost = approx_tokens(message)
    if cost <= remaining:
        selected.append(message)
        remaining -= cost
    else:
        selected.append(message[: remaining * CHARS_PER_TOKEN])
        break
```

If only one user message fits, it must be the one the agent is working on right now — not the
first thing ever said. The message that does not quite fit is truncated rather than dropped,
because a partial "also keep the old endpoint working" still carries the constraint.

## The summary request has no tools

```python
def request_summary(client, history) -> str:
    """A separate model call over the same history. No tools: it must summarize,
    not keep working."""
    prompt = [*history, user_item(SUMMARIZATION_PROMPT)]
```

Leave the tools attached and the model, mid-task, does the natural thing: another `pytest` run.

The prompt itself asks for the right things:

```
Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue
```

Decisions, constraints, next steps — not a narrative of what happened. A summary that reads like
a changelog is useless to the model that has to continue the work.

## Marking the summary

```python
SUMMARY_PREFIX = ("Another language model started to solve this problem and produced a summary "
                  "of its thinking process. ...")
```

Two jobs. It tells the model what it is reading — this is a handoff, build on it, do not redo
it. And it lets the *next* compaction recognize a previous summary and not re-collect it as a
user message, which is what would otherwise make summaries of summaries of summaries.

## Compaction is lossy, and says so

Pretending otherwise is how agents silently forget constraints. The summary prompt asks
explicitly for constraints and preferences because those are exactly what a "describe what
happened" summary omits — and they are exactly what the user will notice the agent violating
three turns later.

## In `code.py`

| Piece | Job |
|---|---|
| `TokenStatus` | Used, remaining, threshold |
| `collect_user_messages` | Real user turns; not injected blocks, not old summaries |
| `session_prefix` | The injected blocks worth keeping |
| `build_compacted_history` | prefix + budgeted user turns + summary |
| `request_summary` | The tool-less model call |

## Run it

```bash
python s11_compaction/code.py --explain             # the rebuild, no API call
python s11_compaction/code.py --window 8000 "..."   # force a real auto-compaction
```

## Real source

- `codex-rs/core/src/compact.rs` — `build_compacted_history`
- `codex-rs/prompts/templates/compact/prompt.md`, `summary_prefix.md`
- `codex-rs/core/src/session/context_window.rs`

## Next

The prompt has been one hard-coded string for eleven chapters.
[s12](../s12_instructions/) assembles the real one.
