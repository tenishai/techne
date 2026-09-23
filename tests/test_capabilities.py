"""techne#638 — the capability report, and the discipline on its invitation.

The report's job is to answer "what can techne do HERE" using the user's own
store. The invitation's job is to say so ONCE. Both halves are tested, but the
one-shot gate gets the most attention: it is the mitigation for this feature's
stated risk (#638's "one design mistake from a clippy"), so a regression there
is the failure that matters.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.config import Config
from techne.core.capabilities import (
    CAPABILITIES,
    INVITE_STATE_KEY,
    capability_report,
    invitation,
    store_facts,
)
from techne.core.database import connect
from techne.core.extension_state import get_state


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    from techne.core.database import init_db

    init_db(cfg.db_path)
    return cfg


def _all_off(config):
    """Every capability OFF. The tests below are about how the report describes an
    OFF capability; before #1020 a fresh install was that state, now it is not."""
    for cap in CAPABILITIES:
        setattr(config.hooks, cap.flag, False)
    return config


#: On by default since #1020: a first-time user should not get a worse techne
#: because they never heard of a flag. The rest stay opt-in.
DEFAULT_ON = {
    "per_turn_recall",
    "capture_bash",
    "codex_grep_assist",
    "codex_edit_rail",
    "codex_reindex_on_edit",
}


def test_fresh_install_reports_exactly_the_default_on_set(config):
    """#638 existed because partners crossed six migrations and felt nothing, the
    capabilities all being opt-in. #1020 turned the recommended ones on by default;
    the report must say so, and still offer the others."""
    report = capability_report(config)
    on = {c["flag"] for c in report["capabilities"] if c["on"]}
    assert on == DEFAULT_ON
    assert report["off_count"] == len(CAPABILITIES) - len(DEFAULT_ON)


def test_every_capability_carries_all_three_required_parts(config):
    """#638 requires three things per capability, and a report missing any one of
    them is decorative rather than actionable: is it on, what it buys, and the
    exact line to enable it."""
    for cap in capability_report(_all_off(config))["capabilities"]:
        assert isinstance(cap["on"], bool)
        assert cap["buys"].strip(), f"{cap['flag']} has no plain-language benefit"
        assert cap["flag"] in cap["enable"], f"{cap['flag']}'s enable line is not actionable"
        assert "hooks:" in cap["enable"], f"{cap['flag']} must say WHERE the line goes"


def test_enable_snippet_cannot_be_read_as_current_state(config):
    """The snippet says `<flag>: true` about a flag that is FALSE. Handed over
    bare it is just valid YAML, and a reader takes it as status — which happened:
    an agent quoted it for an OFF flag and the operator concluded the flag was on.
    So the frame (which file, and that it is currently OFF) travels WITH it."""
    report = capability_report(_all_off(config))
    for cap in report["capabilities"]:
        assert not cap["on"]
        first = cap["enable"].splitlines()[0]
        assert first.startswith("#"), f"{cap['flag']}'s snippet opens as bare config"
        assert "OFF" in first, f"{cap['flag']}'s snippet does not state the current state"
        assert "config.yaml" in first, f"{cap['flag']}'s snippet does not name the file"


def test_enabled_capability_offers_nothing_to_enable(config):
    """An "enable" block for something already enabled is the same ambiguity with
    none of the payoff — there is no change for the reader to make."""
    config.hooks.per_turn_recall = True
    report = capability_report(config)
    recall = next(c for c in report["capabilities"] if c["flag"] == "per_turn_recall")
    assert recall["enable"] == ""


def test_flags_are_real_config_attributes(config):
    """A report that names a flag config does not have would hand the user a line
    that silently does nothing — worse than saying nothing at all."""
    for cap in CAPABILITIES:
        assert hasattr(config.hooks, cap.flag), f"{cap.flag} is not a real hooks flag"


def test_enabled_capability_drops_its_sales_pitch(config):
    """Evidence argues for switching something ON. Once it is on, the argument is
    spent — keeping it would be pitching a feature the user already has."""
    config.hooks.per_turn_recall = True
    report = capability_report(config)
    recall = next(c for c in report["capabilities"] if c["flag"] == "per_turn_recall")
    assert recall["on"] is True
    assert recall["evidence"] == ""


def test_evidence_is_grounded_in_the_actual_store(config):
    """The claim techne makes must be a count from their ledger, not a slogan."""
    conn = connect(config.db_path)
    try:
        for i in range(7):
            conn.execute(
                "INSERT INTO knowledge (type, content, confidence) VALUES ('fact', ?, 'high')",
                (f"durable thing {i}",),
            )
        conn.commit()
    finally:
        conn.close()

    facts = store_facts(config)
    assert facts.knowledge == 7

    recall = next(
        c
        for c in capability_report(_all_off(config))["capabilities"]
        if c["flag"] == "per_turn_recall"
    )
    assert "7" in recall["evidence"]


def test_empty_store_makes_no_claim(config):
    """An empty store has no argument to make. Inventing one is the clippy failure
    #638 explicitly warns about, so evidence stays empty rather than generic."""
    recall = next(
        c for c in capability_report(config)["capabilities"] if c["flag"] == "per_turn_recall"
    )
    assert recall["evidence"] == ""


def test_report_survives_a_store_it_cannot_read(config, monkeypatch):
    """An older install is precisely this report's audience and may predate a
    table it counts. Losing the other twenty facts over one missing table would
    deny the reader everything that IS knowable."""
    config.db_path = config.data_dir / "does-not-exist.db"
    report = capability_report(config)
    assert report["store"]["observations"] == 0
    assert report["capabilities"], "the switchable surface is knowable without the store"


# --- the one-shot gate ------------------------------------------------------


def test_invitation_fires_once_then_stays_silent(config):
    """The discipline IS the feature. #562 measured 74.5% of SessionStart's budget
    going to sessions that produce nothing; a recurring banner is how a real
    signal gets trained into noise."""
    conn = connect(config.db_path)
    try:
        first = invitation(config, conn)
        assert first is not None
        assert "Ask me what techne can do" in first

        assert invitation(config, conn) is None
        assert invitation(config, conn) is None
    finally:
        conn.close()


def test_invitation_returns_on_a_version_change(config, monkeypatch):
    """An upgrade is the only moment the news is both true and news."""
    conn = connect(config.db_path)
    try:
        assert invitation(config, conn) is not None
        assert invitation(config, conn) is None

        import techne

        monkeypatch.setattr(techne, "__version__", "99.0.0", raising=False)
        again = invitation(config, conn)
        assert again is not None
        assert "99.0.0" in again
    finally:
        conn.close()


def test_invitation_records_the_version_even_when_it_says_nothing(config):
    """A fully-configured install must not be re-evaluated every session for the
    rest of the version's life — that is the nag this gate exists to prevent."""
    for cap in CAPABILITIES:
        setattr(config.hooks, cap.flag, True)

    conn = connect(config.db_path)
    try:
        # Nothing to invite about → silence, but the version is still stamped.
        result = invitation(config, conn)
        assert result is None or "workflow" in result
        state = get_state(conn, INVITE_STATE_KEY)
        assert state is not None and state.get("version")
    finally:
        conn.close()


def test_invitation_counts_what_is_off(config):
    _all_off(config)
    config.hooks.per_turn_recall = True
    conn = connect(config.db_path)
    try:
        text = invitation(config, conn)
        assert f"{len(CAPABILITIES) - 1} capabilities are off" in text
    finally:
        conn.close()


def test_broken_store_surfaces_as_a_plain_sqlite_error(config):
    """Pinning HOW this fails, because SessionStart's handler is what keeps the
    session alive and it catches Exception. If the failure mode ever became a
    SystemExit or a bare string, that handler would stop covering it."""
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.Error):
            # No extension_state table — proves the failure is a plain sqlite
            # error the hook's handler covers, not something exotic.
            invitation(config, conn)
    finally:
        conn.close()


# --- the surfaces -----------------------------------------------------------


def test_mcp_tool_is_registered_and_returns_the_report(config):
    """Vendor-neutral by construction (techne#499) — the data comes out of MCP and
    the client's model narrates it, so no Claude-only install step is needed."""
    from techne.mcp_server import TOOL_HANDLERS, TOOLS

    assert any(t["name"] == "techne_capabilities" for t in TOOLS)
    report = TOOL_HANDLERS["techne_capabilities"]({}, config)
    assert report["capabilities"] and "narration_guidance" in report


def test_report_tells_the_agent_not_to_read_it_out(config):
    """The report is data. If the guidance ever stops saying so, the agent starts
    reciting a feature list — which is the thing #638 says does not work."""
    guidance = capability_report(config)["narration_guidance"]
    assert "conversation" in guidance and "not a feature list" in guidance.lower()
