#!/usr/bin/env python3
"""
s05: apply_patch

Codex edits files with one freeform tool whose payload is a patch:

    *** Begin Patch
    *** Update File: src/app.py
    @@ def handler():
    -    return None
    +    return build_response()
    *** End Patch

Why not "write the whole file"? Because a whole-file write costs tokens
proportional to the file, silently discards anything the model did not
reproduce, and gives the harness no way to tell an intended change from a
hallucinated one. A patch is a *claim about the current contents*. If the
context lines are not there, the patch fails loudly instead of overwriting.

The grammar is enforced twice: once by the model (the tool ships a Lark
grammar, so the decoder can only emit well-formed patches) and once here, by
the parser. This file is the second half -- parse, locate, apply, and report a
unified diff.

Locating a chunk is deliberately forgiving, in three descending steps:
exact match, then ignoring trailing whitespace, then ignoring leading and
trailing whitespace. Anything looser would risk patching the wrong place.

Run:
  python s05_apply_patch/code.py --demo         # build a sandbox and patch it
  python s05_apply_patch/code.py --apply DIR < patch.txt

Real source: codex-rs/apply-patch/src/parser.rs, seek_sequence.rs, file_update.rs,
codex-rs/core/src/tools/handlers/apply_patch.lark (the grammar the model sees)
"""

from __future__ import annotations

import difflib
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"
ADD_FILE = "*** Add File: "
DELETE_FILE = "*** Delete File: "
UPDATE_FILE = "*** Update File: "
MOVE_TO = "*** Move to: "
END_OF_FILE = "*** End of File"
CHANGE_CONTEXT = "@@"


class PatchError(Exception):
    """Every failure here is reported to the model, never raised at the user."""


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------


@dataclass
class UpdateChunk:
    change_context: str | None = None
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    is_end_of_file: bool = False


@dataclass
class AddFile:
    path: str
    contents: str


@dataclass
class DeleteFile:
    path: str


@dataclass
class UpdateFile:
    path: str
    move_path: str | None
    chunks: list[UpdateChunk]


Hunk = AddFile | DeleteFile | UpdateFile


def parse_patch(text: str) -> list[Hunk]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != BEGIN_PATCH:
        raise PatchError(f"patch must start with '{BEGIN_PATCH}'")

    hunks: list[Hunk] = []
    i = 1
    saw_end = False

    while i < len(lines):
        line = lines[i]
        if line.strip() == END_PATCH:
            saw_end = True
            break

        if line.startswith(ADD_FILE):
            path = line[len(ADD_FILE) :].strip()
            i += 1
            body: list[str] = []
            while i < len(lines) and lines[i].startswith("+"):
                body.append(lines[i][1:])
                i += 1
            hunks.append(AddFile(path, "\n".join(body) + ("\n" if body else "")))
            continue

        if line.startswith(DELETE_FILE):
            hunks.append(DeleteFile(line[len(DELETE_FILE) :].strip()))
            i += 1
            continue

        if line.startswith(UPDATE_FILE):
            path = line[len(UPDATE_FILE) :].strip()
            i += 1
            move_path = None
            if i < len(lines) and lines[i].startswith(MOVE_TO):
                move_path = lines[i][len(MOVE_TO) :].strip()
                i += 1
            chunks, i = _parse_update_chunks(lines, i)
            if not chunks:
                raise PatchError(f"update hunk for {path} has no changes")
            hunks.append(UpdateFile(path, move_path, chunks))
            continue

        if line.strip() == "":
            i += 1
            continue

        raise PatchError(f"unexpected line in patch: {line!r}")

    if not saw_end:
        raise PatchError(f"patch must end with '{END_PATCH}'")
    if not hunks:
        raise PatchError("empty patch")
    return hunks


def _parse_update_chunks(lines: list[str], i: int) -> tuple[list[UpdateChunk], int]:
    chunks: list[UpdateChunk] = []
    current = UpdateChunk()

    def flush() -> None:
        if current.old_lines or current.new_lines:
            chunks.append(current)

    while i < len(lines):
        line = lines[i]
        if line.startswith(("*** Add File: ", DELETE_FILE, UPDATE_FILE)) or line.strip() == END_PATCH:
            break

        if line.startswith(CHANGE_CONTEXT):
            # A new chunk starts. `@@ def handler():` narrows where to look.
            flush()
            context = line[len(CHANGE_CONTEXT) :].strip()
            current = UpdateChunk(change_context=context or None)
            i += 1
            continue

        if line.strip() == END_OF_FILE:
            current.is_end_of_file = True
            i += 1
            continue

        if line.startswith("+"):
            current.new_lines.append(line[1:])
        elif line.startswith("-"):
            current.old_lines.append(line[1:])
        elif line.startswith(" "):
            current.old_lines.append(line[1:])
            current.new_lines.append(line[1:])
        elif line == "":
            # A bare empty line is a context line whose single space was
            # trimmed in transit. Models do this constantly.
            current.old_lines.append("")
            current.new_lines.append("")
        else:
            raise PatchError(f"unexpected line in update hunk: {line!r}")
        i += 1

    flush()
    return chunks, i


# --------------------------------------------------------------------------
# Locate
# --------------------------------------------------------------------------


def seek_sequence(
    lines: list[str], pattern: list[str], start: int = 0, eof: bool = False
) -> int | None:
    """Find `pattern` in `lines` at or after `start`, loosening as needed."""
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None

    search_start = len(lines) - len(pattern) if eof else start
    search_start = max(0, search_start)
    last = len(lines) - len(pattern)

    for normalize in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
        for i in range(search_start, last + 1):
            if all(normalize(lines[i + k]) == normalize(pattern[k]) for k in range(len(pattern))):
                return i
    if eof:
        # The pattern claimed to be at EOF but is not; look anywhere.
        return seek_sequence(lines, pattern, start, eof=False)
    return None


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


@dataclass
class FileChange:
    kind: str  # add | delete | update
    path: str
    move_path: str | None = None
    old_content: str | None = None
    new_content: str | None = None

    def unified_diff(self) -> str:
        old = (self.old_content or "").splitlines(keepends=True)
        new = (self.new_content or "").splitlines(keepends=True)
        label = self.move_path or self.path
        return "".join(
            difflib.unified_diff(old, new, fromfile=f"a/{self.path}", tofile=f"b/{label}")
        )


def apply_patch(text: str, cwd: str) -> list[FileChange]:
    """Parse, verify every hunk against disk, then write. All or nothing."""
    hunks = parse_patch(text)
    root = Path(cwd)
    changes: list[FileChange] = []

    # Pass 1: compute every result without touching the filesystem, so a
    # failure in hunk 3 does not leave hunks 1 and 2 half-applied.
    for hunk in hunks:
        if isinstance(hunk, AddFile):
            target = root / hunk.path
            if target.exists():
                raise PatchError(f"{hunk.path}: already exists")
            changes.append(FileChange("add", hunk.path, new_content=hunk.contents))

        elif isinstance(hunk, DeleteFile):
            target = root / hunk.path
            if not target.is_file():
                raise PatchError(f"{hunk.path}: no such file")
            changes.append(
                FileChange("delete", hunk.path, old_content=target.read_text())
            )

        else:
            target = root / hunk.path
            if not target.is_file():
                raise PatchError(f"{hunk.path}: no such file")
            old = target.read_text()
            changes.append(
                FileChange(
                    "update",
                    hunk.path,
                    move_path=hunk.move_path,
                    old_content=old,
                    new_content=_apply_chunks(hunk.path, old, hunk.chunks),
                )
            )

    # Pass 2: write.
    for change in changes:
        target = root / change.path
        if change.kind == "add":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_content or "")
        elif change.kind == "delete":
            target.unlink()
        else:
            destination = root / (change.move_path or change.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(change.new_content or "")
            if change.move_path and destination != target:
                target.unlink()

    return changes


def _apply_chunks(path: str, content: str, chunks: list[UpdateChunk]) -> str:
    # A trailing newline is a terminator, not an empty last line. Keeping the
    # artifact would push every end-of-file anchor one line past the end.
    trailing_newline = content.endswith("\n")
    lines = content.split("\n")
    if trailing_newline:
        lines.pop()
    result: list[str] = []
    cursor = 0

    for index, chunk in enumerate(chunks):
        search_from = cursor
        if chunk.change_context:
            found = seek_sequence(lines, [chunk.change_context], cursor)
            if found is None:
                raise PatchError(
                    f"{path}: could not find context {chunk.change_context!r}"
                )
            search_from = found + 1

        at = seek_sequence(lines, chunk.old_lines, search_from, chunk.is_end_of_file)
        if at is None:
            preview = chunk.old_lines[0] if chunk.old_lines else "<empty>"
            raise PatchError(
                f"{path}: chunk {index + 1} does not match the file (looking for {preview!r})"
            )

        result.extend(lines[cursor:at])
        result.extend(chunk.new_lines)
        cursor = at + len(chunk.old_lines)

    result.extend(lines[cursor:])
    return "\n".join(result) + ("\n" if trailing_newline else "")


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------

DEMO_FILE = """\
def handler(request):
    # TODO: implement
    return None


def main():
    print(handler(None))
"""

DEMO_PATCH = """\
*** Begin Patch
*** Update File: app.py
@@ def handler(request):
-    # TODO: implement
-    return None
+    return {"status": 200, "body": request}
*** Add File: README.md
+# demo
+Patched by s05.
*** End Patch
"""


def demo() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="learn-codex-s05-"))
    (workdir / "app.py").write_text(DEMO_FILE)
    print(f"workspace: {workdir}\n")
    print(DEMO_PATCH)
    for change in apply_patch(DEMO_PATCH, str(workdir)):
        print(f"--- {change.kind} {change.path}")
        print(change.unified_diff() or "(new file)")
    print("\nresult:")
    print((workdir / "app.py").read_text())
    return 0


def main(argv: list[str]) -> int:
    if "--demo" in argv or len(argv) == 1:
        return demo()
    if "--apply" in argv:
        cwd = argv[argv.index("--apply") + 1]
        try:
            changes = apply_patch(sys.stdin.read(), cwd)
        except PatchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for change in changes:
            print(f"{change.kind} {change.path}")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
