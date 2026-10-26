"""`runtime:loaded` — deployed is not loaded (techne#491).

WHAT IS STUBBED AND WHY. The concept these assertions turn on is the ORDERING
comparison — did a long-lived process start before the deployed revision was
committed — and the mapping from that ordering to a verdict. So the two INPUTS
are arranged (the process list, the deploy timestamp) and the comparison is never
stubbed. `_running_techne_processes` is exercised for real in
:class:`TestProcessTableParsing`, against fixed `ps` output rather than the
host's, so the parser is covered without the suite depending on what happens to
be running on the machine. (The stub rule from #490 round 2: a stub must not
redefine the concept the assertion turns on.)
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import time

import pytest

from techne.config import Config
from techne.core import checks as checks_mod
from techne.core.checks import Verdict, check_runtime_loaded
from techne.core.checks import _running_techne_processes as _real_scan

# Bound at IMPORT time, deliberately. conftest's autouse `_isolate_process_table`
# replaces the module attribute so the rest of the suite cannot read the host's
# process list; this reference was resolved before that patch and therefore still
# points at the real parser — which is the thing this class exists to test.

# `ps -eo pid=,lstart=,args=` output. Note the DOUBLE space before a single-digit
# day — that is what ps actually emits and it is exactly what a naive split-on-
# one-space parser would break on.
_PS_MACOS = (
    "26356 Thu Aug  6 15:41:42 2026 /usr/local/bin/python -m techne.mcp_server\n"
    "64447 Thu Aug  6 10:53:29 2026 /usr/local/bin/python -m techne.mcp_server\n"
    "  501 Thu Aug  6 09:00:00 2026 /usr/sbin/notifyd\n"
)
_PS_LINUX = (
    "  912 Wed Aug  5 08:15:00 2026 /usr/bin/python3 -m techne.mcp_server\n"
    " 1044 Wed Aug  5 08:15:01 2026 /bin/sh -c something-else\n"
)


# `time.tzset()` is a Unix capability, not a guaranteed one: uv's managed
# python-build-standalone interpreters ship WITHOUT it, so these tests raise
# AttributeError on `./scripts/ci-local.sh 3.13` while passing in CI
# (techne#773). Skip rather than fail -- "this build cannot run the test" is not
# "the code is broken", and a red that CI never reproduces teaches you to ignore
# the local runner.
#
# WHAT KEEPS THIS FROM FIRING IN CI -- name the right mechanism, because this is
# the fact to re-check if the guard ever goes wrong. It is NOT actions/setup-python:
# test.yml's header declines to use it at all (see its `NO actions/setup-python`
# note), and the one job that does (`fork-pr`,
# test.yml:521/536, ubuntu-latest) has never been observed to run -- no fork of
# this repo exists, and it reports `skipping` on every PR. The `test` job is
# `runs-on: [self-hosted, linux, x64, techne-ci]` and builds its venv
# with `uv venv --python ${{ matrix.python-version }}` (the `Create venv` step) against
# interpreters BAKED INTO THE RUNNER IMAGE, under `UV_PYTHON_PREFERENCE: only-system`
# and `UV_PYTHON_DOWNLOADS: never` (test.yml:79-80). That second pin is precisely
# what stops uv from substituting a managed standalone build there the way it did
# here. So the dependency is the runner image plus those two pins -- rebuilt on a
# different cadence by a different process than anything in this repo.
#
# Established by log, not by inference: CI runs `pytest -q -rs -n 4`, which
# enumerates every skip by reason. Job 98062971053 on PR #748 reports
# `3613 passed, 6 skipped` with all six named (LSP x2, model2vec x3,
# real-transcripts x1) -- neither of these tests among them. At that head both
# called `time.tzset()` unguarded, so a PASS entails the attribute resolved.
requires_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"),
    reason="time.tzset() absent on this interpreter (python-build-standalone)",
)


def _epoch(stamp: str) -> float:
    """Local-time interpretation, matching what `ps` emits and mktime consumes."""
    return time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))


def _fake_ps(monkeypatch, stdout: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["ps", "-eo"]:
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)


@pytest.fixture
def local_tz():
    """Set the process timezone for one test and put it back afterwards.

    NOT a sibling "restore" test, which is what this replaced. `tzset()` mutates
    process-global C state, and under `pytest -n 4` the restoring test can land
    on a different xdist worker than the mutating one — so it was structurally
    incapable of doing its job, on top of protecting nothing measurable
    (deselecting it left the suite green). A mutation cleans up after itself.
    Deliberately not routed through `monkeypatch.setenv`: fixture teardown is
    LIFO, so this fixture unwinds BEFORE monkeypatch would restore TZ, and the
    `tzset()` would then read the still-patched value.
    """
    original = os.environ.get("TZ")

    def _set(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    yield _set
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def _arrange(monkeypatch, procs, deployed_at, how="landed here"):
    monkeypatch.setattr(checks_mod, "_running_techne_processes", lambda: procs)
    monkeypatch.setattr(
        checks_mod,
        "_runtime_deployed_at",
        lambda _r: None if deployed_at is None else (deployed_at, how),
    )


class TestProcessTableParsing:
    def test_parses_real_ps_output_and_ignores_everything_else(self, monkeypatch):
        _fake_ps(monkeypatch, _PS_MACOS)

        procs = _real_scan()

        assert [p for p, _s, _c in procs] == [26356, 64447]
        assert procs[0][1] == _epoch("Thu Aug  6 15:41:42 2026")
        assert "techne.mcp_server" in procs[0][2]

    def test_a_single_digit_day_does_not_break_the_split(self, monkeypatch):
        """`ps` pads to a fixed width, so "Aug  6" carries two spaces."""
        _fake_ps(monkeypatch, _PS_LINUX)

        procs = _real_scan()

        assert [p for p, _s, _c in procs] == [912]

    def test_an_unreadable_process_table_is_None_not_empty(self, monkeypatch):
        """None ("could not look") and [] ("looked, nothing there") are different
        facts and map to different verdicts. Collapsing them would report a
        failed probe as a clean bill of health — the founding bug of this
        module."""
        _fake_ps(monkeypatch, "", returncode=1)

        assert _real_scan() is None

    def test_a_malformed_line_is_skipped_not_fatal(self, monkeypatch):
        """This runs inside `techne doctor`; one weird row must not take the
        whole check down."""
        _fake_ps(monkeypatch, "notapid Thu Aug  6 15:41:42 2026 -m techne.mcp_server\n" + _PS_MACOS)

        procs = _real_scan()

        assert [p for p, _s, _c in procs] == [26356, 64447]


class TestABrokenParserNeverReportsAllClear:
    """Finding 2 on PR #510: "some rows failed" and "every row failed" are
    different facts, and conflating them emitted an affirmative all-clear from a
    probe that had stopped working."""

    def test_matching_rows_that_all_fail_to_parse_are_a_failed_probe(self, monkeypatch):
        _fake_ps(monkeypatch, "pid=912 started=whenever -m techne.mcp_server\n")

        assert _real_scan() is None

    def test_and_the_check_calls_that_unknown_not_dormant(self, monkeypatch):
        """The failure it prevents, end to end: DORMANT here reads "nothing can
        hold stale modules" with ok=True — in the exact situation the check
        exists to catch."""
        monkeypatch.setattr(checks_mod, "_running_techne_processes", lambda: None)
        monkeypatch.setattr(checks_mod, "_runtime_deployed_at", lambda _r: (1_700_000_000, "x"))

        r = check_runtime_loaded(Config())

        assert r.verdict is Verdict.UNKNOWN
        assert r.ok is False

    def test_a_genuinely_empty_machine_is_still_dormant(self, monkeypatch):
        """The fix must not turn "nothing running" into a failure."""
        _fake_ps(monkeypatch, "  501 Thu Aug  6 09:00:00 2026 /usr/sbin/notifyd\n")

        assert _real_scan() == []


class TestItMeasuresTheDeploymentNotItsOwnCheckout:
    """Finding 3 on PR #510, reproduced live: running from a review worktree made
    `deployed_at` that worktree's HEAD — a commit existing nowhere else — and a
    server that started after it was vouched for against a revision it could not
    have loaded. The #473 configuration turning the new check green."""

    def test_prefers_the_deployed_runtime_over_the_importing_checkout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "runtime" / ".git").mkdir(parents=True)

        assert checks_mod._deployment_checkout() == tmp_path / "runtime"

    def test_falls_back_to_the_importing_checkout_when_no_runtime_is_deployed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))

        import techne

        expected = pathlib.Path(techne.__file__).resolve().parent.parent
        assert checks_mod._deployment_checkout() == expected

    def test_landing_time_beats_committer_time(self, tmp_path):
        """A commit lands upstream at 22:02, the operator pulls at 23:00, a server
        starts at 22:30: it loaded pre-pull code but started after the commit, and
        committer time reports it LIVE. The reflog mtime is when the ref moved HERE."""
        logs = tmp_path / ".git" / "logs"
        logs.mkdir(parents=True)
        (logs / "HEAD").write_text("ref moved\n")
        os.utime(logs / "HEAD", (1_786_100_000, 1_786_100_000))

        stamp, how = checks_mod._runtime_deployed_at(tmp_path)

        assert stamp == 1_786_100_000
        assert how == "landed here"

    def test_every_verdict_names_the_checkout_it_measured(self, monkeypatch):
        """So the number is falsifiable by whoever reads it, rather than trusted.

        Asserts the PATH THAT WAS MEASURED appears, not the substring "runtime" —
        the first version of this checked for that word and failed on a worktree
        whose path simply doesn't contain it, which would have been a test of the
        directory name rather than of the property.
        """
        _arrange(monkeypatch, [(1, 1.0, "python -m techne.mcp_server")], 1_700_000_000)

        evidence = check_runtime_loaded(Config()).evidence

        assert str(checks_mod._deployment_checkout()) in evidence
        assert "landed here 1700000000" in evidence


class TestVerdicts:
    def test_a_process_older_than_the_deploy_is_stale_with_a_restart_remedy(self, monkeypatch):
        deployed = _epoch("Thu Aug  6 22:02:08 2026")
        _arrange(
            monkeypatch,
            [(26356, _epoch("Thu Aug  6 15:41:42 2026"), "python -m techne.mcp_server")],
            int(deployed),
        )

        r = check_runtime_loaded(Config())

        assert r.verdict is Verdict.STALE
        assert "restart" in r.remedy
        assert "26356" in r.evidence

    def test_the_detail_reports_the_ordering_and_refuses_to_claim_a_tear(self, monkeypatch):
        """THE LOAD-BEARING WORDING. A runtime pull under a live server was
        measured NOT tearing it (2026-08-06: reflect_emit then ran clean on that
        process, 5 threads, 15 candidates). So the ordering over-reports, and the
        detail must say what was observed, not what it implies. If this check
        ever claims "torn", it will cry wolf on every routine deploy and stop
        being read."""
        deployed = int(_epoch("Thu Aug  6 22:02:08 2026"))
        _arrange(monkeypatch, [(1, deployed - 600, "python -m techne.mcp_server")], deployed)

        detail = check_runtime_loaded(Config()).detail.lower()

        assert "predates" in detail
        assert "not a tear" in detail
        # Ban the CLAIM, not the word. The detail says "whether any is actually
        # torn depends on which modules it has yet to import", which is the
        # disclaimer this check exists to make — an earlier version of this
        # assertion banned the substring and failed on the hedge itself.
        assert "is torn" not in detail
        assert "runtime is torn" not in detail

    def test_processes_started_after_the_deploy_are_live(self, monkeypatch):
        deployed = int(_epoch("Thu Aug  6 22:02:08 2026"))
        _arrange(monkeypatch, [(1, deployed + 60, "python -m techne.mcp_server")], deployed)

        r = check_runtime_loaded(Config())

        assert r.verdict is Verdict.LIVE
        assert r.remedy is None

    @requires_tzset
    def test_local_time_is_not_read_as_utc(self, monkeypatch, local_tz):
        """Regression pin for the tz footgun named in `_running_techne_processes`.

        THE PREVIOUS VERSION OF THIS TEST WAS HOLLOW IN THE WORST WAY: it went
        through `_arrange`, which stubs the process scan wholesale and hands the
        check a float, so `strptime`/`mktime` were never called at all —
        instrumented at 0 calls. It passed under ANY implementation, in any
        timezone. Worse, the suite as a whole went green under `TZ=UTC` with
        `calendar.timegm` substituted, and CI containers default to UTC, so the
        footgun was unguarded exactly where regressions are meant to be caught.
        (Prudence, PR #510 — she ran both substitutions to prove it.)

        So this goes through the REAL parser and PINS THE TIMEZONE rather than
        inheriting the host's, asserting a hardcoded epoch. Under `timegm` the
        parsed value would be the UTC reading and this fails by four hours.
        """
        local_tz("America/New_York")
        _fake_ps(monkeypatch, "  912 Thu Aug  6 15:41:42 2026 python -m techne.mcp_server\n")

        (_pid, start, _cmd) = _real_scan()[0]

        # 2026-08-06 15:41:42 in America/New_York (EDT, UTC-4) == 19:41:42Z.
        assert start == 1786045302.0
        assert start != 1786030902.0  # the timegm misreading, four hours off (EDT is UTC-4)

    def test_mixed_fleet_counts_only_the_old_ones(self, monkeypatch):
        deployed = int(_epoch("Thu Aug  6 22:02:08 2026"))
        _arrange(
            monkeypatch,
            [
                (1, deployed - 600, "python -m techne.mcp_server"),
                (2, deployed + 600, "python -m techne.mcp_server"),
            ],
            deployed,
        )

        r = check_runtime_loaded(Config())

        assert r.verdict is Verdict.STALE
        assert "1 of 2" in r.detail

    def test_no_long_lived_process_is_dormant_and_says_why(self, monkeypatch):
        """Nothing can hold stale modules if nothing is long-lived. DORMANT ties
        with LIVE, so this stays out of the banner headline while remaining in
        doctor's table."""
        _arrange(monkeypatch, [], 1_700_000_000)

        r = check_runtime_loaded(Config())

        assert r.verdict is Verdict.DORMANT
        assert "no long-lived" in r.detail

    def test_an_unreadable_process_table_is_unknown_never_green(self, monkeypatch):
        _arrange(monkeypatch, None, 1_700_000_000)

        assert check_runtime_loaded(Config()).verdict is Verdict.UNKNOWN

    def test_a_packaged_install_is_dormant_not_red(self, monkeypatch):
        """No git checkout means no revision to compare, which is a legitimate
        healthy state — the same call its sibling `runtime:revision` makes."""
        _arrange(monkeypatch, [(1, 1.0, "python -m techne.mcp_server")], None)

        r = check_runtime_loaded(Config())

        assert r.verdict is Verdict.DORMANT
        assert "packaged install" in r.detail


class TestItStaysOutOfTheHotPath:
    def test_marked_expensive_so_the_sessionstart_banner_skips_it(self):
        """The marker is the mechanism, not the docstring.

        Its sibling `runtime:revision` shipped a docstring ASSERTING this
        exemption while `collect_health` ran the whole registry unfiltered —
        six git subprocesses against a 5s hook budget on a fail-open path that
        drops the entire banner on overrun (#487 review). This check shells out
        to `ps`, so it inherits the same hazard and must inherit the marker.
        """
        assert getattr(check_runtime_loaded, "expensive", False) is True

    def test_cheap_only_omits_it_and_a_full_run_includes_it(self, monkeypatch):
        """Pins the exemption end-to-end through `run_checks` rather than
        trusting the attribute alone."""
        _arrange(monkeypatch, [], 1_700_000_000)

        cheap = {r.name for r in checks_mod.run_checks(Config(), cheap_only=True)}
        full = {r.name for r in checks_mod.run_checks(Config())}

        assert "runtime:loaded" not in cheap
        assert "runtime:loaded" in full


class TestRegistration:
    def test_the_check_is_registered(self):
        """A check that exists but is not registered reports nothing, and an
        absent check reads as "nothing wrong"."""
        assert check_runtime_loaded in checks_mod.registered_checks()

    @pytest.mark.parametrize("pattern", ["techne.mcp_server", "techne watch"])
    def test_the_long_lived_set_names_the_processes_that_can_hold_modules(self, pattern):
        assert pattern in checks_mod._LONG_LIVED_PROCESSES


class TestTheFormatAssumptionAgainstThisHostsRealPs:
    def test_lstart_is_five_tokens_this_parser_understands(self):
        """THE ONE PREMISE NOTHING ELSE COVERS, unverified through two review
        rounds: every other test feeds fixed `ps` output, so no test had ever
        seen what a real `ps` emits. procps-ng's `pr_lstart` is `%24.24s` of
        `ctime()` and POSIX fixes that layout, but neither Prudence nor I could
        run a Linux `ps` to confirm it — docker was refused in her sandbox.

        This asserts the format against WHATEVER `ps` the suite runs on, so CI's
        Linux runner covers the case a macOS laptop cannot. Shape only, never
        which processes exist, so it is host-independent.
        """
        out = subprocess.run(["ps", "-eo", "pid=,lstart=,args="], capture_output=True, text=True)
        if out.returncode != 0:
            pytest.skip("ps unavailable on this host")
        rows = [ln.split() for ln in out.stdout.splitlines() if len(ln.split()) >= 7]
        assert rows, "ps produced no parseable rows at all"

        pid, lstart = rows[0][0], " ".join(rows[0][1:6])

        assert pid.isdigit()
        time.strptime(lstart, "%a %b %d %H:%M:%S %Y")


class TestNoPsFallsBackToProc:
    """techne#1318: Debian slim and other minimal hosts ship without `ps`, and
    `runtime:loaded` read that as a failed probe (UNKNOWN), so doctor exited 1 on
    a fresh install. A missing `ps` is not a failed read; /proc holds the table."""

    @staticmethod
    def _fake_proc(
        root: pathlib.Path, pid: int, cmdline: bytes, stat_tail: str, btime=1_700_000_000
    ):
        root.mkdir(exist_ok=True)
        (root / "stat").write_text(f"cpu  1 2 3\nbtime {btime}\nprocesses 9\n")
        d = root / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(cmdline)
        (d / "stat").write_text(f"{pid} {stat_tail}\n")

    def test_reads_start_time_after_the_last_paren(self, tmp_path):
        """Field 2 is the command name in parens and may itself hold ") ", so
        fields are counted after the LAST ")"; starttime is field 22."""
        hz = os.sysconf("SC_CLK_TCK")
        after = " ".join(["S"] + ["0"] * 18 + [str(500 * hz)] + ["0"] * 5)
        self._fake_proc(tmp_path, 912, b"python3\0-m\0techne.mcp_server\0", f"(py) x) {after}")
        (tmp_path / "self").mkdir()  # non-numeric entries are skipped

        procs = checks_mod._proc_table_processes(tmp_path)

        assert procs == [(912, 1_700_000_500.0, "python3 -m techne.mcp_server")]

    def test_non_techne_processes_are_ignored(self, tmp_path):
        self._fake_proc(tmp_path, 1, b"/sbin/init\0", "(init) S 0")

        assert checks_mod._proc_table_processes(tmp_path) == []

    def test_matched_rows_that_all_fail_to_parse_are_None(self, tmp_path):
        """The same rule as the `ps` parser: a broken probe is not an empty machine."""
        self._fake_proc(tmp_path, 912, b"python3\0-m\0techne.mcp_server\0", "(py) S 0")

        assert checks_mod._proc_table_processes(tmp_path) is None

    def test_missing_ps_reads_proc(self, monkeypatch):
        def no_ps(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(subprocess, "run", no_ps)
        monkeypatch.setattr(checks_mod, "_proc_table_processes", lambda: [])

        assert _real_scan() == []

    def test_a_ps_that_exists_and_fails_is_still_unknown(self, monkeypatch):
        def broken_ps(cmd, **kwargs):
            raise PermissionError(cmd[0])

        monkeypatch.setattr(subprocess, "run", broken_ps)

        assert _real_scan() is None

    def test_no_ps_and_no_proc_is_dormant_with_the_reason(self, monkeypatch, tmp_path):
        missing = tmp_path / "no-proc"
        real = checks_mod._proc_table_processes
        monkeypatch.setattr(checks_mod, "_running_techne_processes", lambda: real(missing))
        monkeypatch.setattr(checks_mod, "_runtime_deployed_at", lambda _r: (1_700_000_000, "x"))

        r = check_runtime_loaded(Config())

        assert r.verdict is Verdict.DORMANT
        assert r.ok
        assert "ps is not installed" in r.detail
