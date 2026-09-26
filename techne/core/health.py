"""Health summary — the single source of truth for "is techne running & current?".

Both the MCP ``techne_status`` tool and the SessionStart login banner (#76)
build from :func:`collect_health`, so the two surfaces can never drift in what
they report. :func:`format_banner` renders the terse operator-facing banner.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from techne import __version__, _is_installed_path
from techne.config import Config
from techne.core.repo import git_dirs as _git_dirs
from techne.core.repo import read_ref as _read_ref

_HOOK_LAST_RUN_KEY = "hook:session_start:last_run"
_HOOK_LAST_ERROR_KEY = "hook:session_start:last_error"


def _parse_ts(ts: str):
    """Parse a SQLite/ISO timestamp string to an aware UTC datetime, or None.

    SQLite stores timestamps as strings — either ``CURRENT_TIMESTAMP`` form or
    ISO-8601. Try the common shapes without pulling in dateutil.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _installed_revision() -> str | None:
    """The commit an INSTALLED build was built from, via PEP 610 (techne#965).

    `pip install "techne @ git+https://...@<sha>"` writes `direct_url.json` into
    the dist-info with `vcs_info.commit_id` -- the resolved sha, not merely the
    ref that was asked for. Verified against a real install:

        {"url": "...", "vcs_info": {"commit_id": "21813c16ed8be...",
         "requested_revision": "21813c1...", "vcs": "git"}}

    WHY THIS MATTERS AND WHY THE GIT PATH CANNOT COVER IT. An installed build has
    no checkout to read, so `_revision_info` reported `revision: None` and
    `--version` named the last RELEASE. Inside the team we work around that by
    reading git in the runtime checkout; a consumer who installed from a sha --
    which is what we tell external consumers to do -- has no equivalent, and Grip
    had to identify their build by probing for features instead. That is the same
    complaint as techne#825 seen from outside. (An earlier draft also cited
    techne#1100, copied from issue #965's body; that issue number does not exist
    in this repo and the intended one was not guessed at.)

    Returns None for a wheel/sdist/PyPI install (no vcs_info), for an editable
    install (dir_info instead), and when the metadata is absent or malformed --
    all of which are "genuinely unknowable", not failures.
    """
    try:
        import json
        from importlib.metadata import PackageNotFoundError, distribution

        raw = distribution("techne").read_text("direct_url.json")
        if not raw:
            return None
        commit = (json.loads(raw).get("vcs_info") or {}).get("commit_id")
        # VALIDATED, NOT JUST TRUNCATED. `str(commit)[:7]` printed whatever was
        # there: a `commit_id` of "main" came back as "main" and an integer as
        # "1234567", either of which reads like a sha at `--version`. pip writes a
        # hash here per PEP 610, so a non-hex value means the metadata is not what
        # we think it is -- report unknown rather than dress it up.
        if not isinstance(commit, str):
            return None
        commit = commit.strip()
        if len(commit) < 7 or any(c not in "0123456789abcdefABCDEF" for c in commit):
            return None
        return commit[:7]
    except (PackageNotFoundError, OSError, ValueError, KeyError, TypeError):
        return None
    except Exception:  # noqa: BLE001 — metadata probing must never break a health read
        return None


def _package_dir() -> Path:
    """The RESOLVED directory of the imported ``techne`` package.

    Every build-identity surface starts here (techne#982). ``--version`` used to
    pass ``Path(__file__).parent`` unresolved, so a symlinked site-packages read
    as a checkout there and as installed everywhere else.
    """
    import techne

    return Path(techne.__file__).resolve().parent


def build_identity(version: str | None = None) -> str:
    """Version plus build identity — ``--version`` and the MCP handshake (techne#965/#982).

    ONE derivation for both, over the same resolved path ``collect_health`` uses,
    so ``techne --version``, ``serverInfo.version``, ``techne_status`` and the
    SessionStart banner cannot name different builds. The MCP handshake reported
    a bare ``__version__`` after #971 fixed the other three, and ``serverInfo`` is
    what an MCP client displays.

    ``0.16.1+21813c1 (installed)`` borrows PEP 440's LOCAL LABEL shape, but the
    string is NOT a PEP 440 version and must not be parsed: the `` (checkout)`` /
    `` (installed)`` suffix is deliberate, a human-facing build word. A version
    that already carries a local label (``0.0.0+unknown``) takes the revision
    after a space rather than a second ``+``. Never raises.
    """
    version = __version__ if version is None else version
    try:
        info = _revision_info(_package_dir())
        revision, build = info.get("revision"), info.get("build")
    except Exception:  # noqa: BLE001 — an identity line must never fail
        revision, build = None, None
    if revision:
        sep = " " if "+" in version else "+"
        return f"{version}{sep}{revision} ({build})"
    return f"{version} ({build})" if build else version


def _reflog_last_moved(reflog: Path) -> float | None:
    """Unix time of the LAST entry in a git reflog file, or None (techne#571).

    A line is ``<old> <new> <name> <email> <epoch> <tz>\t<message>``; the name
    may contain spaces, so the epoch is read from the end of the part before the
    tab. Absent, empty or malformed means None — never a guess.
    """
    try:
        lines = reflog.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        fields = line.split("\t", 1)[0].split()
        if len(fields) >= 2 and fields[-2].isdigit():
            return float(fields[-2])
        return None
    return None


def _revision_info(
    package_path: Path,
    installed_revision: "Callable[[], str | None] | None" = None,
) -> dict:
    """Identify the code that is actually imported, not the version it claims.

    ``__version__`` comes from ``importlib.metadata`` FOR AN INSTALLED BUILD
    ONLY. It used to come from there always, and for an *editable* install that
    stamp is written once at ``pip install -e`` time and never moves when the
    checkout does — measured four days apart on this machine (#568), and again
    at the 2026-08-28 install (#825). ``techne._resolve_version`` now reads a
    checkout's version from its own ``pyproject.toml`` instead, using THIS
    function's build test so the two cannot disagree; on a checkout it never
    consults the dist-info, and reports ``0.0.0+unknown`` when pyproject is
    unreadable rather than relapsing to it.

    THE REVISION IS STILL THE FINER IDENTIFIER, for a different reason than the
    one this docstring used to give. The version is no longer stale — it is
    COARSE: every commit between two releases declares the same version, so the
    revision is what distinguishes them. "The version string is only good for a
    real install" was the old rationale and is now false; do not restore it.

    Returns ``build`` plus, for a checkout, ``revision`` / ``remote_revision`` /
    ``branch`` / ``drifted`` / ``remote_ref_age_days``. ``drifted`` is None when
    currency could not be established — "I could not check" is reported as itself
    rather than collapsing into "current".

    SIBLING: :func:`techne.core.checks.check_runtime_revision` answers the same
    question for ``techne doctor`` using git subprocesses, and is marked
    ``@expensive`` precisely so the SessionStart banner does NOT pay for it — six
    subprocesses against a 5s hook budget. That exemption is why the banner had
    no revision signal at all and drift ran unnoticed for six days (#568). This
    is the cheap file-read substrate for the same question, and it deliberately
    mirrors that check's hard-won semantics rather than inventing new ones: only
    ``main`` counts as a deployment, and the freshness caveat goes on every
    branch. Keep the two in agreement — a user-visible surface that contradicts
    ``doctor`` is worse than one that stays quiet.
    """
    info: dict = {
        "build": "installed" if _is_installed_path(package_path) else "checkout",
        "revision": None,
        "remote_revision": None,
        "drifted": None,
        "remote_ref_age_days": None,
        "branch": None,
    }
    if info["build"] == "installed":
        # drifted stays None, NOT False. The banner warns here ("may lag repo
        # HEAD") because currency cannot be checked locally without a reference
        # version — so the payload must say "unknown" too. False would claim we
        # checked and it is level, which is the opposite of what the banner says
        # and would let a consumer of the payload draw the reassuring conclusion
        # the banner refuses to draw.
        #
        # THE REVISION, HOWEVER, IS KNOWABLE FOR A VCS INSTALL (techne#965), and
        # supplying it does NOT weaken the paragraph above. `drifted` stays None:
        # naming the commit you were built from says nothing about whether a newer
        # one exists, which is exactly the distinction this branch already draws.
        # INJECTABLE, TO KEEP THIS A PURE FUNCTION OF ITS ARGUMENTS (Prudence, #971).
        # `_installed_revision()` ignores package_path and reads the REAL installed
        # distribution, so the moment this branch started calling it, a caller that
        # constructs a synthetic path no longer controlled the answer. That seam was
        # crossed quietly and exactly one test witnessed it --
        # test_site_packages_is_installed_and_reads_no_refs passed only because this
        # venv is an EDITABLE install; against a VCS-installed environment (the one
        # this change exists to serve) it returns a real sha and the test fails.
        #
        # Mocking it at each call site would have worked; restoring the purity is
        # better, because the next caller gets it for free.
        resolve = _installed_revision if installed_revision is None else installed_revision
        info["revision"] = resolve()
        return info

    root = package_path.parent
    git_dir, common_dir = _git_dirs(root)
    if git_dir is None or common_dir is None:
        # A source tree that is not a checkout (vendored, extracted sdist): the
        # revision is genuinely unknowable. Leave drifted=None.
        return info

    # Which branch HEAD names decides whether "behind main" is even a defect.
    # A feature branch, a PR merge-ref or a detached CI checkout is SUPPOSED to
    # differ from main; warning there fires on every branch and every PR, which
    # is a relative check with no absolute floor — it always has a "worst" item
    # and so trains you to ignore it. Only a checkout that is itself ON main is
    # claiming to track main, and only that claim can be broken. The deployed
    # runtime is exactly that case (#473 pins it to main); dev worktrees are not.
    try:
        raw_head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        raw_head = ""
    # A DETACHED HEAD IS OFF-MAIN, NOT UNREADABLE. When HEAD holds a raw sha it
    # read perfectly well — it just names no branch — so it must take the
    # off-main path, not UNVERIFIED. Recorded as the literal "HEAD" to match
    # check_runtime_revision's convention (`git rev-parse --abbrev-ref HEAD`
    # returns "HEAD" when detached), because that check reports DORMANT/healthy
    # for this state and the two surfaces must not contradict. Every review
    # worktree is detached, so warning here fires on exactly the topology the
    # review tooling runs in — the "trains you to ignore it" failure this whole
    # change exists to prevent. `branch` stays None ONLY when HEAD is genuinely
    # unreadable, which is the only state that deserves UNVERIFIED.
    if raw_head.startswith("ref: "):
        info["branch"] = raw_head[5:].strip()
    elif raw_head:
        info["branch"] = "HEAD"
    tracks_main = info["branch"] == "refs/heads/main"

    head = _read_ref(git_dir, common_dir, "HEAD")
    remote = _read_ref(git_dir, common_dir, "refs/remotes/origin/main")
    info["revision"] = head[:7] if head else None
    info["remote_revision"] = remote[:7] if remote else None

    # The age is computed for EVERY checkout, on or off main. An earlier revision
    # returned before this point when off main, so the caveat this module insists
    # goes "on every branch" was absent from exactly the branches the gate
    # diverted — and docs/arch/liveness.md records the invariant that a working
    # checkout still STATES how far behind it is, because "on a peer's branch,
    # behind the merge" IS the #473 incident.
    # WHEN origin/main LAST MOVED, read from its REFLOG first (techne#571). The
    # last line of `logs/refs/remotes/origin/main` carries the unix time of the
    # last move — the very entry check_runtime_revision reads with `git log -g
    # -1`, so the banner and doctor agree by construction, not by measurement.
    # It also survives `git pack-refs --all --prune`, which removes the loose ref
    # and used to turn the age into "unknown" after a routine gc.
    #
    # Then the loose ref's mtime (the same quantity, when a reflog is disabled or
    # expired), and NEVER FETCH_HEAD: that is the last FETCH, and a fetch that
    # moves nothing still rewrites it (15:05:36 vs 14:37:01 in one repo, measured),
    # so under this label it reads YOUNGER than the truth — the reassuring
    # direction, wrong for a staleness caveat. With neither source there is no
    # honest age: it stays None and the banner says so.
    moved = _reflog_last_moved(common_dir / "logs" / "refs" / "remotes" / "origin" / "main")
    if moved is None:
        try:
            moved = (common_dir / "refs" / "remotes" / "origin" / "main").stat().st_mtime
        except OSError:
            moved = None
    if moved is not None:
        try:
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(moved, tz=timezone.utc)
            info["remote_ref_age_days"] = max(age.days, 0)
        except (OverflowError, OSError, ValueError):
            pass  # an epoch no clock can hold is no honest age either

    if not tracks_main:
        # Deliberately off main, so being unlevel with main is not a defect — but
        # `drifted` stays None, NOT False. False would assert we checked and it is
        # level, which is affirmatively wrong whenever the shas differ, and this
        # payload also feeds the MCP `techne_status` surface where a consumer
        # would read it as the reassurance the banner declines to give. Same rule
        # this function already applies to `installed`.
        return info
    if head and remote:
        info["drifted"] = head != remote
    return info


def collect_health(config: Config) -> dict:
    """Assemble the full health/status payload.

    Always returns a dict. When the DB can't be opened the payload carries
    ``db_ok: False`` and an ``error`` string instead of raising — the banner
    turns that into the alert rather than letting the hook die silently.

    New fields added by techne#95:
      hook_last_run  — ISO timestamp of last session_start hook run (or None)
      hook_last_error — {ts, stage, msg} of last hook failure (or None)
      last_capture   — ISO timestamp of the newest observation (event-time), or None
      stale          — True when capture has gone quiet longer than staleness_hours
      capture_alive  — True when capture is current (last observation within staleness_hours)

    Staleness keys on the last *observation*, NOT the last session row. A single
    long-lived or repeatedly-resumed session (common over tmux/remote) inserts
    one session row but keeps capturing, so MAX(started_at) would falsely read
    as a multi-day gap while capture is in fact healthy.
    """
    from techne.core.state import db_stats

    package_path = str(_package_dir())
    health: dict = {
        "version": __version__,
        "package_path": package_path,
        "db_path": str(config.db_path),
        "sync_adapter": config.sync.adapter,
        # build/revision/drift (#568). The old two-state heuristic read
        # "not site-packages" as "live", meaning tracks-HEAD-so-it-is-fresh.
        # The #473 runtime split broke that: ~/.techne/runtime is an editable
        # checkout that advances ONLY on a manual `git pull`, so it rendered
        # `live` — i.e. healthy — while 23 commits and 6 days behind main.
        # Now the revision is read rather than inferred; see _revision_info.
        **_revision_info(Path(package_path)),
        # Hook health fields (techne#95) — populated below when DB is reachable.
        "hook_last_run": None,
        "hook_last_error": None,
        "last_capture": None,
        "stale": False,
        "capture_alive": False,
    }

    try:
        health.update(db_stats(config.db_path))
        health["db_ok"] = True
    except Exception as e:  # noqa: BLE001 — banner must survive any DB failure
        health["db_ok"] = False
        health["error"] = f"{type(e).__name__}: {e}"
        return health

    # Hook health: read from extension_state (techne#95).
    try:
        from techne.core.database import connect
        from techne.core.extension_state import get_state

        conn = connect(config.db_path)
        try:
            last_run_payload = get_state(conn, _HOOK_LAST_RUN_KEY)
            last_error_payload = get_state(conn, _HOOK_LAST_ERROR_KEY)
        finally:
            conn.close()

        health["hook_last_run"] = last_run_payload.get("ts") if last_run_payload else None
        health["hook_last_error"] = last_error_payload or None
    except Exception:  # noqa: BLE001
        pass  # Non-fatal: extension_state unreadable degrades gracefully

    # last_capture: newest observation event-time — the true "is capture alive?"
    # signal. Keyed on observations, NOT session rows (see collect_health docstring).
    try:
        from techne.core.database import connect as _connect

        _conn = _connect(config.db_path)
        try:
            row = _conn.execute(
                "SELECT MAX(COALESCE(event_at, timestamp)) FROM observations"
            ).fetchone()
            health["last_capture"] = row[0] if row and row[0] else None
        finally:
            _conn.close()
    except Exception:  # noqa: BLE001
        health["last_capture"] = None

    # Staleness alarm: capture has gone quiet longer than staleness_hours.
    # capture_alive is its inverse — current capture, within the window.
    last_capture = health.get("last_capture")
    if last_capture:
        last_dt = _parse_ts(last_capture)
        if last_dt is not None:
            age = datetime.now(tz=timezone.utc) - last_dt
            health["stale"] = age > timedelta(hours=config.staleness_hours)
            health["capture_alive"] = not health["stale"]
    else:
        # No observations at all — brand-new install; don't alarm as stale,
        # but capture is not yet proven alive either.
        health["stale"] = False
        health["capture_alive"] = False

    # Vector-channel health (techne#358). A missing embedding backend silently
    # downgrades recall to FTS-only (search_vec returns []), and a backend/model
    # swap leaves the vec index stale — both with zero signal today. Surface it
    # so the banner can alarm. Reuses vector_channel_status (the same non-mutating
    # source `techne vectors status` uses) so the two never drift. Never fatal.
    try:
        from techne.core.vector_status import vector_channel_status

        # probe_backend_when_empty=False: this runs in the session-start hook, so
        # on an install with no vectors we must not pay the embedding-backend build
        # (techne#361 review). The verdict there is "not degraded" regardless.
        vstatus = vector_channel_status(config.db_path, probe_backend_when_empty=False)
        backend = vstatus["active_backend"]
        any_vectors = any(t.get("exists") for t in vstatus["tables"].values())
        health["vectors"] = {
            "backend": (f"{backend['name']} (dim={backend['dim']})" if backend else None),
            "has_vectors": any_vectors,
            "reembed_needed": vstatus["reembed_needed"],
            # Degraded = stored vectors exist but no backend can use them (the
            # silent FTS-only trap), OR the index is stale after a backend swap.
            # A brand-new install with no backend AND no vectors is NOT degraded
            # (never opted into vectors) — so it doesn't false-alarm.
            "degraded": (backend is None and any_vectors) or bool(vstatus["reembed_needed"]),
        }
    except Exception:  # noqa: BLE001 — banner must survive any vector-status failure
        health["vectors"] = None

    # Subsystem liveness (techne#468). The point of putting this HERE rather than
    # only behind a command: check_hook_drift detected the dead rails correctly
    # from 2026-07-09 but was wired solely to `techne status`, a command last run
    # 17 days after it mattered (techne#452). A detector wired to a command nobody
    # runs is worth no more than no detector — so liveness rides the surface the
    # operator already sees on every session.
    # OUTSIDE the try below, and with its own guard, so the key EXISTS on every
    # path. `None` means "could not be determined" and is deliberately distinct
    # from `[]` ("nothing was deferred"): a consumer doing
    # `.get("checks_deferred", [])` on an ABSENT key reads "nothing deferred" at
    # the exact moment every check was skipped — this module's founding bug, in
    # the code written to remove it. Setting it inside the try covered
    # run_checks throwing but not the IMPORT throwing (PR #526 review).
    try:
        from techne.core.checks import deferred_check_names

        health["checks_deferred"] = deferred_check_names()
    except Exception:  # noqa: BLE001 — an unreachable registry is a result
        health["checks_deferred"] = None

    try:
        from techne.core.checks import run_checks, summarize, worst

        # cheap_only: this runs inside the SessionStart hook under
        # `hooks.timeout_s`, and that path is fail-open — one overrun drops the
        # WHOLE banner, silently. Checks marked @expensive (subprocesses, walks,
        # network) are left to `techne doctor`, which is explicitly invoked.
        results = run_checks(config, cheap_only=True)
        health["checks"] = [
            {
                "name": r.name,
                "subsystem": r.subsystem,
                "kind": r.kind.value,
                "verdict": r.verdict.value,
                "last_fired": r.last_fired,
                "detail": r.detail,
                "evidence": r.evidence,
                "remedy": r.remedy,
            }
            for r in results
        ]
        w = worst(results)
        health["checks_worst"] = w.value if w else None
        health["checks_summary"] = summarize(results)
    except Exception as e:  # noqa: BLE001
        # Even the liveness layer failing must not read as healthy — record it
        # rather than dropping the key, or absence would look like "all fine".
        health["checks"] = []
        health["checks_worst"] = "unknown"
        health["checks_summary"] = {"unknown": 1}
        health["checks_error"] = f"{type(e).__name__}: {e}"

    return health


def format_banner(health: dict) -> str:
    """Render the terse login banner. Warnings only when something is off."""
    if not health.get("db_ok", False):
        return (
            f"\n## techne ⚠️\n"
            f"> DB UNREACHABLE — {health['db_path']}: "
            f"{health.get('error', 'unknown error')}"
        )

    # Build/revision line (#568). The rule this replaces was
    # `healthy = build == "live"`, where "live" meant only "not site-packages".
    # A checkout that nobody pulls is exactly as stale as a lagging install, so
    # currency is now decided by the REVISION, never by the install kind — and a
    # revision we could not read reports as unverified rather than as fine.
    build = health.get("build", "checkout")
    drifted = health.get("drifted")
    rev = health.get("revision")
    # THE FRESHNESS CAVEAT GOES ON EVERY BRANCH, not just the stale one — ported
    # from check_runtime_revision, which learned it the hard way: "level with
    # origin/main" is the FALSE-GREEN case, because the comparison is against a
    # LOCAL ref that is only as fresh as the last fetch. A ref fetched three weeks
    # ago reads as level when main has moved. Note `age == 0` is falsy, so this
    # must test for None, not truthiness, or the caveat silently disappears on
    # the freshest case.
    #
    # "last moved", NOT "fetched", and the label is now exact rather than a
    # compromise. The source is origin/main's reflog, falling back to the loose
    # ref's mtime (see _revision_info — never FETCH_HEAD), and both are when
    # origin/main last CHANGED: the reflog is the very entry
    # check_runtime_revision reads (techne#571). "fetched" would be wrong for it —
    # a fetch that moves nothing rewrites FETCH_HEAD but not the ref (15:05:36 vs
    # 14:37:01 here, 28 minutes apart).
    _age = health.get("remote_ref_age_days")
    fetched = f", local ref last moved {_age}d ago" if _age is not None else ", ref age unknown"
    _remote = health.get("remote_revision")
    branch = health.get("branch")

    if build == "installed":
        # DEMOTED FROM A WARNING TO A STATEMENT (#575), reversing the #568/#569
        # decision to keep healthy=False here. That call was right for its own
        # scope — a PR widening warning coverage must not quietly narrow it — and
        # it deferred this to "the latest-release comparison that would make the
        # check real". Richard's call, 2026-08-18: techne is local-first and does
        # not phone home, so that comparison is not coming, and the ⚠️ would have
        # been permanent.
        #
        # A PERMANENT WARNING NOBODY CAN CLEAR IS NOT A WARNING, IT IS NOISE, and
        # it is not free: this banner's mark is shared with subsystem liveness, so
        # a pinned install rendered ⚠️ forever and every REAL fault below it
        # arrived pre-ignored. Reporting an UNKNOWABLE as unhealthy is a category
        # error — the install is not faulty, its currency is simply not a local
        # fact. #575's own words: "honest and useless".
        #
        # AND THE TWO SURFACES DISAGREED, which this docstring already forbids
        # ("keep the two in agreement — a user-visible surface that contradicts
        # doctor is worse than one that stays quiet"). `check_runtime_revision`
        # has always returned DORMANT for a packaged install, with the comment "a
        # legitimate, healthy state — not something to report red" (checks.py).
        # So `techne doctor` called this condition healthy while the banner
        # called it a fault, for the same install. This aligns the banner TO
        # doctor rather than inventing a third opinion.
        #
        # The honesty is kept where honesty belongs, in the TEXT: the line still
        # says currency is unchecked and names how to check it, and `drifted`
        # stays None in the payload (never False — we did not check, so no
        # consumer may conclude "level"). What changes is only that we stop
        # crying wolf about it.
        healthy = True
        # NAME THE COMMIT WHEN WE KNOW IT (techne#965). A PEP 610 VCS install
        # records the sha it was built from, and the banner is the surface an
        # external consumer actually reads -- the first cut of #965 claimed the
        # banner gained this identity and it had NOT, because this branch built
        # its note from a literal. Currency is still unchecked and the text still
        # says so; what changes is that "which build" is answerable.
        build_note = (
            f"installed build {rev} — currency not checked (techne does not phone "
            "home); compare against github.com/tenishai/techne/releases"
            if rev
            else (
                "installed build — currency not checked (techne does not phone "
                "home); compare against github.com/tenishai/techne/releases"
            )
        )
    elif branch and branch != "refs/heads/main":
        # Deliberately off main — a feature branch, worktree or CI checkout, which
        # is SUPPOSED to differ, so this stays healthy. But it STILL STATES the
        # comparison: docs/arch/liveness.md records that a working checkout must
        # say how far behind it is, because "on a peer's branch, behind the merge"
        # IS the #473 incident, and reporting it green with no number reproduced
        # that bug inside its own fix. Exact distance needs an object walk we
        # cannot do from file reads; the sha comparison is the cheap honest half.
        # Gated on `branch` BEFORE `drifted` so the data model does not need a
        # false `drifted=False` to route the renderer here.
        healthy = True
        short = "a detached HEAD" if branch == "HEAD" else branch.removeprefix("refs/heads/")
        # ACTUALLY COMPARE. An earlier revision said "differs from origin/main"
        # without ever comparing the shas, so a branch freshly cut from an
        # up-to-date main — before its first commit, which is exactly when a
        # SessionStart banner greets you — printed the same sha on both sides of
        # the word "differs". doctor says "level with origin/main" for this.
        #
        # OFF MAIN AND AHEAD IS WHERE THE TWO PART (techne#572). doctor picks its
        # wording on `behind` alone, so a feature branch with an unpushed commit
        # (behind=0, ahead=1) is "level with origin/main" there and "differs from
        # origin/main" here. A sha comparison cannot tell ahead from behind — the
        # same limit the DIVERGED branch below states for main-and-ahead — so this
        # says "differs" and does not guess a direction. Both lines stay green.
        if not _remote:
            # KNOWN, DELIBERATE DISAGREEMENT WITH doctor. A single-branch clone
            # (`git clone --branch feat --single-branch`) genuinely has no
            # origin/main, and check_runtime_revision returns UNKNOWN there —
            # its `upstream is None` guard sits BEFORE its branch gate. Off-main
            # is the deliberately-quiet path and the text states the absence
            # outright, so green is the right call for a one-line banner; the
            # disagreement is recorded here rather than left for the next reader
            # to discover as a contradiction.
            cmp = "no origin/main ref"
        elif rev == _remote:
            cmp = f"level with origin/main {_remote}"
        else:
            cmp = f"differs from origin/main {_remote}"
        build_note = f"checkout {rev} · on {short} (not a deployment; {cmp}{fetched})"
    elif drifted is False:
        healthy = True
        build_note = f"checkout {rev} · level with origin/main{fetched}"
    elif drifted is True:
        # NOT "behind" — a sha comparison cannot tell behind from ahead, and on
        # main-and-AHEAD (unpushed commits) `doctor` reports LIVE while a bare
        # `pull` would not clear this line. A remedy that cannot clear its own
        # finding is the never-green defect checks.py has already paid for twice,
        # so this names only what it can prove and prescribes a command that
        # resolves either direction.
        healthy = False
        build_note = (
            f"checkout {rev} · DIVERGED from origin/main {_remote}{fetched} — "
            f"merged ≠ deployed; check which way: "
            f"git -C {Path(health['package_path']).parent} status -sb"
        )
    else:
        # Includes the unreadable-HEAD case: without a branch this cannot tell a
        # deployment from a working checkout, so it must not assert either.
        healthy = False
        build_note = (
            f"checkout {rev or '?'} · revision UNVERIFIED (branch or origin/main unreadable)"
        )

    # The headline mark must also reflect SUBSYSTEM liveness (techne#468).
    # Otherwise the banner renders ✅ while listing a failing subsystem two lines
    # below — a green top-line over a red detail is the precise shape of the bug
    # this work exists to remove.
    checks_bad = (health.get("checks_worst") or "live") not in ("live", "dormant")
    mark = "✅" if (healthy and not checks_bad) else "⚠️"

    # techne owns its own heading line (renders bright/high-white in the
    # terminal markdown); the stats fold into a blockquote beneath it.
    # Lead with a blank line: emitted as the hook's top-level systemMessage,
    # an ATX heading at char 0 collapses against Claude Code's message chrome,
    # so the leading \n guarantees the heading breaks onto its own line.
    lines = [f"\n## techne {mark}"]

    last = health.get("last_session") or "—"
    lines.append(f"> v{health['version']} · {build_note}")
    lines.append(
        f"> {health['observations']} obs · {health['threads']} threads "
        f"({health['active_threads']} active) · {health['knowledge']} knowledge "
        f"· last session {last}"
    )

    # Per-category memory fill-state (techne#334): break the knowledge total into
    # its types (+ identity) so the agent sees WHERE memory accumulates and can
    # self-regulate what it writes, instead of an opaque store-level total. Terse:
    # canonical type order first, then any non-canonical types so the breakdown
    # always accounts for the full total; non-zero categories only. Note: identity
    # lives in its own table, appended as an extra category — so this line totals
    # knowledge + identity, i.e. slightly more than the `knowledge` count above.
    by_type = dict(health.get("knowledge_by_type") or {})
    if by_type or health.get("identity"):
        from techne.core.knowledge import VALID_TYPES

        ordered = [t for t in VALID_TYPES if by_type.get(t)]
        extras = sorted(t for t in by_type if t not in VALID_TYPES and by_type.get(t))
        cats = [(t, by_type[t]) for t in (*ordered, *extras)]
        if health.get("identity"):
            cats.append(("identity", health["identity"]))
        if cats:
            lines.append("> memory: " + " · ".join(f"{n} {t}" for t, n in cats))

    # Outbox: a noop adapter never drains by design, so a backlog there is
    # informational, not a warning. A real adapter with a backlog is a ⚠️.
    pending = health.get("pending_outbox", 0)
    if pending:
        adapter = health["sync_adapter"]
        obx_mark = "⚠️ " if adapter != "noop" else ""
        lines.append(f"> {obx_mark}outbox: {pending} pending (adapter={adapter})")

    # Staleness alarm (techne#95): warning when capture has gone quiet. Keyed on
    # the last observation, not session rows — a long/resumed session is normal.
    if health.get("stale"):
        last_capture_str = health.get("last_capture") or "unknown"
        lines.append(
            f"> ⚠️ techne capture gap: no observations for {_format_age(last_capture_str)}"
            f" — hook may be down"
        )

    # Vector channel (techne#358): alarm when recall has silently degraded to
    # FTS-only — stored vectors but no backend to use them — or the vec index is
    # stale after a backend/model swap. Healthy and never-used states stay silent,
    # consistent with the outbox/staleness lines above (warnings-only banner).
    vec = health.get("vectors")
    if vec and vec.get("degraded"):
        if not vec.get("backend"):
            lines.append(
                "> ⚠️ vector search off — no embedding backend; recall is keyword-only. "
                "Install techne[vectors], then `techne vectors backfill`"
            )
        elif vec.get("reembed_needed"):
            lines.append(
                "> ⚠️ vectors stale after a backend/model swap — run `techne vectors backfill`"
            )

    # Hook error surfacing (techne#95): surface last persisted failure.
    hook_err = health.get("hook_last_error")
    if hook_err and isinstance(hook_err, dict):
        stage = hook_err.get("stage", "?")
        msg = hook_err.get("msg", "?")
        err_ts = hook_err.get("ts", "")
        lines.append(f"> ⚠️ hook error [{stage}] {msg} (at {err_ts})")

    # Subsystem liveness headline (techne#468). ONE line, and only when something
    # is actually wrong — the banner's value is that a warning on it means
    # something, so a permanent "all good" row would train the eye to skip it.
    # Names the worst offenders rather than a bare count, because "3 stale" sends
    # you hunting while "rail:consolidate, rail:dream" is actionable.
    # techne#488 — say how many checks were NOT run. `{'dormant': 12}` gives a
    # reader no way to tell there was ever a thirteenth, and this module's whole
    # premise is that an absent check reads as "nothing wrong".
    #
    # ONLY ON A LINE THAT ALREADY EXISTS. The banner prints no liveness row when
    # everything is healthy, on purpose — "a permanent 'all good' row would train
    # the eye to skip it" — so a deferred notice cannot be given its own row
    # without reversing that call. The unconditional record is the structured
    # `checks_deferred` key, which every dict consumer (techne_status, the MCP
    # surface) sees whether or not a warning renders here.
    # `or []` also absorbs the None ("undetermined") case: no note rather than a
    # confident claim about a registry we could not read.
    deferred = health.get("checks_deferred") or []
    # Capped at 3 like the offender list below, whose reasoning applies equally:
    # a long banner line is bad. `techne doctor` is already named in the sentence
    # this suffix attaches to, so it is not repeated here.
    deferred_note = ""
    if deferred:
        shown = ", ".join(deferred[:3])
        rest = len(deferred) - 3
        deferred_note = (
            f" ({len(deferred)} deferred: {shown}{f', +{rest} more' if rest > 0 else ''})"
        )

    worst_verdict = health.get("checks_worst")
    if worst_verdict and worst_verdict not in ("live", "dormant"):
        # THE LIVENESS LAYER ITSELF FAILED — a separate case, and previously the
        # worst line the banner could print. collect_health's except arm leaves
        # `checks` EMPTY, so the naming code below rendered
        # "> ⚠️ UNKNOWN:  — `techne doctor`" — strictly worse than the bare count
        # it was written to improve on, and it discarded the one actionable fact:
        # `checks_error` was WRITTEN at collect_health and read nowhere in the
        # repository. Same defect as doctor's probe-failure path, on the surface
        # the operator actually sees. (#469 review 8, issue 2.)
        checks_error = health.get("checks_error")
        if checks_error:
            lines.append(
                f"> ⚠️ liveness check FAILED: {checks_error} — `techne doctor`{deferred_note}"
            )
            # `else`, not an early return: this block happens to be last today,
            # so a return loses nothing — but it would silently drop any line
            # appended below it later, on the one path where the operator most
            # needs the full banner. (#469 review 9.)
        else:
            all_bad = [
                c
                for c in (health.get("checks") or [])
                if c.get("verdict") not in ("live", "dormant")
            ]
            # Name only the subsystems matching the HEADLINE verdict. Listing a
            # STALE rail under an "UNOBSERVABLE:" label mislabels it, and a
            # banner that misreports its own findings is worse than one that
            # says less.
            headline = [c for c in all_bad if c.get("verdict") == worst_verdict]
            named = ", ".join(c["name"] for c in headline[:3])
            extra = len(headline) - 3
            others = len(all_bad) - len(headline)
            tail = f" (+{extra} more)" if extra > 0 else ""
            tail += f", {others} other" if others else ""
            lines.append(
                f"> ⚠️ {worst_verdict.upper()}: {named}{tail} — `techne doctor`{deferred_note}"
            )

    return "\n".join(lines)


def _format_age(ts_str: str) -> str:
    """Return a human-readable duration string for the time since ts_str."""
    last_dt = _parse_ts(ts_str)
    if last_dt is None:
        return "unknown duration"
    age = datetime.now(tz=timezone.utc) - last_dt
    total_hours = int(age.total_seconds() // 3600)
    if total_hours < 48:
        return f"{total_hours}h"
    days = total_hours // 24
    return f"{days}d"
