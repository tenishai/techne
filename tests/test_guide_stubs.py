"""techne#1040 — `techne init` installs the research-guide stubs.

A stub's whole job is to hold nothing that can go stale (#988 was two hand-kept
copies of a procedure drifting in both directions). So the tests check that the
stub is thin, that it loads the guide rather than quoting it, that its description
comes from the guide, and that it follows the same ownership rule as the
procedures: an edited stub is reported, never silently replaced.
"""

import json

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core.database import init_db
from techne.docs import bundled_guide
from techne.platforms.claude_code import skills as skills_mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated techne home + Claude config dir (skills/ and commands/)."""
    cc = tmp_path / "cc"
    cc.mkdir()
    monkeypatch.setattr(skills_mod, "claude_config_dir", lambda: cc)
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: cc / "commands")
    config = Config()
    config.db_path = tmp_path / "techne.db"
    init_db(config.db_path).close()
    return config, cc


def _stub(cc, name):
    return cc / "skills" / name / "SKILL.md"


@pytest.mark.parametrize("name", sorted(skills_mod.GUIDE_STUBS))
def test_the_stub_loads_its_guide_and_does_not_copy_it(name):
    guide = skills_mod.GUIDE_STUBS[name][0]
    text = skills_mod.render_guide_stub(name)

    assert text.startswith("---\n")
    assert f"\nname: {name}\n" in text
    # The load line: at the start of a line, pre-approved, and failure made visible.
    assert f"\n!`techne guide {guide} 2>&1 || echo " in text
    assert "Bash(techne guide *)" in text
    assert "$ARGUMENTS" in text
    # Thin: none of the guide's body rides along, so there is nothing to drift.
    body = bundled_guide(guide)
    assert "## The steps" in body
    assert "## The steps" not in text
    assert len(text) < len(body) / 5


@pytest.mark.parametrize("name", sorted(skills_mod.GUIDE_STUBS))
def test_the_description_is_the_one_the_guide_recommends(name):
    """One spelling of the description: the guide's own stub frontmatter."""
    guide = skills_mod.GUIDE_STUBS[name][0]
    section = bundled_guide(guide).split("## Make this a skill on your platform")[1]
    recommended = next(line for line in section.splitlines() if line.startswith("description: "))
    assert recommended in skills_mod.render_guide_stub(name)


@pytest.mark.parametrize(
    "desc", ["Use when: always", "Use when:", "- a list item", "#hashtag", "a # comment"]
)
def test_a_description_that_would_break_yaml_is_quoted(monkeypatch, desc):
    """Each of these breaks a plain scalar or silently changes its value. The rule is
    the parser's own round-trip, so the list here is a sample, not the definition."""
    import yaml

    monkeypatch.setattr(
        "techne.docs.bundled_guide",
        lambda name: f"## Make this a skill on your platform\ndescription: {desc}\n",
    )
    rendered = skills_mod._guide_description("investigating")
    assert rendered != desc
    assert yaml.safe_load(f"description: {rendered}") == {"description": desc}


@pytest.mark.parametrize("name", sorted(skills_mod.GUIDE_STUBS))
def test_no_stub_pre_approves_git(name):
    """No git rule is safe to pre-approve here: a prefix rule cannot stop options git
    reads after the URL (`-c credential.helper=!…`, `--template=…`), and this is the
    skill built around untrusted input. The clone prompts instead (#1042 r2)."""
    front = skills_mod.render_guide_stub(name).split("---\n")[1]
    assert "Bash(git" not in front


@pytest.mark.parametrize("name", sorted(skills_mod.GUIDE_STUBS))
def test_the_rendered_frontmatter_parses(name):
    import yaml

    front = skills_mod.render_guide_stub(name).split("---\n")[1]
    data = yaml.safe_load(front)
    assert data["name"] == name
    assert data["description"]


def test_install_then_reinstall_is_idempotent(env):
    config, cc = env
    first = {r.name: r.action for r in skills_mod.register_guide_stubs(config)}
    assert first == {name: "installed" for name in skills_mod.GUIDE_STUBS}
    for name in skills_mod.GUIDE_STUBS:
        assert _stub(cc, name).read_text() == skills_mod.render_guide_stub(name)

    second = {r.name: r.action for r in skills_mod.register_guide_stubs(config)}
    assert set(second.values()) == {"unchanged"}


def test_an_edited_stub_is_kept_and_force_takes_the_shipped_one(env):
    config, cc = env
    skills_mod.register_guide_stubs(config)
    edited = _stub(cc, "investigate-url")
    edited.write_text(edited.read_text() + "\n# my tweak\n", encoding="utf-8")
    mine = edited.read_text()

    results = {r.name: r.action for r in skills_mod.register_guide_stubs(config)}
    assert results["investigate-url"] == "kept-user-copy"
    assert edited.read_text() == mine
    assert results["survey-field"] == "unchanged"

    results = {r.name: r.action for r in skills_mod.register_guide_stubs(config, force=True)}
    assert results["investigate-url"] == "updated"
    assert edited.read_text() == skills_mod.render_guide_stub("investigate-url")


def test_a_hand_written_identical_stub_is_adopted(env):
    """The machine this was built on had hand-written stubs before the installer."""
    config, cc = env
    path = _stub(cc, "survey-field")
    path.parent.mkdir(parents=True)
    path.write_text(skills_mod.render_guide_stub("survey-field"), encoding="utf-8")

    results = {r.name: r.action for r in skills_mod.register_guide_stubs(config)}
    assert results["survey-field"] == "unchanged"


def test_a_legacy_command_and_a_plugin_under_the_same_name_are_reported(env):
    """#988's failure was two procedures under one name. Report them; never delete —
    they may be the user's."""
    config, cc = env
    legacy = cc / "commands" / "investigate-url.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("an old full copy\n", encoding="utf-8")
    (cc / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"survey-field@some-marketplace": True}}),
        encoding="utf-8",
    )

    results = {r.name: r for r in skills_mod.register_guide_stubs(config)}
    assert "command file" in results["investigate-url"].detail
    assert "enabled plugin survey-field@some-marketplace" in results["survey-field"].detail
    assert legacy.read_text() == "an old full copy\n"

    status = {r.name: r for r in skills_mod.guide_stubs_status(config)}
    assert "command file" in status["investigate-url"].detail


def test_a_disabled_plugin_is_not_a_collision(env):
    config, cc = env
    (cc / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"survey-field@m": False}}), encoding="utf-8"
    )
    assert skills_mod.guide_stub_collisions("survey-field") == []


def test_status_distinguishes_absent_current_and_user_modified(env):
    config, cc = env
    assert {r.action for r in skills_mod.guide_stubs_status(config)} == {"absent"}
    skills_mod.register_guide_stubs(config)
    _stub(cc, "survey-field").write_text("mine\n", encoding="utf-8")
    status = {r.name: r.action for r in skills_mod.guide_stubs_status(config)}
    assert status == {"investigate-url": "current", "survey-field": "user-modified"}


def test_remove_takes_only_what_techne_owns(env):
    config, cc = env
    skills_mod.register_guide_stubs(config)
    _stub(cc, "survey-field").write_text("mine\n", encoding="utf-8")

    results = {r.name: r.action for r in skills_mod.unregister_guide_stubs(config)}
    assert results == {"investigate-url": "removed", "survey-field": "kept-user-copy"}
    assert not (cc / "skills" / "investigate-url").exists(), "empty stub dir left behind"
    assert _stub(cc, "survey-field").read_text() == "mine\n"


def test_the_two_manifests_do_not_share_ownership(env):
    """Removing the stubs must not make techne forget it owns the procedures."""
    config, cc = env
    skills_mod.register_skills(config)
    skills_mod.register_guide_stubs(config)
    skills_mod.unregister_guide_stubs(config)
    procs = {r.name: r.action for r in skills_mod.skills_status(config)}
    assert set(procs.values()) == {"current"}
    assert skills_mod._load_manifest(config)  # procedures' ownership record survives


def test_skills_status_cli_lists_the_stubs(env):
    config, cc = env
    result = CliRunner().invoke(main, ["skills", "status"], obj={"config": config})
    assert result.exit_code == 0, result.output
    assert "Guide stubs directory:" in result.output
    for name in skills_mod.GUIDE_STUBS:
        assert f"  {name}: absent" in result.output
