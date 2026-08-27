from __future__ import annotations

import pytest
from helpers import load

mod = load("s05_apply_patch")


def write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_add_update_delete(tmp_path):
    write(tmp_path, "keep.py", "a\nb\nc\n")
    write(tmp_path, "gone.py", "x\n")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.py\n"
        "+print('hi')\n"
        "*** Update File: keep.py\n"
        "@@\n"
        "-b\n"
        "+B\n"
        "*** Delete File: gone.py\n"
        "*** End Patch\n"
    )
    changes = mod.apply_patch(patch, str(tmp_path))

    assert {c.kind for c in changes} == {"add", "update", "delete"}
    assert (tmp_path / "new.py").read_text() == "print('hi')\n"
    assert (tmp_path / "keep.py").read_text() == "a\nB\nc\n"
    assert not (tmp_path / "gone.py").exists()


def test_context_line_narrows_the_match(tmp_path):
    write(tmp_path, "a.py", "def one():\n    return 0\n\ndef two():\n    return 0\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@ def two():\n"
        "-    return 0\n"
        "+    return 2\n"
        "*** End Patch\n"
    )
    mod.apply_patch(patch, str(tmp_path))
    assert (tmp_path / "a.py").read_text() == "def one():\n    return 0\n\ndef two():\n    return 2\n"


def test_mismatched_context_fails_loudly(tmp_path):
    write(tmp_path, "a.py", "actual content\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        "-content the model imagined\n"
        "+replacement\n"
        "*** End Patch\n"
    )
    with pytest.raises(mod.PatchError, match="does not match"):
        mod.apply_patch(patch, str(tmp_path))
    assert (tmp_path / "a.py").read_text() == "actual content\n"


def test_failure_leaves_nothing_half_applied(tmp_path):
    write(tmp_path, "ok.py", "one\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: ok.py\n"
        "@@\n"
        "-one\n"
        "+ONE\n"
        "*** Update File: missing.py\n"
        "@@\n"
        "-x\n"
        "+y\n"
        "*** End Patch\n"
    )
    with pytest.raises(mod.PatchError, match="no such file"):
        mod.apply_patch(patch, str(tmp_path))
    assert (tmp_path / "ok.py").read_text() == "one\n"


def test_move_renames_the_file(tmp_path):
    write(tmp_path, "old.py", "value = 1\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: old.py\n"
        "*** Move to: sub/new.py\n"
        "@@\n"
        "-value = 1\n"
        "+value = 2\n"
        "*** End Patch\n"
    )
    mod.apply_patch(patch, str(tmp_path))
    assert not (tmp_path / "old.py").exists()
    assert (tmp_path / "sub/new.py").read_text() == "value = 2\n"


def test_whitespace_tolerance():
    lines = ["def f():", "    return 1  ", "done"]
    assert mod.seek_sequence(lines, ["    return 1"], 0) == 1
    assert mod.seek_sequence(lines, ["return 1"], 0) == 1
    assert mod.seek_sequence(lines, ["nope"], 0) is None


def test_end_of_file_marker_anchors_at_the_end(tmp_path):
    write(tmp_path, "a.txt", "x\nend\nmiddle\nend\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-end\n"
        "+END\n"
        "*** End of File\n"
        "*** End Patch\n"
    )
    mod.apply_patch(patch, str(tmp_path))
    assert (tmp_path / "a.txt").read_text() == "x\nend\nmiddle\nEND\n"


def test_adding_over_an_existing_file_is_refused(tmp_path):
    write(tmp_path, "a.py", "here\n")
    patch = "*** Begin Patch\n*** Add File: a.py\n+other\n*** End Patch\n"
    with pytest.raises(mod.PatchError, match="already exists"):
        mod.apply_patch(patch, str(tmp_path))


def test_malformed_patches_are_rejected(tmp_path):
    with pytest.raises(mod.PatchError, match="must start with"):
        mod.apply_patch("Update File: a.py\n", str(tmp_path))
    with pytest.raises(mod.PatchError, match="must end with"):
        mod.apply_patch("*** Begin Patch\n*** Delete File: a.py\n", str(tmp_path))


def test_unified_diff_is_produced_for_the_event_stream(tmp_path):
    write(tmp_path, "a.py", "one\ntwo\n")
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-two\n+2\n*** End Patch\n"
    change = mod.apply_patch(patch, str(tmp_path))[0]
    diff = change.unified_diff()
    assert "-two" in diff and "+2" in diff
