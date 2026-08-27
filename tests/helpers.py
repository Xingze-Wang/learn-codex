"""Shared test doubles.

Every chapter's `code.py` is standalone and its live path talks to the
Responses API. Tests never do: they load the module by path and inject a
scripted client, so the whole harness is exercised offline.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(chapter: str) -> Any:
    """Import `<chapter>/code.py` as a module."""
    path = ROOT / chapter / "code.py"
    name = f"learn_codex_{chapter}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ScriptedClient:
    """Replays a fixed list of turns; records every request it was given."""

    def __init__(self, mod: Any, turns: list[list[Any]]) -> None:
        self.mod = mod
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any):
        self.requests.append(kwargs)
        if not self.turns:
            raise AssertionError("model called more times than the script allows")
        for event in self.turns.pop(0):
            yield event


def say(mod: Any, text: str) -> list[Any]:
    """A turn where the model just answers."""
    return [
        mod.OutputTextDelta(text),
        mod.OutputItemDone(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ),
        mod.Completed(10, 5),
    ]


def call(mod: Any, cmd: str, call_id: str = "call_1", name: str = "exec_command", **extra: Any) -> list[Any]:
    """A turn where the model calls the shell."""
    args = {"cmd": cmd, **extra}
    return [
        mod.OutputItemDone(
            {
                "type": "function_call",
                "name": name,
                "arguments": __import__("json").dumps(args),
                "call_id": call_id,
            }
        ),
        mod.Completed(10, 5),
    ]
