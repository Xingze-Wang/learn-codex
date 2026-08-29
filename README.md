# learn-codex

**Read [openai/codex](https://github.com/openai/codex) by rebuilding it: fifteen mechanisms, fifteen runnable files.**

[English](README.md) · [中文](README-zh.md)

> **Never written code?** Start with **[the primer](PRIMER.md)** — twenty minutes, and
> every code block here becomes readable. It assumes nothing.

---

The agent loop in Codex is about thirty lines. Send the conversation, run whatever
`function_call` comes back, send it again, stop when nothing comes back. You can write it in an
afternoon, and this repo does, in [s01](s01_agent_loop/).

Everything else — the other 99% of `codex-rs` — answers three questions the loop does not:

```
What is this thing allowed to do?      sandbox, approval, exec policy, hooks
What does it know, and what did it
  forget?                              instructions, AGENTS.md, skills, compaction, rollout
Who is watching, and how do they
  interrupt?                           the submission/event protocol, and four frontends on it
```

That is what a harness *is*. Codex is a good specimen because its answers are unusually legible:
a real OS sandbox instead of a blocklist, an append-only log instead of hidden state, one
protocol seam instead of a UI wired into the loop.

Each chapter here takes one of those mechanisms, explains why it exists and what breaks without
it, and implements it in a single standalone Python file you can run. Every chapter cites the
Rust files it came from.

---

## The chapters

| | Chapter | Maxim |
|---|---|---|
| [s01](s01_agent_loop/) | Agent Loop | *"One loop, one shell."* |
| [s02](s02_protocol/) | Submission / Event protocol | *"The caller does not call the agent. It submits an Op and reads Events."* |
| [s03](s03_turn_context/) | TurnContext and world state | *"Freeze the settings when the turn starts. Tell the model only what changed."* |
| [s04](s04_tool_registry/) | The tool registry | *"Assemble per turn. Dispatch by name. Never raise."* |
| [s05](s05_apply_patch/) | apply_patch | *"A patch is a claim about the current contents. If the claim is false, refuse."* |
| [s06](s06_unified_exec/) | Unified exec | *"Return early with a session id, not late with a timeout."* |
| [s07](s07_sandbox/) | The sandbox | *"Ask the kernel, not the command string."* |
| [s08](s08_approval/) | Approval and escalation | *"Run it in the sandbox first. Ask only when the sandbox says no."* |
| [s09](s09_exec_policy/) | Exec policy | *"Approval fatigue is a security failure."* |
| [s10](s10_rollout/) | Rollout | *"Append, never rewrite. Resume and fork fall out for free."* |
| [s11](s11_compaction/) | Compaction | *"Compact before the request that would fail, not after."* |
| [s12](s12_instructions/) | Instructions, AGENTS.md, skills | *"Always-applicable instructions are loaded. Sometimes-applicable ones are advertised."* |
| [s13](s13_mcp/) | MCP | *"A coding agent has no business shipping a Jira client."* |
| [s14](s14_hooks/) | Hooks | *"Everything a hook returns is advisory, except `deny`."* |
| [s15](s15_harness/) | The harness | *"A harness is what you get when the mechanisms compose."* |

## The loop, for reference

```python
while True:
    calls = []
    for event in client.stream(instructions=..., input_items=list(history), tools=tools):
        if isinstance(event, OutputItemDone):
            history.append(event.item)                      # messages, reasoning, calls alike
            if event.item.get("type") == "function_call":
                calls.append(event.item)

    if not calls:
        return                                              # the turn is over

    for call in calls:
        history.append({"type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": dispatch(call)})
```

Fourteen chapters are built around this and none of them change it.

## Quick start

```bash
git clone <this repo> && cd learn-codex
pip install -r requirements.txt
cp .env.example .env      # then put your OPENAI_API_KEY in it, or just export it
```

Seven chapters need no API key at all — they are pure mechanism, and they run right now
(plus s15's dry run, which wires everything and calls nothing):

```bash
python s05_apply_patch/code.py --demo        # parse and apply a patch
python s06_unified_exec/code.py --demo       # a live PTY session you can talk to
python s07_sandbox/code.py --demo            # real seatbelt enforcement (macOS)
python s09_exec_policy/code.py               # the rule engine on sample commands
python s10_rollout/code.py --demo            # record, list, resume, fork
python s13_mcp/code.py --demo                # a real MCP client and server
python s14_hooks/code.py --demo              # hooks firing across a turn
python s15_harness/code.py --dry-run         # the whole harness, wired, calling nothing
```

Three of them read your actual Codex installation:

```bash
python s10_rollout/code.py --list ~/.codex   # your real sessions
python s12_instructions/code.py --show .     # your real AGENTS.md and skills
python s14_hooks/code.py --show              # your real hooks.json
```

With a key, every chapter runs live:

```bash
export OPENAI_API_KEY=...
python s01_agent_loop/code.py "count the python files under ."
python s15_harness/code.py "what does this repo do?"
```

## Tests

```bash
python -m pytest tests -q
```

143 tests, no API key, no network. The tests are the second half of the documentation: each one
names a specific thing that would break. `tests/test_s07_sandbox.py` runs the real sandbox;
`tests/test_s13_mcp.py` starts real MCP servers.

## Where Codex and Claude Code actually differ

Most people arrive here having used one of the two, so it is worth naming the differences the
public write-ups keep landing on. The line that recurs, and that this repo agrees with:
**Codex enforces in the kernel; Claude Code enforces in the harness.**

| | Codex | Claude Code |
|---|---|---|
| Default enforcement | An OS sandbox is on by default (seatbelt / Landlock + seccomp); the user is asked only when the kernel refuses ([s07](s07_sandbox/), [s08](s08_approval/)) | Permission rules evaluated in the harness are the primary layer, with OS sandboxing available underneath |
| Deciding without asking | A Starlark rule file: `prefix_rule(pattern=[...], decision="allow"\|"prompt"\|"forbidden")` ([s09](s09_exec_policy/)) | Allow/deny rules in settings, matched per tool and argument |
| Editing files | One freeform `apply_patch` tool whose grammar is enforced during decoding ([s05](s05_apply_patch/)) | Typed `Edit` / `Write` tools with old-string/new-string arguments |
| Running commands | `exec_command` opens a PTY session that outlives the call; `write_stdin` continues it ([s06](s06_unified_exec/)) | `Bash`, with background execution and output polling |
| Project instructions | `AGENTS.md`, concatenated project root → cwd ([s12](s12_instructions/)) | `CLAUDE.md`, with imports |
| Session state | Append-only JSONL rollout with resume and fork ([s10](s10_rollout/)) | Transcripts with `/resume` |
| Public surface | One Op/Event core behind four frontends — TUI, `exec --json`, app-server, MCP server ([s02](s02_protocol/), [s15](s15_harness/)) | CLI, Agent SDK, and hooks |

Converging, not diverging: **MCP**, **hooks** (the JSON wire shape is nearly identical —
`hookSpecificOutput.permissionDecision`), **compaction**, **skills**, and a **plan/todo tool**
exist in both.

The Codex column is drawn from `codex-rs`, which is open source and is what every chapter here
cites. The Claude Code column is from its public documentation and observable behavior; it is
not open source, so treat that column as the weaker evidence.

Background reading, for how others frame it:
[Inside the Agent Harness](https://medium.com/jonathans-musings/inside-the-agent-harness-how-codex-and-claude-code-actually-work-63593e26c176) ·
[awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering) ·
[The Anatomy of an Agent Harness](https://www.langchain.com/blog/the-anatomy-of-an-agent-harness) ·
[Top Agent Harnesses](https://aimultiple.com/agent-harness)

## How to read it

Every chapter has the same shape, and assumes you know nothing going in:

```
The problem        a concrete thing that breaks, in plain language
First: <concept>   the one idea you need before the code makes sense
                   (what a PTY is, what an OS sandbox is, what JSON-RPC is...)
The solution       a diagram, and a table of signal -> meaning -> action
How it works       Step 1..N, each a few lines of code and a sentence saying why
Try it             the exact command, and what to watch when you run it
Next               the pain this leaves behind, which is the next chapter
```

Chapters are standalone. Each `code.py` runs on its own and repeats whatever kernel it needs, so
you can open s09 without having read s08.

s15 is the exception: it imports the other chapters, because composition is its subject.

Suggested order if you are not reading straight through:

- **What makes it an agent** — s01, s02, s04
- **What makes it safe** — s07, s08, s09
- **What makes it survive** — s10, s11
- **What makes it extensible** — s12, s13, s14

And if any word here is unfamiliar, the [primer](PRIMER.md) has a glossary of every one of them.

## Project structure

```
learn-codex/
  s01_agent_loop/
    README.md            # English
    README.zh.md         # 中文
    code.py              # standalone, runnable
  ...
  s15_harness/           # imports the others
  tests/                 # 143 tests, offline
```

## Honesty about scope

`codex-rs` is a large production system in Rust; this is ~7,000 lines of Python plus ~1,800 of tests. It is a reading
aid, not a reimplementation. Where a chapter simplifies, it says so. Where a detail is load-bearing —
`realpath` on sandbox roots, `store: false` and encrypted reasoning, unanswered `call_id`s, the
`EIO` that means a PTY child exited — it is kept, because those are the details that decide
whether the mechanism works.

Every chapter ends with the `codex-rs` paths it was derived from. Read those next.

## Credits

The structure of this repo — one mechanism per chapter, one maxim, one runnable file — follows
[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code).

Codex is © OpenAI, Apache-2.0. This repo is an independent study of its public source.
