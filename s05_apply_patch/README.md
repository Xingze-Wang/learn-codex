# s05: apply_patch — a patch is a claim about the current contents

[English](README.md) · [中文](README.zh.md)

[s04](../s04_tool_registry/) → `s05` → [s06](../s06_unified_exec/) → ... → [s15](../s15_harness/)

> *"A patch is a claim about the current contents. If the claim is false, refuse."*
>
> **Harness layer**: writing — the one operation that does not go through the shell.

---

## The problem

The model wants to change three lines in a 900-line file. How does it express that?

There look to be three options. The first two fail in the same place.

**Option one: have the model print the whole file back.**

- You pay tokens for 900 lines to change 3.
- Worse: the model is rewriting **from memory**. The function it did not mention is now gone —
  and **nothing errors**. You find out when the tests run.

**Option two: a line-numbered diff ("replace line 412 with…").**

- If the file shifted by one line since the model read it, the patch applies **cleanly to the
  wrong place**.
- And there is nothing in the patch that would let the program notice.

Both share one root cause: **neither carries what the model believed the file looked like.**

---

## First: a context patch verifies itself

The third option, the one Codex takes:

```
*** Begin Patch
*** Update File: src/app.py
@@ def handler():
-    return None
+    return build_response()
*** End Patch
```

Read what that says:

- `@@ def handler():` — "look near the line `def handler():`" (optional, it narrows the search)
- `-    return None` — "**this line is there right now**; remove it"
- `+    return build_response()` — "put this in its place"

The line starting with `-` is the important one: **it is an assertion.**

If `return None` is not there — someone else edited the file, or the model misremembered — the
**patch fails and the model is told**, instead of overwriting someone's work.

As a bonus, this makes the cost proportional to **the size of the change**, not the size of the
file.

---

## The solution

The grammar has exactly three kinds of hunk:

```
*** Add File: path        followed by +lines = the entire new file
*** Delete File: path     nothing else needed
*** Update File: path     an optional *** Move to: path, then chunks
```

A chunk is an optional `@@ context line`, then lines prefixed `+` (add), `-` (remove), or
` ` (unchanged).

The grammar is enforced **twice**:

1. **On the model side** — the tool ships this Lark grammar ([s04](../s04_tool_registry/)) and
   the decoder cannot emit anything outside it.
2. **Here** — the parser checks again, because you never assume the upstream behaved.

---

## How it works

Three steps: parse, locate, write.

### Step 1: parse

Scan line by line; `*** Xxx File:` starts a new hunk. Inside an update hunk, `@@` starts a new
chunk:

```python
if line.startswith("+"):
    current.new_lines.append(line[1:])            # added
elif line.startswith("-"):
    current.old_lines.append(line[1:])            # removed
elif line.startswith(" "):
    current.old_lines.append(line[1:])            # context: belongs to both sides
    current.new_lines.append(line[1:])
```

A context line goes into both `old_lines` and `new_lines` because it is present before *and*
after.

One detail that is not decoration:

```python
elif line == "":
    # A bare empty line is a context line whose single space was
    # trimmed in transit. Models do this constantly.
    current.old_lines.append("")
    current.new_lines.append("")
```

An empty context line should be `" "` — one space. But that space gets stripped by all kinds of
things along the way. Without this branch, any patch spanning a blank line fails.

### Step 2: locate — forgiving, in three descending steps

Take `old_lines` and find where they are:

```python
for normalize in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
    for i in range(search_start, last + 1):
        if all(normalize(lines[i + k]) == normalize(pattern[k]) for k in range(len(pattern))):
            return i
```

Three passes, each looser than the last:

1. **exact match**
2. **ignoring trailing whitespace** (an editor stripped it)
3. **ignoring leading and trailing whitespace** (the indentation changed)

**First hit wins**, so a file containing both an exact match and an indentation-shifted one
always takes the exact one.

**Why not looser still?** Fuzzy matching and similarity scores start patching **the wrong
place**. A rejected patch costs one turn; a misapplied one costs a debugging session — and you
may not even know to start one.

`*** End of File` anchors the search at the end:

```python
search_start = len(lines) - len(pattern) if eof else start
```

for the common case of appending to a file whose last lines also appear earlier.

### Step 3: write — all or nothing

```python
# Pass 1: compute every result without touching the filesystem, so a
# failure in hunk 3 does not leave hunks 1 and 2 half-applied.
for hunk in hunks:
    ...
    changes.append(FileChange("update", hunk.path, old_content=old,
                              new_content=_apply_chunks(hunk.path, old, hunk.chunks)))

# Pass 2: write.
for change in changes:
    ...
```

The whole patch resolves in memory first — every file read, every chunk located, every result
computed — **and only then is anything written**.

Why care so much? **Half-applied edits are the worst available outcome.** The model's next
`git diff` shows a state neither it nor the user asked for, and it will reason forward from
that state.

---

## The diff goes back out

The harness has both the before and the after, so it can produce a real diff:

```python
def unified_diff(self) -> str:
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{self.path}", tofile=f"b/{label}"))
```

The terminal UI renders it, `--json` ships it, [s15](../s15_harness/) accumulates them across a
turn. **The model never has to run `git diff` to show its work.**

---

## What is in `code.py`

| Piece | Job |
|---|---|
| `parse_patch` | Text → `AddFile` / `DeleteFile` / `UpdateFile` |
| `seek_sequence` | The three-step location search |
| `apply_patch` | Verify everything, then write everything |
| `FileChange.unified_diff` | What the event stream carries |

---

## Try it

This chapter needs **no API key** — it is pure logic:

```bash
python s05_apply_patch/code.py --demo
```

It builds a temp directory with an `app.py` and applies a patch, printing the patch, the
generated diff, and the resulting file.

**What to watch**: make it fail on purpose. Copy the demo's patch, change
`-    # TODO: implement` to a line that is **not** in the file, then:

```bash
python s05_apply_patch/code.py --apply /tmp/somewhere < your-patch.txt
```

You get `chunk 1 does not match the file (looking for ...)`, and **the file is byte-for-byte
unchanged**. That is the entire point of the chapter.

---

## Real source

- `codex-rs/apply-patch/src/parser.rs`, `seek_sequence.rs`, `file_update.rs`
- `codex-rs/core/src/tools/handlers/apply_patch.lark` — the grammar the model is given

---

## Next

Editing is solved. But running commands is still at s01's level: `/bin/bash -lc CMD`, wait for
exit.

That breaks on all of these: `cd build && make` (the cd is lost), `python3` (never exits),
`npm run dev` (must keep running), `ssh host` (waits for a password).

[s06](../s06_unified_exec/) is a shell that **outlives the tool call**.
