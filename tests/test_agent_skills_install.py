"""The Agent Skills renderer — the second host for the same bundled procedures.

The point of these tests is NOT that files get written. It is that the second
renderer earns its keep: one source of procedure text, two layouts, and two
independent ownership records so installing to one host cannot corrupt what techne
believes about the other.
"""

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.platforms.agent_skills import skills as agent_skills
from techne.platforms.claude_code import skills as claude_skills
from techne.skills import SKILL_NAMES, bundled_skill_text


@pytest.fixture
def env(tmp_path):
    config = Config()
    config.db_path = tmp_path / "techne.db"
    init_db(config.db_path).close()
    return config, tmp_path / "agents-skills"


def test_layout_is_a_directory_per_skill_not_a_flat_file(env):
    """The whole reason a second renderer exists. Claude Code takes commands/<name>.md;
    the open standard takes <name>/SKILL.md. Same text, different shape."""
    config, target = env
    agent_skills.register_skills(config, target=target)

    for name in SKILL_NAMES:
        assert (target / name / "SKILL.md").is_file(), f"{name} not in Agent Skills layout"
        assert not (target / f"{name}.md").exists(), f"{name} written in the flat layout"


def test_both_renderers_emit_byte_identical_text(env):
    """One procedure, two hosts. If these ever diverge, a fix to a procedure reaches
    one host and not the other — which is the drift that moving into the wheel was
    meant to end."""
    config, target = env
    agent_skills.register_skills(config, target=target)

    for name in SKILL_NAMES:
        written = (target / name / "SKILL.md").read_text(encoding="utf-8")
        assert written == bundled_skill_text(name)


def test_the_two_hosts_keep_independent_ownership_records(env, tmp_path, monkeypatch):
    """Editing the copy on one host must not make techne think the OTHER host's copy
    diverged. Distinct manifest keys are what guarantee that, so assert on the
    behaviour rather than on the key string."""
    config, target = env
    cc_dir = tmp_path / "claude" / "commands"
    monkeypatch.setattr(claude_skills, "skills_dir", lambda: cc_dir)

    claude_skills.register_skills(config)
    agent_skills.register_skills(config, target=target)

    # Edit ONLY the Agent Skills copy.
    edited = target / "self-care" / "SKILL.md"
    edited.write_text(bundled_skill_text("self-care") + "\n<!-- local tweak -->\n")

    agent_rows = {r.name: r.action for r in agent_skills.skills_status(config, target=target)}
    claude_rows = {r.name: r.action for r in claude_skills.skills_status(config)}

    assert agent_rows["self-care"] == "user-modified"
    assert claude_rows["self-care"] == "current", "editing one host disturbed the other"


def test_a_user_edited_file_is_kept_not_overwritten(env):
    """Same discipline as the Claude Code renderer: techne reports divergence rather
    than silently restoring its own copy."""
    config, target = env
    agent_skills.register_skills(config, target=target)

    path = target / "techne-promote" / "SKILL.md"
    path.write_text("mine now\n")

    rows = {r.name: r.action for r in agent_skills.register_skills(config, target=target)}
    assert rows["techne-promote"] == "kept-user-copy"
    assert path.read_text() == "mine now\n"

    forced = {r.name: r.action for r in agent_skills.register_skills(config, target=target, force=True)}
    assert forced["techne-promote"] == "updated"
    assert path.read_text(encoding="utf-8") == bundled_skill_text("techne-promote")


def test_reinstall_is_idempotent(env):
    config, target = env
    first = {r.name: r.action for r in agent_skills.register_skills(config, target=target)}
    second = {r.name: r.action for r in agent_skills.register_skills(config, target=target)}

    assert set(first.values()) == {"installed"}
    assert set(second.values()) == {"unchanged"}


def test_remove_takes_only_what_techne_owns_and_cleans_the_empty_dir(env):
    """An emptied <name>/ left behind reads to a host as a broken skill, so the
    directory goes with the file it held."""
    config, target = env
    agent_skills.register_skills(config, target=target)

    kept = target / "survey-field" / "SKILL.md"
    kept.write_text("edited\n")

    rows = {r.name: r.action for r in agent_skills.unregister_skills(config, target=target)}

    assert rows["survey-field"] == "kept-user-copy"
    assert kept.exists()
    assert not (target / "self-care").exists(), "owned skill's directory not cleaned up"


def test_a_tree_installed_by_the_old_shell_script_is_adoptable(env):
    """Files placed by install-elsewhere.sh have no manifest entry. Without adoption
    they would read as user-modified forever and never take an upgrade — the same
    pre-#641 case the Claude Code renderer handles."""
    config, target = env
    path = target / "self-care" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(bundled_skill_text("self-care"), encoding="utf-8")

    rows = {r.name: r.action for r in agent_skills.register_skills(config, target=target)}
    assert rows["self-care"] == "unchanged"

    status = {r.name: r.action for r in agent_skills.skills_status(config, target=target)}
    assert status["self-care"] == "current"


def test_host_defaults_resolve_and_an_explicit_target_wins(tmp_path):
    assert agent_skills.skills_dir("goose").root.as_posix().endswith(".config/agents/skills")
    assert agent_skills.skills_dir("crush").root.as_posix().endswith(".agents/skills")

    explicit = agent_skills.skills_dir("goose", tmp_path / "elsewhere")
    assert explicit.root == tmp_path / "elsewhere"

    with pytest.raises(ValueError, match="unknown host"):
        agent_skills.skills_dir("nonesuch")
