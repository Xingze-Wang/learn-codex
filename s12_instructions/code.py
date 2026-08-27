#!/usr/bin/env python3
"""
s12: Instructions -- base prompt, AGENTS.md, and skills

Every chapter so far passed one hard-coded `BASE_INSTRUCTIONS` string. A real
session assembles its instructions from four sources, and the order and the
*channel* each one uses are both deliberate:

    instructions field   the base prompt for this model family
    developer message    permissions, environment -- harness facts (s03)
    user message         AGENTS.md, concatenated root -> cwd
    user message         the skills index: names and one-line descriptions only

AGENTS.md is discovered, not configured:

    1. walk up from cwd until a project-root marker (`.git`) is found
    2. collect every AGENTS.md from that root down to cwd, inclusive
    3. concatenate in that order -- nearest file last, so it wins
    4. never walk past the project root

The layering is the whole design: a monorepo's root AGENTS.md sets house
style, `services/api/AGENTS.md` overrides it for that service, and neither
file has to know the other exists.

Skills use the opposite trick. A skill is a directory with a `SKILL.md` whose
YAML frontmatter carries a name and a description. Only those two lines enter
the prompt. The body -- which can be thousands of tokens of procedure -- is
read by the agent with the shell, when and only when it decides the skill
applies. A hundred skills cost a hundred lines of context, not a hundred
documents.

That is the same economics as AGENTS.md in reverse: instructions that always
apply are always loaded; instructions that sometimes apply are advertised and
fetched.

Run:
  python s12_instructions/code.py --show           # assemble for the current directory
  python s12_instructions/code.py --demo           # build a fake repo and show layering

Real source: codex-rs/core/src/agents_md.rs (discovery), codex-rs/core/src/skills.rs,
codex-rs/skills/src/parser.rs, codex-rs/core/src/context/ (the injected blocks)

Builds on the s01 kernel.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")

AGENTS_FILENAME = "AGENTS.md"
AGENTS_OVERRIDE_FILENAME = "AGENTS.override.md"
PROJECT_ROOT_MARKERS = (".git",)
SKILL_FILENAME = "SKILL.md"
MAX_AGENTS_BYTES = 32 * 1024

BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in a terminal harness.

You and the user share one workspace. Read before you write, run commands in
small steps, and prefer the project's own conventions over your defaults.

When a skill in the index below is relevant, read its SKILL.md with the shell
before acting on it. Do not guess at its contents.
"""

# --------------------------------------------------------------------------
# Kernel (s01) -- streaming Responses client, unchanged from chapter to chapter
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputTextDelta:
    delta: str


@dataclass(frozen=True)
class OutputItemDone:
    item: dict[str, Any]


@dataclass(frozen=True)
class Completed:
    input_tokens: int
    output_tokens: int


ResponseEvent = OutputTextDelta | OutputItemDone | Completed


class ModelClient(Protocol):
    def stream(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Iterator[ResponseEvent]: ...


class ResponsesClient:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        from openai import OpenAI

        self._client = OpenAI()
        self.model = model

    def stream(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Iterator[ResponseEvent]:
        with self._client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_items,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            store=False,
            stream=True,
            include=["reasoning.encrypted_content"],
        ) as stream:
            for event in stream:
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    yield OutputTextDelta(event.delta)
                elif kind == "response.output_item.done":
                    raw = event.item.model_dump(exclude_none=True)
                    raw.pop("id", None)
                    raw.pop("status", None)
                    yield OutputItemDone(raw)
                elif kind == "response.completed":
                    usage = getattr(event.response, "usage", None)
                    yield Completed(
                        getattr(usage, "input_tokens", 0) or 0,
                        getattr(usage, "output_tokens", 0) or 0,
                    )


def _message_text(item: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "")
        for part in item.get("content", [])
        if part.get("type") in ("output_text", "text")
    )


def user_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


# --------------------------------------------------------------------------
# AGENTS.md discovery
# --------------------------------------------------------------------------


def find_project_root(cwd: str | Path, markers: tuple[str, ...] = PROJECT_ROOT_MARKERS) -> Path:
    """Nearest ancestor holding a marker; cwd itself if there is none.

    Without this bound, a session started in `/Users/me/code/x` would pick up
    an AGENTS.md sitting in `/Users/me` and apply someone's unrelated notes to
    every project on the machine.
    """
    cwd = Path(cwd).resolve()
    for candidate in (cwd, *cwd.parents):
        if any((candidate / marker).exists() for marker in markers):
            return candidate
    return cwd


@dataclass(frozen=True)
class AgentsDoc:
    path: Path
    content: str


def discover_agents_docs(cwd: str | Path, *, user_home: str | Path | None = None) -> list[AgentsDoc]:
    """User-level file first, then project root down to cwd. Nearest wins."""
    cwd = Path(cwd).resolve()
    docs: list[AgentsDoc] = []

    if user_home:
        personal = Path(user_home) / AGENTS_FILENAME
        if personal.is_file():
            docs.append(AgentsDoc(personal, _read(personal)))

    root = find_project_root(cwd)
    chain = [cwd, *cwd.parents]
    directories = []
    for directory in chain:
        directories.append(directory)
        if directory == root:
            break
    for directory in reversed(directories):  # root first, cwd last
        override = directory / AGENTS_OVERRIDE_FILENAME
        target = override if override.is_file() else directory / AGENTS_FILENAME
        if target.is_file():
            docs.append(AgentsDoc(target, _read(target)))
    return docs


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")[:MAX_AGENTS_BYTES].strip()
    except OSError:
        return ""


def render_agents_docs(docs: list[AgentsDoc]) -> str:
    if not docs:
        return ""
    blocks = [f"<!-- {doc.path} -->\n{doc.content}" for doc in docs if doc.content]
    if not blocks:
        return ""
    return "<user_instructions>\n" + "\n\n".join(blocks) + "\n</user_instructions>"


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    scope: str  # user | project

    def index_line(self) -> str:
        return f"- {self.name}: {self.description}  (read: {self.path})"


def parse_skill(path: Path, scope: str) -> Skill | None:
    """Frontmatter only. The body is never parsed here -- that is the point."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    _, _, rest = text.partition("---\n")
    frontmatter, sep, _body = rest.partition("\n---")
    if not sep:
        return None

    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if line.startswith((" ", "\t")) or ":" not in line:
            continue  # nested keys (metadata:) are not needed for the index
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")

    name = fields.get("name") or path.parent.name
    description = fields.get("description", "")
    if not description:
        return None  # a skill the model cannot judge is worse than no skill
    return Skill(name, description, path, scope)


def discover_skills(*roots: tuple[str | Path, str]) -> list[Skill]:
    skills: list[Skill] = []
    for root, scope in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob(f"*/{SKILL_FILENAME}")):
            skill = parse_skill(skill_md, scope)
            if skill is not None:
                skills.append(skill)
    return skills


def render_skills(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = [skill.index_line() for skill in skills]
    return (
        "<skills>\n"
        "Available skills. Read the SKILL.md file before using one.\n"
        + "\n".join(lines)
        + "\n</skills>"
    )


def default_skill_roots(cwd: str | Path, codex_home: str | Path) -> list[tuple[Path, str]]:
    return [(Path(codex_home) / "skills", "user"), (Path(cwd) / ".codex" / "skills", "project")]


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


@dataclass
class Prompt:
    instructions: str
    items: list[dict[str, Any]] = field(default_factory=list)

    def token_estimate(self) -> int:
        text = self.instructions + "".join(
            part.get("text", "") for item in self.items for part in item.get("content", [])
        )
        return max(1, len(text) // 4)


def developer_item(text: str) -> dict[str, Any]:
    """Harness facts go on the developer channel; they outrank user text."""
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    }


def build_prompt(
    cwd: str | Path,
    *,
    codex_home: str | Path,
    permissions_block: str = "",
) -> Prompt:
    prompt = Prompt(instructions=BASE_INSTRUCTIONS)

    if permissions_block:
        prompt.items.append(developer_item(permissions_block))

    agents = render_agents_docs(discover_agents_docs(cwd, user_home=codex_home))
    if agents:
        prompt.items.append(user_item(agents))

    skills = render_skills(discover_skills(*default_skill_roots(cwd, codex_home)))
    if skills:
        prompt.items.append(user_item(skills))

    return prompt


PERMISSIONS_BLOCK = """\
<permissions>
  <sandbox_mode>workspace-write</sandbox_mode>
  <approval_policy>on-request</approval_policy>
  <network_access>false</network_access>
</permissions>"""


# --------------------------------------------------------------------------
# Demos
# --------------------------------------------------------------------------


def show(cwd: str) -> int:
    codex_home = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
    root = find_project_root(cwd)
    print(f"cwd:          {Path(cwd).resolve()}")
    print(f"project root: {root}")

    docs = discover_agents_docs(cwd, user_home=codex_home)
    print(f"\nAGENTS.md files ({len(docs)}, nearest last):")
    for doc in docs:
        print(f"  {doc.path}  ({len(doc.content)} chars)")

    skills = discover_skills(*default_skill_roots(cwd, codex_home))
    print(f"\nskills ({len(skills)}):")
    for skill in skills[:12]:
        print(f"  [{skill.scope}] {skill.name}: {skill.description[:70]}")
    if len(skills) > 12:
        print(f"  ... and {len(skills) - 12} more")

    prompt = build_prompt(cwd, codex_home=codex_home, permissions_block=PERMISSIONS_BLOCK)
    print(f"\nassembled prompt: {len(prompt.items)} items, ~{prompt.token_estimate()} tokens")
    for item in prompt.items:
        text = item["content"][0]["text"]
        print(f"\n--- {item['role']} ---")
        print(text[:400] + ("..." if len(text) > 400 else ""))
    return 0


def demo() -> int:
    root = Path(tempfile.mkdtemp(prefix="learn-codex-s12-")).resolve()
    (root / ".git").mkdir()
    (root / AGENTS_FILENAME).write_text(
        "# Monorepo rules\n- Python 3.12, type hints everywhere.\n- Never commit to main.\n"
    )
    service = root / "services" / "api"
    service.mkdir(parents=True)
    (service / AGENTS_FILENAME).write_text(
        "# services/api\n- This service is Go, not Python. Ignore the root's Python rule.\n"
    )
    home = root / "fake-codex-home"
    (home / "skills" / "release").mkdir(parents=True)
    (home / "skills" / "release" / SKILL_FILENAME).write_text(
        '---\nname: "release"\ndescription: "Cut a release: changelog, tag, publish."\n---\n\n'
        + "# Release procedure\n" + "step\n" * 500
    )
    (home / AGENTS_FILENAME).write_text("# personal\n- Prefer short commit messages.\n")

    print(f"repo: {root}\n")
    for label, cwd in (("at the repo root", root), ("inside services/api", service)):
        docs = discover_agents_docs(cwd, user_home=home)
        print(f"{label}:")
        for doc in docs:
            print(f"  {doc.path.relative_to(root)}")
        print()

    skill_file = home / "skills" / "release" / SKILL_FILENAME
    skill = parse_skill(skill_file, "user")
    print(f"skill file on disk: {len(skill_file.read_text())} chars")
    print(f"what enters the prompt: {len(skill.index_line())} chars")
    print(f"  {skill.index_line()[:100]}")
    print("\nthe body is read only if the agent runs `cat` on that path.")
    return 0


def main(argv: list[str]) -> int:
    if "--show" in argv:
        index = argv.index("--show")
        cwd = argv[index + 1] if len(argv) > index + 1 else os.getcwd()
        return show(cwd)
    return demo()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
