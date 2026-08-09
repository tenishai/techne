"""Tests for the health summary + login banner (#76)."""

import pytest

from techne.config import Config
from techne.core.database import init_db
from techne.core.health import collect_health, format_banner


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    init_db(cfg.db_path).close()
    return cfg


def test_collect_health_db_ok(config):
    h = collect_health(config)
    assert h["db_ok"] is True
    assert h["version"]  # non-empty
    assert h["package_path"].endswith("techne")
    assert h["sync_adapter"] == "noop"
    # db_stats fields present
    for key in ("observations", "threads", "active_threads", "knowledge", "pending_outbox"):
        assert key in h


def test_collect_health_db_unreachable(tmp_path):
    cfg = Config()
    # Point at a path that cannot be opened as a DB (a directory).
    cfg.db_path = tmp_path  # a directory, not a file
    h = collect_health(cfg)
    assert h["db_ok"] is False
    assert "error" in h
    # The shared payload still carries the build metadata even on DB failure.
    assert h["version"]


def test_banner_healthy_live(config):
    h = collect_health(config)
    h["build"] = "live"
    banner = format_banner(h)
    # leads with a blank line so the heading clears Claude Code's systemMessage chrome
    assert banner.startswith("\n## techne ✅")
    assert "live" in banner
    assert "obs ·" in banner
    assert "threads" in banner


def test_banner_installed_build_warns(config):
    h = collect_health(config)
    h["build"] = "installed"
    banner = format_banner(h)
    assert "⚠️" in banner
    assert "may lag repo HEAD" in banner


def test_banner_db_unreachable(config):
    h = collect_health(config)
    h["db_ok"] = False
    h["error"] = "OperationalError: boom"
    banner = format_banner(h)
    assert "DB UNREACHABLE" in banner
    assert "boom" in banner


def test_banner_outbox_noop_no_warning(config):
    h = collect_health(config)
    h["build"] = "live"
    h["pending_outbox"] = 1189
    h["sync_adapter"] = "noop"
    banner = format_banner(h)
    assert "outbox: 1189 pending" in banner
    # noop never drains by design → informational, not a ⚠️ on the outbox line.
    outbox_line = [ln for ln in banner.splitlines() if "outbox" in ln][0]
    assert "⚠️" not in outbox_line


def test_banner_outbox_real_adapter_warns(config):
    h = collect_health(config)
    h["build"] = "live"
    h["pending_outbox"] = 5
    h["sync_adapter"] = "http"
    banner = format_banner(h)
    outbox_line = [ln for ln in banner.splitlines() if "outbox" in ln][0]
    assert "⚠️" in outbox_line


def test_banner_no_outbox_line_when_empty(config):
    h = collect_health(config)
    h["build"] = "live"
    h["pending_outbox"] = 0
    banner = format_banner(h)
    assert "outbox" not in banner


# --- Vector channel health (techne#358) ---------------------------------------


def _healthy_h(config):
    h = collect_health(config)
    h["build"] = "live"
    return h


def test_banner_vectors_no_backend_warns(config):
    """Stored vectors but no backend = the silent FTS-only trap → ⚠️."""
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": None,
        "has_vectors": True,
        "reembed_needed": False,
        "degraded": True,
    }
    banner = format_banner(h)
    vline = [ln for ln in banner.splitlines() if "vector" in ln.lower()][0]
    assert "⚠️" in vline
    assert "keyword-only" in vline
    assert "techne[vectors]" in vline


def test_banner_vectors_reembed_needed_warns(config):
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": "model2vec (dim=256)",
        "has_vectors": True,
        "reembed_needed": True,
        "degraded": True,
    }
    banner = format_banner(h)
    vline = [ln for ln in banner.splitlines() if "vector" in ln.lower()][0]
    assert "⚠️" in vline
    assert "stale" in vline
    assert "backfill" in vline


def test_banner_vectors_healthy_silent(config):
    """Healthy backend → no vector line (warnings-only banner)."""
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": "model2vec (dim=256)",
        "has_vectors": True,
        "reembed_needed": False,
        "degraded": False,
    }
    banner = format_banner(h)
    assert "vector" not in banner.lower()


def test_banner_vectors_never_used_silent(config):
    """No backend AND no vectors = never opted in → no false alarm."""
    h = _healthy_h(config)
    h["vectors"] = {
        "backend": None,
        "has_vectors": False,
        "reembed_needed": False,
        "degraded": False,
    }
    banner = format_banner(h)
    assert "vector" not in banner.lower()


def test_collect_health_maps_degraded_vector_status(config, monkeypatch):
    """collect_health folds vector_channel_status into a degraded verdict:
    stored vectors + no backend → degraded True."""
    fake = {
        "active_backend": None,
        "stamp": None,
        "tables": {"observations_vec": {"exists": True, "dim": 256, "rows": 10}},
        "reembed_needed": False,
    }
    monkeypatch.setattr(
        "techne.core.vector_status.vector_channel_status", lambda db_path, **kw: fake
    )
    h = collect_health(config)
    assert h["vectors"]["backend"] is None
    assert h["vectors"]["has_vectors"] is True
    assert h["vectors"]["degraded"] is True


def test_collect_health_vectors_healthy_not_degraded(config, monkeypatch):
    fake = {
        "active_backend": {"name": "model2vec", "dim": 256},
        "stamp": {"name": "model2vec", "dim": 256},
        "tables": {"observations_vec": {"exists": True, "dim": 256, "rows": 10}},
        "reembed_needed": False,
    }
    monkeypatch.setattr(
        "techne.core.vector_status.vector_channel_status", lambda db_path, **kw: fake
    )
    h = collect_health(config)
    assert h["vectors"]["degraded"] is False
    assert h["vectors"]["backend"] == "model2vec (dim=256)"


def test_collect_health_skips_backend_build_when_no_vectors(config, monkeypatch):
    """#361 review: on an install with no vec tables and no stamp, the session-start
    hot path must NOT build the embedding backend (the expensive load + dim probe)."""
    from unittest.mock import MagicMock

    spy = MagicMock()
    monkeypatch.setattr("techne.core.embeddings._get_embedding_model", spy)
    h = collect_health(config)  # fresh DB → no *_vec tables, no stamp
    spy.assert_not_called()  # backend build short-circuited
    assert h["vectors"]["degraded"] is False


def test_vector_status_probes_with_allow_download_false(config, monkeypatch):
    """The download-safety guarantee: when the backend IS built, it's with
    allow_download=False so a cold-cache fetch can't block the hook."""
    from unittest.mock import MagicMock

    from techne.core.vector_status import vector_channel_status

    fake_model = MagicMock()
    fake_model.name, fake_model.dim = "model2vec", 256
    spy = MagicMock(return_value=fake_model)
    monkeypatch.setattr("techne.core.embeddings._get_embedding_model", spy)
    # Default probe_backend_when_empty=True → build attempted even on an empty channel.
    vector_channel_status(config.db_path)
    spy.assert_called_once_with(allow_download=False)


def test_collect_health_vectors_none_on_status_error(config, monkeypatch):
    """collect_health degrades vectors→None (never raises) if the status check throws."""

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("techne.core.vector_status.vector_channel_status", boom)
    h = collect_health(config)
    assert h["vectors"] is None


# ---------------------------------------------------------------------------
# Per-category memory fill-state (techne#334)
# ---------------------------------------------------------------------------


def test_banner_memory_line_breaks_down_by_type(config):
    """The banner surfaces per-category knowledge counts so the agent can
    self-regulate what it writes (techne#334)."""
    from techne.core.knowledge import store

    store(config.db_path, "postgres is the datastore", type="fact")
    store(config.db_path, "another fact about caching", type="fact")
    store(config.db_path, "always use type hints", type="procedure")
    store(config.db_path, "chose FastAPI", type="decision")

    h = collect_health(config)
    assert h["knowledge_by_type"] == {"fact": 2, "procedure": 1, "decision": 1}

    banner = format_banner(h)
    assert "> memory:" in banner
    # canonical VALID_TYPES order: fact before decision before procedure
    assert "2 fact" in banner
    assert "1 decision" in banner
    assert "1 procedure" in banner
    assert banner.index("2 fact") < banner.index("1 decision") < banner.index("1 procedure")


def test_banner_memory_line_absent_on_empty_store(config):
    """No knowledge and no identity → no memory line (nothing to self-regulate)."""
    h = collect_health(config)
    assert h["knowledge_by_type"] == {}
    banner = format_banner(h)
    assert "> memory:" not in banner


def test_banner_memory_line_includes_identity(config):
    """Identity lives in its own table but folds into the same breakdown."""
    from techne.core.database import connect

    conn = connect(config.db_path)
    try:
        conn.execute(
            "INSERT INTO identity (key, value) VALUES ('user.name', 'Richard')"
        )
        conn.commit()
    finally:
        conn.close()

    h = collect_health(config)
    banner = format_banner(h)
    assert "1 identity" in banner


def test_banner_memory_line_appends_non_canonical_types_after_canonical(config):
    """A knowledge type not in VALID_TYPES (e.g. a legacy/other-domain 'pattern'
    row) still renders — appended AFTER the canonical types so the breakdown
    always accounts for the full total (techne#334, Prudence PR #366)."""
    from techne.core.database import connect
    from techne.core.knowledge import store

    store(config.db_path, "a real fact", type="fact")
    # 'pattern' is not in VALID_TYPES, so store() would reject it — insert direct.
    conn = connect(config.db_path)
    try:
        conn.execute(
            "INSERT INTO knowledge (type, content) VALUES ('pattern', 'some pattern')"
        )
        conn.commit()
    finally:
        conn.close()

    h = collect_health(config)
    assert h["knowledge_by_type"].get("pattern") == 1
    banner = format_banner(h)
    assert "1 pattern" in banner
    # canonical 'fact' renders BEFORE the non-canonical 'pattern'
    assert banner.index("1 fact") < banner.index("1 pattern")


class TestBannerLiveness:
    """The banner's liveness integration, which had NO tests at all (#469 review 8).

    Both load-bearing mutations survived the full suite: forcing ``checks_bad``
    to False (so the headline mark ignores subsystem liveness) and deleting the
    ``⚠️ VERDICT: names`` line outright. The comment in health.py nominates this
    as the surface the whole detector rests on — "a detector wired to a command
    nobody runs is worth no more than no detector" — and the entire mechanism
    that claim depends on could be deleted with the suite green.
    """

    def test_headline_mark_reflects_a_failing_subsystem(self, config):
        h = collect_health(config)
        h["build"] = "live"
        h["checks_worst"] = "stale"
        h["checks"] = [
            {"name": "rail:consolidate", "verdict": "stale"},
            {"name": "rail:dream", "verdict": "stale"},
        ]
        banner = format_banner(h)
        # A green top-line over a red detail is the precise shape of the bug
        # techne#468 exists to remove.
        assert banner.startswith("\n## techne ⚠️")
        # Named, not counted: "3 stale" sends you hunting, names are actionable.
        assert "STALE: rail:consolidate, rail:dream" in banner
        assert "`techne doctor`" in banner

    def test_dormant_is_not_a_warning(self, config):
        """DORMANT means correctly idle, not broken — it must not flip the mark
        or emit a verdict line, or the banner trains the eye to skip it."""
        h = collect_health(config)
        h["build"] = "live"
        h["checks_worst"] = "dormant"
        h["checks"] = [{"name": "rail:dream", "verdict": "dormant"}]
        banner = format_banner(h)
        assert banner.startswith("\n## techne ✅")
        assert "techne doctor" not in banner

    def test_absent_liveness_data_leaves_the_mark_alone(self, config):
        """No checks key at all (an older payload) must not manufacture a warning."""
        h = collect_health(config)
        h["build"] = "live"
        h.pop("checks_worst", None)
        h.pop("checks", None)
        assert format_banner(h).startswith("\n## techne ✅")

    def test_only_the_headline_verdict_is_named(self, config):
        """Listing a STALE rail under an UNOBSERVABLE label mislabels it; the
        others are counted, not named."""
        h = collect_health(config)
        h["build"] = "live"
        h["checks_worst"] = "unobservable"
        h["checks"] = [
            {"name": "hook:pre_compact", "verdict": "unobservable"},
            {"name": "rail:dream", "verdict": "stale"},
        ]
        banner = format_banner(h)
        assert "UNOBSERVABLE: hook:pre_compact" in banner
        assert "rail:dream" not in banner
        assert "1 other" in banner

    def test_a_failing_liveness_layer_names_the_error_not_the_empty_set(self, config):
        """The except arm of collect_health, end to end.

        `checks` is EMPTY there, so the naming code rendered
        "> ⚠️ UNKNOWN:  — `techne doctor`" — strictly worse than a bare count —
        while `checks_error` held the only actionable fact and was read nowhere
        in the repository. (#469 review 8, issue 2.)
        """
        h = collect_health(config)
        h["build"] = "live"
        h["checks"] = []
        h["checks_worst"] = "unknown"
        h["checks_error"] = "TypeError: unhashable type: 'list'"
        banner = format_banner(h)
        assert banner.startswith("\n## techne ⚠️")
        assert "liveness check FAILED: TypeError: unhashable type: 'list'" in banner
        # The empty-name line must be gone, not merely accompanied.
        assert "UNKNOWN:  —" not in banner

    def test_the_failure_path_is_reachable_from_collect_health(self, config, monkeypatch):
        """Pins the wiring, not just the formatter: a raising run_checks must
        land in the except arm with the exception recorded."""
        from techne.core import checks as checks_mod

        def _boom(config, **kwargs):
            # **kwargs: collect_health passes cheap_only=True (#487 review
            # finding 1). A stub with a narrower signature than the real function
            # fails on the CALL, not in the code under test — it would still go
            # red here, but for the wrong reason, and a future signature change
            # should not read as this path breaking.
            raise TypeError("unhashable type: 'list'")

        monkeypatch.setattr(checks_mod, "run_checks", _boom)
        h = collect_health(config)
        assert h["checks_worst"] == "unknown"
        assert h["checks_error"] == "TypeError: unhashable type: 'list'"
        assert "liveness check FAILED" in format_banner(h)


# ---- deferred checks: say what the banner did NOT run (techne#488) ---------


def test_deferred_check_names_are_exactly_the_expensive_ones():
    from techne.core.checks import deferred_check_names, registered_checks

    expected = [fn.__name__ for fn in registered_checks() if getattr(fn, "expensive", False)]
    assert deferred_check_names() == expected
    # Without this the test passes vacuously the day the last @expensive marker
    # is removed, and goes on "proving" the feature works with nothing in it.
    assert expected, "no @expensive check is registered — this assertion would prove nothing"


def test_collect_health_records_what_the_cheap_run_skipped(config):
    """The structured key, which is populated whether or not a warning renders."""
    from techne.core.checks import deferred_check_names

    h = collect_health(config)

    assert h["checks_deferred"] == deferred_check_names()
    # The whole point: the summary counts only what ran, so the reader needs an
    # independent statement of what did not.
    assert set(h["checks_deferred"]).isdisjoint({c["name"] for c in h["checks"]})


def test_deferred_never_becomes_a_verdict(config):
    """The rejected design, pinned so nobody re-adds it.

    A synthetic UNKNOWN placeholder for the skipped check was the obvious
    option. `health.py` ranks verdicts through `worst()`, so it would pin a
    permanent ⚠️ on every banner for checks that were never run — a cure worse
    than the disease. Deferred-ness is not a verdict.
    """
    h = collect_health(config)

    assert h["checks_deferred"], "fixture must actually defer something"
    assert "unknown" not in h["checks_summary"]
    assert h["checks_worst"] in ("live", "dormant", None), h["checks_worst"]
    # And the banner stays quiet: deferral alone is not a warning.
    assert "⚠️" not in format_banner(h)


def test_banner_names_the_deferred_checks_on_a_warning_line(config):
    # Built from a real payload rather than a hand-written dict: format_banner
    # reads keys this test does not care about, and hand-maintaining that shape
    # makes the test fail for reasons unrelated to what it asserts.
    h = collect_health(config)
    h["checks"] = [{"name": "rail:reflect", "verdict": "stale"}]
    h["checks_worst"] = "stale"
    h["checks_summary"] = {"stale": 1}
    h["checks_deferred"] = ["check_runtime_loaded"]

    banner = format_banner(h)

    assert "1 deferred: check_runtime_loaded" in banner
    # Attached to the existing liveness row, not given a row of its own.
    liveness_lines = [ln for ln in banner.splitlines() if "deferred" in ln]
    assert len(liveness_lines) == 1, banner
    # OCCURRENCES, not lines: the earlier version counted lines containing the
    # remedy and so passed happily while one line said `techne doctor` twice
    # (PR #526 review, nit 5).
    assert liveness_lines[0].count("techne doctor") == 1, liveness_lines[0]


def test_deferred_is_recorded_even_when_the_check_run_fails(config, monkeypatch):
    """Populated BEFORE run_checks, on purpose.

    The failure path is exactly where a reader most needs to know the summary is
    partial; setting the key afterwards would drop it there.
    """
    import techne.core.checks as checks_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(checks_mod, "run_checks", _boom)

    h = collect_health(config)

    assert h["checks_error"].startswith("RuntimeError")
    assert h["checks_deferred"], "deferred names were lost on the failure path"


def test_checks_deferred_is_none_not_absent_when_the_registry_cannot_be_read(config, monkeypatch):
    """The import-failure path (PR #526 review, issue 1).

    Setting the key inside the same `try` as the import meant an ImportError
    left it ABSENT — and a consumer doing `.get("checks_deferred", [])` then
    reads "nothing was deferred" at the exact moment EVERY check was skipped.
    `None` says "could not be determined", which is the true answer and is
    distinguishable from `[]`.
    """
    import builtins

    real_import = builtins.__import__

    def _fail_on_checks(name, *a, **kw):
        if name == "techne.core.checks":
            raise ImportError("simulated: checks module unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_on_checks)

    h = collect_health(config)

    assert "checks_deferred" in h, "key vanished — a consumer's .get(...,[]) now lies"
    assert h["checks_deferred"] is None, h["checks_deferred"]
    assert h["checks_error"].startswith("ImportError")
    # And the banner must not claim anything about a registry it could not read.
    assert "deferred" not in format_banner(h)


def test_the_liveness_failed_line_also_carries_the_deferred_note(config):
    """The other render site, which was untested (PR #526 review, nit 4).

    Deleting the suffix from the `liveness check FAILED` branch left the whole
    suite green — and that branch is the one the design rationale calls most
    load-bearing, since it is the path where the summary is most partial.
    """
    h = collect_health(config)
    h["checks"] = []
    h["checks_worst"] = "unknown"
    h["checks_summary"] = {"unknown": 1}
    h["checks_error"] = "RuntimeError: probe exploded"
    h["checks_deferred"] = ["check_runtime_loaded"]

    banner = format_banner(h)

    assert "liveness check FAILED" in banner
    assert "1 deferred: check_runtime_loaded" in banner
