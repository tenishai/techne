"""Health summary — the single source of truth for "is techne running & current?".

Both the MCP ``techne_status`` tool and the SessionStart login banner (#76)
build from :func:`collect_health`, so the two surfaces can never drift in what
they report. :func:`format_banner` renders the terse operator-facing banner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from techne import __version__
from techne.config import Config

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
    import techne
    from techne.core.state import db_stats

    package_path = str(Path(techne.__file__).resolve().parent)
    health: dict = {
        "version": __version__,
        "package_path": package_path,
        "db_path": str(config.db_path),
        "sync_adapter": config.sync.adapter,
        # "live" = running from an editable checkout (package_path is the repo
        # working tree). "installed" = a site-packages/pipx copy that can lag
        # repo HEAD — the exact editable-venv-vs-pipx drift trap. Heuristic, but
        # cheap: no git subprocess in a hot startup hook.
        "build": "installed" if "site-packages" in package_path else "live",
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

        vstatus = vector_channel_status(config.db_path)
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

    return health


def format_banner(health: dict) -> str:
    """Render the terse login banner. Warnings only when something is off."""
    if not health.get("db_ok", False):
        return (
            f"\n## techne ⚠️\n"
            f"> DB UNREACHABLE — {health['db_path']}: "
            f"{health.get('error', 'unknown error')}"
        )

    build = health["build"]
    # ✅ only when running live code; an installed copy gets a ⚠️ so the
    # version-drift trap is visible at a glance.
    healthy = build == "live"
    mark = "✅" if healthy else "⚠️"
    build_note = "live" if healthy else "installed — may lag repo HEAD"

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
