"""Tests for `techne doctor` — core-DB health / duplicate-row audit.

The duplicate-row check exists to catch silent-accumulation bugs (a table growing
duplicates it should never have) — a failure mode with no functional symptom, so
it is invisible to behavior tests and code review and only shows up as storage
growth. thread_boundaries hit exactly that; this asserts the audit now catches it.
"""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from techne.cli import main


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """A Config pointed at a fresh (v-current) tmp db, injected via Config.load()."""
    from techne.config import Config
    from techne.core.database import init_db

    db = tmp_path / "techne.db"
    init_db(db).close()
    cfg = Config()
    cfg.db_path = db
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def _seed_duplicate_boundaries(db_path):
    """Force a duplicate-row state by dropping the guard index (simulating a
    pre-v17 / regressed DB) and inserting the same (session, pair) three times."""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('s1')")
    conn.execute("DROP INDEX IF EXISTS idx_thread_boundaries_triple")
    for _ in range(3):
        conn.execute(
            "INSERT INTO thread_boundaries (session_id, obs_index_a, obs_index_b, "
            "gap_seconds, family_a, family_b, signals_json, total_score, is_boundary) "
            "VALUES ('s1', 0, 1, 60.0, 'fs', 'fs', '{}', 0.0, 0)"
        )
    conn.commit()
    conn.close()


def test_doctor_clean_db_passes(cli_db):
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "no duplicates" in result.output
    assert "All checks passed" in result.output


def test_doctor_flags_duplicates_and_exits_nonzero(cli_db):
    # --no-liveness scopes this to what it is about (storage debt). doctor now
    # answers two independent questions and the summary line names both, so a
    # bare "issue(s) found" no longer appears (techne#468).
    _seed_duplicate_boundaries(cli_db.db_path)
    result = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert result.exit_code == 1, result.output
    assert "thread_boundaries" in result.output
    assert "duplicate rows" in result.output
    assert "storage issue(s)" in result.output


def test_doctor_exits_nonzero_when_a_subsystem_is_not_confirmed(cli_db):
    """Liveness alone must be able to fail the command.

    The regression this guards: on 2026-08-03 `techne doctor` printed "All
    checks passed" while consolidate and dream had been cold for four weeks,
    because it only ever looked at storage.
    """
    from techne.core.database import connect

    # Give the store activity, so missing hook markers are undecidable rather
    # than merely "nothing has happened yet".
    conn = connect(cli_db.db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES (?, ?)", ("s1", "2026-08-01 00:00:00")
        )
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "not confirmed working" in result.output


def _settings_path():
    """The tmp SETTINGS_PATH the autouse `_isolate_host_config` fixture pinned."""
    from techne.platforms.claude_code import hooks as hooks_mod

    return hooks_mod.SETTINGS_PATH


def _write_settings(body: str):
    """Write settings.json into the isolated host dir (the fixture pins the path
    but does not create the directory)."""
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _install_launcher():
    """Make the store look INSTALLED the way `techne init` makes it look installed.

    `register_hooks` writes techne-hook.sh next to settings.json and `unregister`
    deletes it, so its presence is the "techne installed hooks here" predicate the
    doctor drift block gates on. Derived from SETTINGS_PATH, so the isolation
    fixture already redirects it into tmp.
    """
    launcher = _settings_path().parent / "techne-hook.sh"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\n")

    # AND the MCP registration, because `techne init` writes both and "installed"
    # means both. Without it the MCP check (techne#370) correctly reports a
    # missing registration, which is a real failure but not the one these tests
    # isolate — they exist to move the exit code on DRIFT alone.
    import json as _json

    from techne.platforms.claude_code import hooks as hooks_mod

    mcp = hooks_mod.MCP_CONFIG_PATH
    mcp.parent.mkdir(parents=True, exist_ok=True)
    mcp.write_text(
        _json.dumps(
            {
                "mcpServers": {
                    "techne": {
                        "command": "/bin/sh",
                        "args": [str(launcher), "-m", "techne.mcp_server"],
                    }
                }
            }
        )
    )
    return launcher


def test_doctor_counts_drift_on_an_installed_system(cli_db):
    """Drift on an INSTALLED machine must fail the command.

    A hooks.* flag enabled in config.yaml but absent from settings.json is a hook
    that can never fire — the silent no-op techne#287 exists to catch. Scoped to a
    store where liveness itself is not failing (a fresh db is DORMANT, not
    UNOBSERVABLE), so the exit code moves ONLY because drift is counted: this dies
    to `failing += len(drift)` → `+= 0` and to deleting the block outright, the two
    mutants that survived review 6.
    """
    _install_launcher()
    # Valid JSON, correct shape, nothing registered — drift, not "unusable".
    _write_settings("{}")

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "Hook registration drift" in result.output
    # ASSERT THE COUNT, not just the noun. `drift_failures = len(drift)` → `= 1`
    # survived review 6's sweep because only the substring was pinned; derive the
    # expected number from the warning lines the same run printed.
    warnings = [ln for ln in result.output.splitlines() if ln.startswith("  ! ")]
    assert len(warnings) > 1, result.output
    assert f"{len(warnings)} hook registration issue(s)" in result.output
    # Counted separately from liveness: a drift warning is not a subsystem that
    # failed to run, and saying "N subsystem(s) not confirmed working" for it is
    # the honesty rule leaking through another door (review 6, issue 4).
    assert "subsystem(s) not confirmed working" not in result.output
    # The remedy carries the operator's ACTUAL posture, never a literal — a bare
    # `techne init` re-resolves `auto` and can change posture as a side effect.
    assert f"--posture {cli_db.posture.mode}" in result.output


def test_doctor_reports_but_does_not_count_drift_when_not_installed(cli_db):
    """A machine that never ran `techne init` must not exit 1 for drift.

    Every config-enabled hook reads as drifted there, and a red the operator
    cannot clear teaches them to ignore the signal — the critical-2 mistake fixed
    earlier in this PR. The warning is still PRINTED; only the count is withheld.
    """
    _write_settings("{}")  # present, but no launcher => not installed

    cli_db.posture.mode = "spool-primary"

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Hook registration drift" in result.output
    assert "not counted" in result.output
    assert "hook registration issue(s)" not in result.output
    # Even the INSTALL hint goes through init_remedy: a bare `techne init`
    # re-resolves --posture from `auto` and saves it, which is the same silent
    # demotion fixed at three other sites. (#469 review 9.)
    assert "--posture spool-primary" in result.output


def test_doctor_counts_drift_when_settings_json_is_missing_but_hooks_were_installed(cli_db):
    """The false negative the launcher proxy exists to close.

    A machine whose settings.json was deleted AFTER `techne init` looks identical
    to a fresh one if you gate on settings.json — so the loudest possible signal
    (every capture rail dead) was reported as "not counted — run `techne init` to
    install" to a system that has demonstrably already run. techne-hook.sh
    survives that deletion, so the two states are distinguishable after all.
    """
    _install_launcher()  # installed... and no settings.json at all

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "Hook registration drift" in result.output
    assert "hook registration issue(s)" in result.output
    assert "not counted" not in result.output


def test_doctor_names_the_settings_path_and_the_launcher(cli_db):
    """The interpolations, not just the surrounding prose.

    Setting `settings_path = None` / `launcher_name = None` at their assignment
    passed every test in this file while printing "repair or move aside None" and
    "(no None)" — because the assertions pinned the substring and never the
    value. (#469 review 8, issue 4.)
    """
    settings = _write_settings("{ this is not json")
    launcher = _install_launcher()

    unusable = CliRunner().invoke(main, ["doctor"]).output
    assert f"repair or move aside {settings}" in unusable

    launcher.unlink()  # installed -> not installed, same store
    not_installed = CliRunner().invoke(main, ["doctor"]).output
    assert f"(no {launcher.name})" in not_installed


def test_doctor_remedy_echoes_a_non_default_posture(cli_db):
    """The remedy must never hardcode a posture.

    `techne init --posture observe` handed to an operator in `spool-primary`
    DEMOTES THEM GLOBALLY — init resolves the flag, rebuilds PostureConfig and
    saves it, flipping reflect out of advisory — because the health check told
    them to. Asserted with a non-default value so a hardcoded "observe" cannot
    pass by coincidence. (#469 review 7, critical 2.)
    """
    _install_launcher()
    _write_settings("{}")
    cli_db.posture.mode = "spool-primary"

    result = CliRunner().invoke(main, ["doctor"])
    assert "--posture spool-primary" in result.output, result.output
    assert "--posture observe" not in result.output


def test_doctor_survives_the_drift_import_itself_failing(cli_db, monkeypatch):
    """The handler must not crash the command it exists to protect.

    Everything imported inside the try is a function-local for the whole
    function, so an import-time failure — a real one: hooks.py resolves the
    claude config dir at module scope and `Path("~nosuchuser/x").expanduser()`
    raises — left the handler referencing an unbound SETTINGS_PATH. The operator
    lost the entire remainder of the report to an UnboundLocalError, from the
    block written so a failing diagnostic "must not VANISH". (Review 7,
    critical 1.)
    """
    from techne.platforms.claude_code import hooks as hooks_mod

    # Make the from-import itself fail, the same shape as an import-time raise.
    # monkeypatch.delattr, never a bare delattr — these names must come back, or
    # the rest of the suite runs against a module we amputated.
    for name in ("SETTINGS_PATH", "_launcher_path", "check_hook_drift", "settings_unusable_reason"):
        monkeypatch.delattr(hooks_mod, name, raising=False)

    result = CliRunner().invoke(main, ["doctor"])
    # SystemExit is the ordinary non-zero exit; anything else is the crash.
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"doctor raised instead of reporting: {result.exception!r}"
    )
    assert "the drift check itself failed" in result.output
    # The REST OF THE REPORT still ran — the actual damage of the crash.
    assert "Reclaimable space" in result.output
    assert result.exit_code == 1, result.output


def test_doctor_blames_itself_not_the_operators_settings_when_the_probe_breaks(cli_db, monkeypatch):
    """An internal bug must not be routed to the settings.json remedy.

    Telling an operator whose settings.json is FINE to move aside the file
    holding the rest of their Claude Code configuration — because of a bug in our
    code — is the issue-2 mislabel arriving through the exception door. The
    heading is honest ("cannot be verified"); the remedy must not name a file we
    have no evidence against. (Review 7, issue 5.)
    """
    from techne.platforms.claude_code import hooks as hooks_mod

    _install_launcher()
    _write_settings("{}")  # healthy and readable — the point of the test

    def _boom(config):
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(hooks_mod, "check_hook_drift", _boom)

    result = CliRunner().invoke(main, ["doctor"])
    assert "Hook registration cannot be verified" in result.output
    assert "techne bug" in result.output
    assert "repair or move aside" not in result.output
    # Do not ask for an artefact this path withholds: it formats str(exc) only,
    # and nothing here prints a traceback. (Review 8, nit 5.)
    assert "traceback" not in result.output.lower()
    assert "hook registration issue(s)" in result.output


def test_doctor_summary_names_both_live_and_dormant(cli_db, monkeypatch):
    """The three-way honesty summary's middle branch, the one with no test.

    `elif dormant:` → `elif False:` survived the suite and made doctor print
    "All 6 subsystems confirmed live." beneath a table listing twelve, six of
    them dormant. That is the ordinary steady state of a healthy install, not a
    corner case — and it is finding 1's lie by another route. (Review 8, issue 3.)
    """
    from techne.core import checks as checks_mod
    from techne.core.checks import CheckResult, Kind, Verdict

    def _mixed(config):
        return [
            CheckResult(
                name=f"rail:{n}",
                subsystem="rails",
                kind=Kind.TEMPORAL,
                verdict=v,
                detail="",
            )
            for n, v in (("a", Verdict.LIVE), ("b", Verdict.LIVE), ("c", Verdict.DORMANT))
        ]

    monkeypatch.setattr(checks_mod, "run_checks", _mixed)

    result = CliRunner().invoke(main, ["doctor", "--no-liveness"])
    assert result.exit_code == 0  # sanity: the flag still skips the table

    result = CliRunner().invoke(main, ["doctor"])
    assert "2 live, 1 dormant — none failing." in result.output
    assert "subsystems confirmed live" not in result.output


def test_doctor_passes_when_no_checks_are_registered(cli_db, monkeypatch):
    """The empty-registry path returns two zeros, not one zero and a stray.

    Pins `return 0, 0`: with nothing registered there is nothing to fail and
    nothing drifted, so doctor must exit 0 and say why the table is missing.
    """
    from techne.core import checks as checks_mod

    monkeypatch.setattr(checks_mod, "run_checks", lambda config: [])

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "No subsystem checks registered." in result.output
    assert "All checks passed" in result.output


def test_doctor_reports_a_failing_drift_check_rather_than_swallowing_it(cli_db, monkeypatch):
    """A diagnostic that raises must not go SILENT.

    The first cut caught the exception into `drift, installed = [], False`, which
    printed nothing — and an absent check reads as "nothing wrong", the exact
    failure mode this module exists to prevent. run_checks holds the opposite
    policy: a check that raises becomes an UNKNOWN result CARRYING the exception.
    """
    from techne.platforms.claude_code import hooks as hooks_mod

    def _boom(config):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(hooks_mod, "check_hook_drift", _boom)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "the drift check itself failed" in result.output
    assert "probe exploded" in result.output
    assert "hook registration issue(s)" in result.output


def test_doctor_does_not_report_drift_on_another_platform(cli_db):
    """settings.json registration is claude-code's mechanism only.

    check_hook_drift does not self-guard, so without the platform gate an
    opencode operator who merely HAS a ~/.claude/settings.json — ordinary on a
    machine that runs both — gets exit 1 on a correct config plus a remedy
    telling them to reinstall onto a platform they do not use. doctor promises it
    is safe to run in CI; that is a red they cannot clear. (Review 6, critical 1.)
    """
    _install_launcher()
    _write_settings("{}")
    cli_db.platform = "opencode"

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Hook registration" not in result.output


def test_doctor_labels_an_unusable_settings_file_honestly(cli_db):
    """The word "drift" is a precise claim, and an unreadable settings.json is not it.

    There we cannot tell — and `techne init` is the wrong remedy besides:
    register_hooks walks the same structure without guarding it, so init itself
    raises on most unusable shapes. Handing over a command that crashes, at the
    moment every hook may be dead, is the lie PR #457 removed from the other two
    surfaces. (Review 6, issue 2.)
    """
    _install_launcher()
    _write_settings("{ this is not json")

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "cannot be verified" in result.output
    assert "Hook registration drift" not in result.output
    assert "repair or move aside" in result.output
    # The counting IS right here: a present-but-unreadable settings.json is a
    # real, actionable failure, unlike a machine that was never initialised.
    assert "hook registration issue(s)" in result.output


def test_doctor_vacuum_reports_and_shrinks_the_file(cli_db):
    """The --vacuum path must reclaim space and report an honest figure.

    Guards against the WAL/stat concern: techne runs journal_mode=WAL, so this
    asserts the main-file stat() actually drops after VACUUM (it does — VACUUM
    rewrites the main db file directly, not via the -wal sidecar).
    """
    import os

    # Grow the file with ~2.5 MB of distinct-triple rows, then delete them all so
    # the pages sit on the freelist (reclaimable but not yet returned to the OS).
    conn = sqlite3.connect(cli_db.db_path)
    conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES ('s1')")
    conn.executemany(
        "INSERT INTO thread_boundaries (session_id, obs_index_a, obs_index_b, "
        "gap_seconds, family_a, family_b, signals_json, total_score, is_boundary) "
        "VALUES ('s1', ?, ?, 60.0, 'fs', 'fs', ?, 0.0, 0)",
        [(i, i + 1, "x" * 500) for i in range(5000)],
    )
    conn.commit()
    conn.execute("DELETE FROM thread_boundaries")
    conn.commit()
    conn.close()

    before = os.path.getsize(cli_db.db_path)
    # --no-liveness: this test is about VACUUM reclaiming pages, and the seeded
    # store has activity but no hook markers, which liveness correctly fails on.
    # Scoping keeps the assertion about the thing under test (techne#468).
    result = CliRunner().invoke(main, ["doctor", "--vacuum", "--no-liveness"])
    assert result.exit_code == 0, result.output
    assert "VACUUM" in result.output and "reclaimed" in result.output
    after = os.path.getsize(cli_db.db_path)
    assert after < before, f"file did not shrink: {before} -> {after}"


# ---------------------------------------------------------------------------
# techne#370 — the MCP surface can be dead while everything else reads green
# ---------------------------------------------------------------------------


def _write_mcp(entry):
    """Write an mcpServers.techne entry verbatim, or clear the file if None."""
    import json as _json

    from techne.platforms.claude_code import hooks as hooks_mod

    mcp = hooks_mod.MCP_CONFIG_PATH
    mcp.parent.mkdir(parents=True, exist_ok=True)
    mcp.write_text(_json.dumps({"mcpServers": {"techne": entry}} if entry else {}))
    return mcp


def test_a_dead_mcp_interpreter_is_reported(cli_db, tmp_path):
    """THE BUG. A raw interpreter path that no longer exists — a reboot clearing
    a /tmp venv, a worktree cleanup, a pipx reinstall landing elsewhere.

    The observed failure was split-brain and silent: the SessionStart badge said
    `techne ✅ live` because the hooks self-heal, while `claude mcp list` said
    `✘ Failed to connect` and no mcp__techne__* tool existed for the whole
    session. Nothing reported it.
    """
    _install_launcher()
    _write_settings("{}")
    _write_mcp({"command": "/private/tmp/gone/.venv/bin/python", "args": ["-m", "techne.mcp_server"]})

    result = CliRunner().invoke(main, ["doctor"])
    assert "mcp:registration" in result.output
    assert "/private/tmp/gone" in result.output, result.output
    assert result.exit_code == 1


def test_a_launcher_backed_mcp_entry_is_healthy(cli_db):
    """The self-healing form must read clean — otherwise the fix reports itself
    as the bug and the operator learns to ignore the check."""
    launcher = _install_launcher()
    _write_settings("{}")
    _write_mcp({"command": "/bin/sh", "args": [str(launcher), "-m", "techne.mcp_server"]})

    out = CliRunner().invoke(main, ["doctor"]).output
    assert "mcp:registration" in out
    assert "launch command resolves" in out, out


def test_a_missing_mcp_registration_is_not_silently_healthy(cli_db):
    """An absent MCP config read as HEALTHY in my first cut — the false green
    this check exists to remove, reintroduced inside the fix for it."""
    _install_launcher()
    _write_settings("{}")
    from techne.platforms.claude_code import hooks as hooks_mod

    hooks_mod.MCP_CONFIG_PATH.unlink(missing_ok=True)

    out = CliRunner().invoke(main, ["doctor"]).output
    assert "not registered as an MCP server" in out, out


def test_an_uninitialised_machine_is_not_told_its_mcp_is_broken(cli_db):
    """No launcher means techne never installed here. Reporting a broken MCP
    there is a red the operator cannot act on — the mistake #469 review 6 caught
    in the drift block, which must not be repeated one check over."""
    from techne.platforms.claude_code import hooks as hooks_mod

    hooks_mod.MCP_CONFIG_PATH.unlink(missing_ok=True)

    out = CliRunner().invoke(main, ["doctor"]).output
    assert "mcp:registration" not in out, out
