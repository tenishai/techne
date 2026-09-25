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
from techne.core import capabilities as capmod
from techne.core.capabilities import (
    CAPABILITIES,
    INVITE_STATE_KEY,
    acknowledge,
    capability_report,
    clear_acknowledgement,
    invitation,
    store_facts,
)
from techne.core.database import connect
from techne.core.extension_state import get_state

#: Hook-flag entries only. Probe entries (#1022) read the machine, not config.
HOOK_CAPS = tuple(c for c in CAPABILITIES if not c.probe)
PROBE_CAPS = tuple(c for c in CAPABILITIES if c.probe)


def _set_probes(monkeypatch, **states):
    """Pin every probe (default: in place) so results do not depend on this machine."""
    probes = {c.probe: (lambda cfg, v=states.get(c.probe, True): v) for c in PROBE_CAPS}
    monkeypatch.setattr(capmod, "_PROBES", probes)


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    _set_probes(monkeypatch)
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    from techne.core.database import init_db

    init_db(cfg.db_path)
    return cfg


def _all_off(config):
    """Every capability OFF. The tests below are about how the report describes an
    OFF capability; before #1020 a fresh install was that state, now it is not.
    Probes are pinned per test with `_set_probes`."""
    for cap in HOOK_CAPS:
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
    on = {c["flag"] for c in report["capabilities"] if c["on"] and not c["probe"]}
    assert on == DEFAULT_ON
    assert report["off_count"] == len(HOOK_CAPS) - len(DEFAULT_ON)


def test_every_capability_carries_all_three_required_parts(config, monkeypatch):
    """#638 requires three things per capability, and a report missing any one of
    them is decorative rather than actionable: is it on, what it buys, and the
    exact line to enable it."""
    _set_probes(monkeypatch, **{c.probe: False for c in PROBE_CAPS})
    for cap in capability_report(_all_off(config))["capabilities"]:
        assert isinstance(cap["on"], bool)
        assert cap["buys"].strip(), f"{cap['flag']} has no plain-language benefit"
        if cap["probe"]:
            # Not a hooks line: a command or settings edit, fully rendered.
            body = cap["enable"].split("\n", 1)[1]
            assert body.strip() and "{" not in body, f"{cap['flag']}'s enable is not concrete"
            continue
        assert cap["flag"] in cap["enable"], f"{cap['flag']}'s enable line is not actionable"
        assert "hooks:" in cap["enable"], f"{cap['flag']} must say WHERE the line goes"


def test_enable_snippet_cannot_be_read_as_current_state(config, monkeypatch):
    """The snippet says `<flag>: true` about a flag that is FALSE. Handed over
    bare it is just valid YAML, and a reader takes it as status — which happened:
    an agent quoted it for an OFF flag and the operator concluded the flag was on.
    So the frame (which file, and that it is currently OFF) travels WITH it."""
    _set_probes(monkeypatch, **{c.probe: False for c in PROBE_CAPS})
    report = capability_report(_all_off(config))
    for cap in report["capabilities"]:
        assert not cap["on"]
        first = cap["enable"].splitlines()[0]
        assert first.startswith("#"), f"{cap['flag']}'s snippet opens as bare config"
        if cap["probe"]:
            assert "NOT IN PLACE" in first, f"{cap['flag']}'s snippet hides the state"
            continue
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
    for cap in HOOK_CAPS:
        assert hasattr(config.hooks, cap.flag), f"{cap.flag} is not a real hooks flag"
    for cap in PROBE_CAPS:
        assert cap.probe in capmod._PROBES, f"{cap.flag} names no probe"


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
    for cap in HOOK_CAPS:
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
        assert f"{len(HOOK_CAPS) - 1} capabilities are off" in text
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


# --- probe entries (techne#1022) -------------------------------------------


def _row(report, flag):
    return next((c for c in report["capabilities"] if c["flag"] == flag), None)


def test_a_probe_that_finds_a_gap_reports_it_off_and_invites(config, monkeypatch):
    """A missing piece that no hooks flag can see must still reach the report AND
    the session-start count, or the probe entries are decoration."""
    _set_probes(monkeypatch, vectors_backend=False)
    report = capability_report(config)
    row = _row(report, "vectors_backend")
    assert row["on"] is False
    assert "pipx install" in row["enable"] and "techne vectors backfill" in row["enable"]
    assert report["off_count"] == len(HOOK_CAPS) - len(DEFAULT_ON) + 1

    conn = connect(config.db_path)
    try:
        text = invitation(config, conn)
    finally:
        conn.close()
    assert f"{len(HOOK_CAPS) - len(DEFAULT_ON) + 1} capabilities are off" in text


def test_a_probe_that_cannot_tell_says_nothing(config, monkeypatch):
    """None is not False: "not cached" for a cache nobody read is a guess dressed
    as a fact, so an unmeasured or inapplicable entry is left out entirely."""
    _set_probes(monkeypatch, vectors_model=None)
    assert _row(capability_report(config), "vectors_model") is None
    conn = connect(config.db_path)
    try:
        text = invitation(config, conn) or ""
    finally:
        conn.close()
    assert f"{len(HOOK_CAPS) - len(DEFAULT_ON)} capabilities are off" in text


def test_a_probe_that_raises_does_not_sink_the_report(config, monkeypatch):
    def boom(cfg):
        raise RuntimeError("settings.json unparseable")

    monkeypatch.setitem(capmod._PROBES, "single_memory_layer", boom)
    report = capability_report(config)
    assert _row(report, "single_memory_layer") is None
    assert _row(report, "per_turn_recall") is not None


def test_model_probe_defers_to_the_backend_probe(config, monkeypatch):
    """With no backend installed, "model not cached" is the same gap said twice."""
    from techne.core import embeddings

    monkeypatch.setattr(embeddings, "backend_available", lambda: False)
    assert capmod._probe_vectors_model(config) is None
    monkeypatch.setattr(embeddings, "backend_available", lambda: True)
    monkeypatch.setattr(embeddings, "model_cached", lambda cfg=None: False)
    assert capmod._probe_vectors_model(config) is False
    monkeypatch.setattr(embeddings, "model_cached", lambda cfg=None: None)
    assert capmod._probe_vectors_model(config) is None


def test_auto_memory_probe_respects_spool_primary(config, monkeypatch):
    """Under spool-primary techne works WITH Claude Code's memory on purpose, so
    auto-memory being on there is the chosen setup, not a gap to report."""
    from techne.platforms.claude_code import hooks

    monkeypatch.setattr(hooks, "auto_memory_enabled", lambda: True)
    config.platform = "claude-code"
    config.posture.mode = "observe"
    assert capmod._probe_single_memory_layer(config) is False
    config.posture.mode = "spool-primary"
    assert capmod._probe_single_memory_layer(config) is None
    config.posture.mode = "observe"
    config.platform = "standalone"
    assert capmod._probe_single_memory_layer(config) is None
    config.platform = "claude-code"
    monkeypatch.setattr(hooks, "auto_memory_enabled", lambda: False)
    assert capmod._probe_single_memory_layer(config) is True


def test_auto_memory_enable_names_the_real_settings_file(config, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    _set_probes(monkeypatch, single_memory_layer=False)
    row = _row(capability_report(config), "single_memory_layer")
    assert str(tmp_path / "cc" / "settings.json") in row["enable"]
    assert '"autoMemoryEnabled": false' in row["enable"]


# --- acknowledgements (techne#1022) ----------------------------------------


def test_acknowledged_opt_out_stops_counting_but_stays_visible(config):
    """A deliberate choice must stop nagging, and must not vanish from the report —
    the operator should still see what they chose and how to undo it."""
    _all_off(config)
    conn = connect(config.db_path)
    try:
        acknowledge(conn, "per_turn_seed", "too chatty for me")
        text = invitation(config, conn)
    finally:
        conn.close()
    assert f"{len(HOOK_CAPS) - 1} capabilities are off" in text

    report = capability_report(config)
    row = _row(report, "per_turn_seed")
    assert row["on"] is False
    assert row["acknowledged"]["reason"] == "too chatty for me"
    assert report["off_count"] == len(HOOK_CAPS)
    assert report["unacknowledged_off_count"] == len(HOOK_CAPS) - 1


def test_invitation_is_silent_when_every_off_item_is_acknowledged(config, monkeypatch):
    monkeypatch.setattr(capmod, "workflow_state", lambda cfg: [])
    conn = connect(config.db_path)
    try:
        for cap in HOOK_CAPS:
            if not getattr(config.hooks, cap.flag):
                acknowledge(conn, cap.flag)
        assert invitation(config, conn) is None
    finally:
        conn.close()


def test_acknowledgements_do_not_expire_across_versions(config, monkeypatch):
    """Decided for #1022: an opt-out is a decision, not a snooze."""
    _all_off(config)
    conn = connect(config.db_path)
    try:
        acknowledge(conn, "capture_read")
        invitation(config, conn)
        import techne

        monkeypatch.setattr(techne, "__version__", "99.0.0", raising=False)
        text = invitation(config, conn)
    finally:
        conn.close()
    assert f"{len(HOOK_CAPS) - 1} capabilities are off" in text


def test_clearing_an_acknowledgement_counts_it_again(config):
    conn = connect(config.db_path)
    try:
        acknowledge(conn, "capture_read")
        assert clear_acknowledgement(conn, "capture_read") is True
        assert clear_acknowledgement(conn, "capture_read") is False
    finally:
        conn.close()
    assert _row(capability_report(config), "capture_read")["acknowledged"] is None


def test_acknowledging_an_unknown_flag_is_refused(config):
    """A typo recorded as an opt-out would silently acknowledge nothing."""
    conn = connect(config.db_path)
    try:
        with pytest.raises(KeyError):
            acknowledge(conn, "per_turn_recal")
    finally:
        conn.close()


def test_cli_ack_and_unack_round_trip(config):
    from click.testing import CliRunner

    from techne.cli import main

    runner = CliRunner()
    run = lambda *a: runner.invoke(main, ["capabilities", *a], obj={"config": config})  # noqa: E731

    res = run("ack", "per_turn_seed", "--reason", "not for me")
    assert res.exit_code == 0, res.output
    shown = run()
    assert "OFF ON PURPOSE (1)" in shown.output and "not for me" in shown.output
    assert run("--json").exit_code == 0

    assert "acknowledgement cleared" in run("unack", "per_turn_seed").output
    assert "OFF ON PURPOSE" not in run().output
    assert run("ack", "no_such_flag").exit_code == 2


def test_mcp_tool_records_and_clears_an_acknowledgement(config):
    from techne.mcp_server import TOOL_HANDLERS

    tool = TOOL_HANDLERS["techne_capabilities"]
    out = tool({"acknowledge": "capture_read", "reason": "ledger size"}, config)
    assert out["change"]["acknowledged"] == "capture_read"
    assert _row(out, "capture_read")["acknowledged"]["reason"] == "ledger size"

    out = tool({"unacknowledge": "capture_read"}, config)
    assert out["change"]["cleared"] is True
    assert _row(out, "capture_read")["acknowledged"] is None

    assert "error" in tool({"acknowledge": "nope"}, config)
    config.db_path = config.data_dir / "never-initialised.db"
    assert "error" in tool({"acknowledge": "capture_read"}, config)
    assert not config.db_path.exists(), "an ack must not create a store"
    assert "error" in tool({"acknowledge": "capture_read", "unacknowledge": "capture_read"}, config)
