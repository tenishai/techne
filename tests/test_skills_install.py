"""techne#641 — the operational procedures ship with the tool.

The behaviour under test is mostly about what the installer REFUSES to do. A
procedure is a document someone may have tuned, not plumbing techne owns, so the
interesting cases are all "techne must not silently overwrite this".
"""

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.platforms.claude_code import skills as skills_mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated techne home + Claude config dir."""
    cc = tmp_path / "cc"
    cc.mkdir()
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: cc / "commands")
    config = Config()
    config.db_path = tmp_path / "techne.db"
    init_db(config.db_path).close()
    return config, cc / "commands"


def test_bundled_procedures_are_packaged_and_portable(env):
    """They must be IN the wheel, and carry nothing machine-specific — the whole
    argument for shipping them with techne is that any install gets a correct copy."""
    for name in skills_mod.SKILL_NAMES:
        text = skills_mod.bundled_skill_text(name)
        assert text.startswith("---"), f"{name} lost its frontmatter"
        assert "description:" in text
        for leak in ("/Users/dimartin", "dimartin", "mash1", "richdimartino"):
            assert leak not in text, f"{name} carries machine-specific string {leak!r}"


def test_install_then_reinstall_is_idempotent(env):
    config, target = env
    first = {r.name: r.action for r in skills_mod.register_skills(config)}
    assert set(first.values()) == {"installed"}
    assert (target / "self-care.md").exists()

    second = {r.name: r.action for r in skills_mod.register_skills(config)}
    assert set(second.values()) == {"unchanged"}


def test_a_user_edited_procedure_is_never_silently_overwritten(env):
    """The load-bearing refusal. register_hooks strips and rewrites the block it
    owns; this must NOT, because the user may have tuned the procedure."""
    config, target = env
    skills_mod.register_skills(config)
    edited = target / "self-care.md"
    edited.write_text(edited.read_text() + "\n# my local tweak\n", encoding="utf-8")
    mine = edited.read_text()

    results = {r.name: r.action for r in skills_mod.register_skills(config)}
    assert results["self-care"] == "kept-user-copy"
    assert edited.read_text() == mine, "techne overwrote an edited procedure"
    assert results["techne-promote"] == "unchanged", "one diverged file must not block the others"


def test_force_takes_the_shipped_version(env):
    config, target = env
    skills_mod.register_skills(config)
    edited = target / "self-care.md"
    edited.write_text("gone\n", encoding="utf-8")

    results = {r.name: r.action for r in skills_mod.register_skills(config, force=True)}
    assert results["self-care"] == "updated"
    assert edited.read_text() == skills_mod.bundled_skill_text("self-care")


def test_an_upgrade_replaces_techne_s_own_older_copy(env, monkeypatch):
    """The upgrade path that makes this worth shipping: techne's own stale copy is
    replaced, and the user is not asked about a file they never touched."""
    config, target = env
    skills_mod.register_skills(config)
    # Simulate: techne 0.14 wrote this; 0.15 ships something different.
    monkeypatch.setattr(skills_mod, "bundled_skill_text", lambda name: f"NEW {name}\n")

    results = {r.name: r.action for r in skills_mod.register_skills(config)}
    assert set(results.values()) == {"updated"}
    assert (target / "self-care.md").read_text() == "NEW self-care\n"


def test_a_preexisting_identical_copy_adopts_cleanly(env):
    """A machine that already had the file from before #641 has no manifest entry.
    Without adoption it would read as user-modified forever and never upgrade."""
    config, target = env
    target.mkdir(parents=True, exist_ok=True)
    (target / "self-care.md").write_text(
        skills_mod.bundled_skill_text("self-care"), encoding="utf-8"
    )

    results = {r.name: r.action for r in skills_mod.register_skills(config)}
    assert results["self-care"] == "unchanged"
    assert skills_mod._load_manifest(config)["self-care"], "identical pre-existing copy not adopted"


def test_status_distinguishes_absent_current_and_user_modified(env):
    """#638 needs this: a config flag cannot tell you whether the hygiene pass is
    actually PRESENT on an install."""
    config, target = env
    assert {r.action for r in skills_mod.skills_status(config)} == {"absent"}

    skills_mod.register_skills(config)
    assert {r.action for r in skills_mod.skills_status(config)} == {"current"}

    edited = target / "self-care.md"
    edited.write_text(edited.read_text() + "x", encoding="utf-8")
    by_name = {r.name: r.action for r in skills_mod.skills_status(config)}
    assert by_name["self-care"] == "user-modified"
    assert by_name["techne-promote"] == "current"


def test_remove_keeps_a_modified_copy(env):
    """Uninstalling techne is not a licence to delete someone's edited document."""
    config, target = env
    skills_mod.register_skills(config)
    edited = target / "self-care.md"
    edited.write_text(edited.read_text() + "mine", encoding="utf-8")

    results = {r.name: r.action for r in skills_mod.unregister_skills(config)}
    assert results["self-care"] == "kept-user-copy"
    assert results["techne-promote"] == "removed"
    assert edited.exists()
    assert not (target / "techne-promote.md").exists()


def test_unknown_skill_name_is_refused(env):
    with pytest.raises(ValueError, match="unknown skill"):
        skills_mod.bundled_skill_text("not-a-skill")
