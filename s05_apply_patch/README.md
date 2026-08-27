# s05: apply_patch

[English](README.md) · [中文](README.zh.md)

[s04](../s04_tool_registry/) → `s05` → [s06](../s06_unified_exec/)

> *"A patch is a claim about the current contents. If the claim is false, refuse."*

---

Codex edits files with one tool whose payload is a patch:

```
*** Begin Patch
*** Update File: src/app.py
@@ def handler():
-    return None
+    return build_response()
*** End Patch
```

Not a whole-file write, and not a line-number diff. Both alternatives fail in the same place —
when the model's idea of the file is out of date:

- **Whole-file write** costs tokens proportional to the file, and silently deletes anything the
  model failed to reproduce. A 900-line file rewritten from memory loses the function nobody
  mentioned.
- **Line-number diff** applies cleanly to the wrong lines the moment the file has shifted by
  one, and there is nothing in the patch to detect that with.

A context patch carries its own verification. `-    return None` says *this line is there right
now*. If it is not, the patch fails and the model gets told, instead of overwriting work.

## The grammar is enforced twice

Once by the model — the tool ships the Lark grammar (s04) and the decoder cannot emit anything
outside it — and once by the parser here. Three hunk types:

```
*** Add File: path        followed by +lines
*** Delete File: path
*** Update File: path     optional *** Move to: path, then chunks
```

A chunk is an optional `@@ context` line, then lines prefixed with `+`, `-`, or a space.

One parser detail that is not decoration:

```python
elif line == "":
    # A bare empty line is a context line whose single space was
    # trimmed in transit. Models do this constantly.
    current.old_lines.append("")
    current.new_lines.append("")
```

## Locating a chunk: forgiving, in three steps

```python
for normalize in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
    for i in range(search_start, last + 1):
        if all(normalize(lines[i + k]) == normalize(pattern[k]) for k in range(len(pattern))):
            return i
```

Exact match, then ignoring trailing whitespace, then ignoring leading and trailing whitespace.
Each step is looser than the last, and the search stops at the first one that hits — so a file
with both an exact and an indentation-shifted match always takes the exact one.

Anything looser (fuzzy matching, similarity scores) would start patching the wrong place, which
is worse than failing: a rejected patch costs a turn, a misapplied one costs a debugging session.

`*** End of File` anchors the search at the end instead of the beginning, for the common case of
appending to a file whose last lines appear earlier too.

## All or nothing

```python
# Pass 1: compute every result without touching the filesystem, so a
# failure in hunk 3 does not leave hunks 1 and 2 half-applied.
```

The whole patch is resolved in memory first — every file read, every chunk located, every result
computed — and only then written. A patch that touches four files and fails on the fourth leaves
the workspace exactly as it was. Half-applied edits are the worst outcome available: the model's
next `git diff` shows a state neither it nor the user asked for.

## The diff goes back out

```python
def unified_diff(self) -> str:
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{self.path}", tofile=f"b/{label}"))
```

The harness knows the before and after, so it can emit a real diff as an event. The TUI renders
it; `--json` ships it; s15 accumulates them into a turn diff. The model never has to run
`git diff` to show its work.

## In `code.py`

| Piece | Job |
|---|---|
| `parse_patch` | Text → `AddFile` / `DeleteFile` / `UpdateFile` hunks |
| `seek_sequence` | Three-step location search |
| `apply_patch` | Verify everything, then write everything |
| `FileChange.unified_diff` | What the event stream carries |

## Run it

```bash
python s05_apply_patch/code.py --demo
python s05_apply_patch/code.py --apply /path/to/workdir < patch.txt
```

## Real source

- `codex-rs/apply-patch/src/parser.rs`, `seek_sequence.rs`, `file_update.rs`
- `codex-rs/core/src/tools/handlers/apply_patch.lark` — the grammar the model is given

## Next

Editing is solved. [s06](../s06_unified_exec/) fixes the other half: a shell that survives the
tool call.
