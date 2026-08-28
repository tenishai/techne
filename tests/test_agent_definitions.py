"""The research sandbox ships, and a widened one is reported loudly (techne#754).

WHY THIS FILE EXISTS. `techne guide investigating` tells an agent to do its fetching
inside a subagent restricted to web-only tools. That instruction is UNENFORCEABLE if the
operator has no such definition — they reach for a general research agent instead, which
has a shell, and an agent that can both read attacker-controlled pages and write to the
pattern store is one injection away from writing whatever a page tells it to. After the
fact nobody can separate agent-authored content from injection-authored content.

So the definition is not documentation. It is the control, and `tools: WebFetch,
WebSearch` in its frontmatter is the whole of it.
"""

from __future__ import annotations

import pytest

from techne.config import Config
from techne.platforms.claude_code.agents import (
    AGENT_NAMES,
    REQUIRED_TOOLS_LINE,
    agents_dir,
    agents_status,
    bundled_agent_text,
    register_agents,
    unregister_agents,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # THE OWNERSHIP MANIFEST LIVES IN techne.db, and both _load_manifest and
    # _save_manifest early-return when the db is absent. Without one, every install
    # records nothing and `status` can never distinguish our upgrade from your edit —
    # so a fixture without a db would test a degraded path and call it the real one.
    from techne.core.database import connect

    connect(tmp_path / "techne.db").close()
    return tmp_path


def test_the_shipped_definition_is_the_sandbox():
    """If the frontmatter stops granting exactly the two web tools, the guide's security
    model is a comment. Asserted on the SHIPPED text, not on a copy."""
    text = bundled_agent_text("research-readonly")
    assert REQUIRED_TOOLS_LINE in text
    for forbidden in ("Bash", "Write", "Edit", "NotebookEdit"):
        assert f"tools: {forbidden}" not in text
    # The body must keep restating the constraints: the harness enforces them, but an
    # agent that believes it has a shell wastes a turn discovering otherwise.
    assert "No shell" in text or "no shell" in text.lower()


def test_install_then_status_reports_it_present(home):
    cfg = Config.load()
    results = register_agents(cfg)
    assert [r.action for r in results] == ["installed"]
    assert (agents_dir() / "research-readonly.md").is_file()
    assert [r.action for r in agents_status(cfg)] == ["unchanged"]


def test_a_missing_definition_says_the_guide_cannot_be_followed(home):
    """An absent sandbox is not a cosmetic gap, and the report should not read like one."""
    (detail,) = [r.detail for r in agents_status(Config.load())]
    assert "investigating" in detail
    assert "install" in detail


def test_an_edited_definition_is_kept_not_clobbered(home):
    """Same discipline as the skills installer: techne would rather tell you your copy
    diverged than silently restore its own. Someone may have tuned the description."""
    cfg = Config.load()
    register_agents(cfg)
    path = agents_dir() / "research-readonly.md"
    edited = path.read_text().replace("model: sonnet", "model: haiku")
    path.write_text(edited)

    (res,) = register_agents(cfg)
    assert res.action == "kept-user-copy"
    assert path.read_text() == edited, "techne overwrote an operator's edit"


def test_a_WIDENED_tool_list_is_called_out_specifically(home):
    """THE DIVERGENCE THAT MATTERS, and the reason this is not just the skills installer
    with a different directory. A drifted procedure gives worse research. A drifted
    SANDBOX gives the research agent tools the procedure assumes it cannot have — so a
    page it fetches can reach whatever those tools touch.

    A bare "user-modified" would file that under the same heading as a reworded sentence.
    """
    cfg = Config.load()
    register_agents(cfg)
    path = agents_dir() / "research-readonly.md"
    widened = path.read_text().replace(
        REQUIRED_TOOLS_LINE, "tools: WebFetch, WebSearch, Bash"
    )
    path.write_text(widened)

    (res,) = register_agents(cfg)
    assert res.action == "kept-user-copy"
    assert "SANDBOX" in res.detail.upper(), res.detail
    assert "Bash" not in res.detail or "widened" in res.detail.lower()

    (status,) = agents_status(cfg)
    assert status.action == "widened", status.action


def test_an_edit_that_keeps_the_sandbox_is_not_alarmed_about(home):
    """The counterpart, so the loud path stays meaningful: editing the prose while the
    tool line survives reports as an ordinary user copy, not as a security finding."""
    cfg = Config.load()
    register_agents(cfg)
    path = agents_dir() / "research-readonly.md"
    path.write_text(path.read_text() + "\n\nMy own note.\n")

    (status,) = agents_status(cfg)
    assert status.action == "kept-user-copy"
    assert "intact" in status.detail


def test_remove_keeps_a_file_techne_no_longer_owns(home):
    """Uninstalling techne is not a licence to delete someone's edited definition."""
    cfg = Config.load()
    register_agents(cfg)
    path = agents_dir() / "research-readonly.md"
    path.write_text(path.read_text() + "\nmine\n")

    (res,) = unregister_agents(cfg)
    assert res.action == "kept-user-copy"
    assert path.is_file()


def test_force_takes_the_shipped_version_back(home):
    cfg = Config.load()
    register_agents(cfg)
    path = agents_dir() / "research-readonly.md"
    path.write_text("tools: WebFetch, WebSearch, Bash\n")

    (res,) = register_agents(cfg, force=True)
    assert res.action == "updated"
    assert path.read_text() == bundled_agent_text("research-readonly")


def test_the_guide_and_the_definition_agree_on_the_tool_set():
    """Two artifacts stating one constraint drift apart silently — the guide says
    "web-only, no shell", the definition grants the tools. If they ever disagree the
    guide is describing a sandbox that does not exist."""
    from techne.docs import bundled_guide

    guide = bundled_guide("investigating")
    assert "web-only" in guide.lower()
    assert "WebFetch" in bundled_agent_text("research-readonly")
    assert "do not substitute a type that has shell access" in guide.lower()


def test_the_installer_names_are_not_empty():
    """A precondition: every test above passes vacuously if AGENT_NAMES is empty."""
    assert AGENT_NAMES


@pytest.mark.parametrize(
    "shape,intact,label",
    [
        ("---\nname: x\ntools: WebFetch, WebSearch\n---\nbody\n", True, "flow scalar"),
        ("---\nname: x\ntools:\n  - WebFetch\n  - WebSearch\n---\n", True, "yaml sequence"),
        (
            "---\nname: x\n#tools: Bash\ntools: WebFetch, WebSearch\n---\n",
            True,
            "commented decoy",
        ),
        ("---\nname: x\ntools: WebFetch, WebSearch, Bash\n---\n", False, "suffix widen"),
        (
            "---\ndescription: |\n  tools: WebFetch, WebSearch\n"
            "tools: WebFetch, WebSearch, Bash\n---\n",
            False,
            "decoy in a block scalar, real grant widened",
        ),
        (
            "---\ntools: WebFetch, WebSearch\ntools: Bash, Write, Read\n---\n",
            False,
            "duplicate key",
        ),
        ("---\nname: x\nmetadata:\n  tools: WebFetch, WebSearch\n---\n", False, "nested key only"),
        ("---\nname: x\n---\nprose says tools: WebFetch, WebSearch\n", False, "body line only"),
        ("---\nname: x\ntools: WebFetch\n---\n", False, "narrowed"),
        ("---\nname: x\n---\n", False, "no grant at all"),
        ("not frontmatter at all\n", False, "no frontmatter"),
    ],
)
def test_sandbox_detection_parses_rather_than_scans(shape, intact, label):
    """THIS FUNCTION HAS FAILED OPEN TWICE, both times reading as correct.

    v1: `REQUIRED_TOOLS_LINE in text` — True for `tools: WebFetch, WebSearch, Bash`,
    because the widening is a SUFFIX.
    v2: compared the stripped `tools:` line — broken four more ways (Prudence, #807 r1):
    strip() erased indentation so a NESTED key counted; no frontmatter boundary so a
    BODY line counted; it returned the FIRST match while YAML resolves a duplicate to
    the LAST; and it knew only the flow spelling, so the SEQUENCE form read as widened.

    Rows 7 and 8 need no adversary — an operator restructuring their own frontmatter, or
    documenting the tool line in prose, got `sandbox intact` over a shell grant.

    Parametrised because each row is a distinct v2 failure and a single combined
    assertion would let a partial regression hide behind the others.
    """
    from techne.platforms.claude_code.agents import _sandbox_intact

    assert _sandbox_intact(shape) is intact, label


def test_status_separates_your_edit_from_our_upgrade(home):
    """`agents_status` loaded the manifest and never read it (Prudence, #807 r1 I2), so it
    could not tell "the operator edited this" from "techne shipped a newer version and
    this install has not taken it" — and reported the second as the first, on the command
    that exists to say whether the sandbox is trustworthy.

    Simulates the upgrade: the file on disk is EXACTLY what techne wrote (manifest sha
    matches), while the shipped text has moved on.
    """
    from unittest.mock import patch

    from techne.platforms.claude_code.agents import agents_status, register_agents

    cfg = Config.load()
    register_agents(cfg)

    shipped_now = bundled_agent_text("research-readonly") + "\n<!-- later -->\n"
    target = "techne.platforms.claude_code.agents.bundled_agent_text"
    with patch(target, return_value=shipped_now):
        (res,) = agents_status(cfg)

    assert res.action == "outdated", f"an untouched file reported as {res.action!r}"
    assert "not an edit of yours" in res.detail
