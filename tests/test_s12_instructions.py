from __future__ import annotations

from helpers import load

mod = load("s12_instructions")


def make_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("root rules")
    nested = tmp_path / "services" / "api"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("service rules")
    return nested


def test_project_root_is_the_nearest_marker(tmp_path):
    nested = make_repo(tmp_path)
    assert mod.find_project_root(nested) == tmp_path.resolve()


def test_no_marker_means_cwd_only(tmp_path):
    (tmp_path / "AGENTS.md").write_text("stray")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert mod.find_project_root(deep) == deep.resolve()
    assert mod.discover_agents_docs(deep) == []


def test_docs_are_root_first_nearest_last(tmp_path):
    nested = make_repo(tmp_path)
    contents = [doc.content for doc in mod.discover_agents_docs(nested)]
    assert contents == ["root rules", "service rules"]


def test_discovery_never_walks_past_the_project_root(tmp_path):
    outside = tmp_path / "outer"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("someone else's notes")
    repo = outside / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("mine")
    assert [d.content for d in mod.discover_agents_docs(repo)] == ["mine"]


def test_user_level_agents_md_comes_first(tmp_path):
    nested = make_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("personal")
    contents = [d.content for d in mod.discover_agents_docs(nested, user_home=home)]
    assert contents == ["personal", "root rules", "service rules"]


def test_override_file_wins_over_agents_md(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("shared")
    (tmp_path / "AGENTS.override.md").write_text("local only")
    assert [d.content for d in mod.discover_agents_docs(tmp_path)] == ["local only"]


def test_skill_index_carries_only_name_and_description(tmp_path):
    skills_root = tmp_path / "skills" / "release"
    skills_root.mkdir(parents=True)
    body = "# Release\n" + ("step\n" * 1000)
    (skills_root / "SKILL.md").write_text(
        '---\nname: "release"\ndescription: "Cut a release."\nmetadata:\n  short-description: "x"\n---\n\n' + body
    )
    skill = mod.parse_skill(skills_root / "SKILL.md", "user")
    assert skill.name == "release"
    assert skill.description == "Cut a release."
    assert "step" not in skill.index_line()
    assert len(skill.index_line()) < len(body) / 10


def test_skill_without_a_description_is_skipped(tmp_path):
    root = tmp_path / "skills" / "mystery"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text('---\nname: "mystery"\n---\n\nbody\n')
    assert mod.parse_skill(root / "SKILL.md", "user") is None
    assert mod.discover_skills((tmp_path / "skills", "user")) == []


def test_prompt_uses_the_developer_channel_for_harness_facts(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("rules")
    prompt = mod.build_prompt(tmp_path, codex_home=tmp_path / "home", permissions_block="<permissions/>")
    roles = [item["role"] for item in prompt.items]
    assert roles[0] == "developer"
    assert "user" in roles
