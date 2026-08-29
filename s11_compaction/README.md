# s11: Compaction — before the request that would fail

[English](README.md) · [中文](README.zh.md)

[s10](../s10_rollout/) → `s11` → [s12](../s12_instructions/) → ... → [s15](../s15_harness/)

> *"Compact before the request that would fail, not after."*
>
> **Harness layer**: context management — how a finite window serves a long task.

---

## The problem

Every chapter so far grows `history` forever. A two-hour session holds:

- dozens of `pytest` outputs, thousands of lines each
- hundreds of file reads, in full
- the model's reasoning from every turn

Then one request hits the model's context limit, the API errors, and **the whole thing stops** —
halfway through the task.

---

## First: what a context window is

Think of it as **a fixed-size sheet of scratch paper** the model works on.

Every request rewrites the entire `history` onto that sheet ([s01](../s01_agent_loop/)'s
`store: false` guarantees this). The model reads the whole sheet before deciding anything.

The sheet has a fixed size (say 272k tokens). Go over and the API refuses the request.

And in coding tasks, **what fills it is almost always tool output**:

- reading one long file puts the whole file on the sheet;
- one test or build run adds tens of KB;
- searching across files keeps appending.

The longer the task runs, the fuller the sheet.

---

## The solution

Check **before the next request**, and compact if you are over:

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

**Why not wait for the error?** Because by then you are mid-task, the request has to be thrown
away and rebuilt anyway — and half the context may be results that just came back and have never
been read. Compacting **at a boundary** is far more controllable than crashing at an
**arbitrary point**.

The threshold is a fraction of the window:

```python
AUTO_COMPACT_RATIO = 0.80

def needs_compaction(self, ratio: float = AUTO_COMPACT_RATIO) -> bool:
    return self.used >= self.window * ratio
```

The 20% left over is not waste — it is **headroom for the compaction itself**, which is also a
request.

---

## How it works

Compaction is: have the model summarize itself, then rebuild history. Four steps.

**Step 1**: decide what survives.

| | |
|---|---|
| **keep** | the session prefix (instructions, environment) — cheap, and the agent is lost without it |
| **keep** | recent user messages, newest first until a token budget runs out |
| **keep** | one summary, marked |
| **drop** | every tool output, every reasoning item, every intermediate step |

The dropped part is **90% of the tokens and almost none of the value**: a 4000-line `pytest` log
matters only through the sentence *"three tests fail in test_auth"*.

And the summary is written by **the model that just did the work** — so it knows which sentence
that is.

**Step 2**: have the model summarize itself. Note this call carries **no tools**.

```python
def request_summary(client, history) -> str:
    """A separate model call over the same history. No tools: it must summarize,
    not keep working."""
    prompt = [*history, user_item(SUMMARIZATION_PROMPT)]
```

What happens if you leave the tools attached? A model that is mid-task does the natural thing:
**runs `pytest` again.**

The prompt asks for specific things (this is Codex's own text):

```
Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue
```

**Decisions, constraints, next steps — not a narrative of what happened.** A summary that reads
like a changelog is useless to the model that has to continue.

**Step 3**: budget newest-first.

```python
for message in reversed(user_messages):          # start from the newest
    if remaining <= 0:
        break
    cost = approx_tokens(message)
    if cost <= remaining:
        selected.append(message)
        remaining -= cost
    else:
        selected.append(message[: remaining * CHARS_PER_TOKEN])   # truncate what does not fit
        break
selected.reverse()
```

**Why newest-first?** If only one user message fits, it must be the one the agent is working on
**right now**, not the first thing ever said.

The one that does not quite fit is **truncated rather than dropped** — half of "also keep the old
endpoint working" still carries the constraint.

**Step 4**: mark the summary.

```python
SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary "
    "of its thinking process. ... use the information in this summary to assist "
    "with your own analysis:"
)
```

That prefix does **two** jobs:

1. It tells the model what it is reading — **this is a handoff, build on it, do not redo it.**
2. It lets the **next** compaction recognize an existing summary:

```python
def is_summary_item(item) -> bool:
    return (item.get("type") == "message"
            and item.get("role") == "user"
            and _text_of(item).startswith(SUMMARY_PREFIX))
```

Without job 2, the next compaction re-collects the previous summary as "something the user
said", and you get **summaries of summaries of summaries**, each blurrier than the last.

---

## Compaction is lossy, and says so

Pretending otherwise is how agents **silently forget constraints**.

The summary prompt asks explicitly for constraints and preferences because those are exactly what
a "describe what happened" summary omits — and exactly what the user notices the agent violating
three turns later.

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `TokenStatus` | Used, remaining, threshold |
| `collect_user_messages` | Real user turns; not injected blocks, not old summaries |
| `session_prefix` | The injected blocks worth keeping |
| `build_compacted_history` | prefix + budgeted user turns + summary |
| `request_summary` | The tool-less model call |

---

## Try it

First, the rebuild with **no API call**:

```bash
python s11_compaction/code.py --explain
```

```
history: 18 items, ~3947 tokens
status: 99% of a 4k window -> needs compaction: True

rebuilt: 4 items, ~274 tokens  (6% of before)
  [user] <environment_context>...
  [user] port the auth module to the new session API...
  [user] also keep the old endpoint working...
  [summary] Another language model started to solve this problem...
```

**What to watch**: 18 items become 4, tokens drop to 6%. And **both user messages survive** —
everything dropped was reasoning and tool output.

To see a real auto-compaction, shrink the window until it has to happen:

```bash
python s11_compaction/code.py --window 8000 "count the lines in every python file in this repo"
```

Partway through you will see `[auto-compacted: 8200 -> 900 tokens]`, and then it **keeps
working** — with nothing required from you.

---

## Real source

- `codex-rs/core/src/compact.rs` — `build_compacted_history`
- `codex-rs/prompts/templates/compact/prompt.md`, `summary_prefix.md`
- `codex-rs/core/src/session/context_window.rs`

---

## Next

Eleven chapters in, `BASE_INSTRUCTIONS` has been one hard-coded string.

In a real session the prompt is assembled from four sources — and **which channel** each one uses
is as deliberate as the order. More importantly: how does a repository tell the agent what the
rules are here?

[s12](../s12_instructions/) is how `AGENTS.md` gets **discovered** (not configured), and the
inverted trick that skills use.
