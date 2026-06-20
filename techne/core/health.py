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


def collect_health(config: Config) -> dict:
    """Assemble the full health/status payload.

    Always returns a dict. When the DB can't be opened the payload carries
    ``db_ok: False`` and an ``error`` string instead of raising — the banner
    turns that into the alert rather than letting the hook die silently.

    New fields added by techne#95:
      hook_last_run  — ISO timestamp of last session_start hook run (or None)
      hook_last_error — {ts, stage, msg} of last hook failure (or None)
      stale          — True when prior last_session is older than staleness_hours
      capture_alive  — True when observations table has grown in the last 48h
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

    # Staleness: compare prior last_session to now (techne#95).
    try:
        last_session_str = health.get("last_session")
        if last_session_str:
            # SQLite stores timestamps as strings — may be ISO or SQLite CURRENT_TIMESTAMP.
            # Parse the most common formats without pulling in dateutil.
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    last_dt = datetime.strptime(last_session_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                last_dt = None

            if last_dt is not None:
                age = datetime.now(tz=timezone.utc) - last_dt
                health["stale"] = age > timedelta(hours=config.staleness_hours)
        else:
            # No sessions at all — treat as potentially stale but don't alarm on
            # a brand-new installation.
            health["stale"] = False
    except Exception:  # noqa: BLE001
        health["stale"] = False

    # capture_alive: did observations grow in the last 48h? Cheap single query.
    try:
        from techne.core.database import connect as _connect

        _conn = _connect(config.db_path)
        try:
            row = _conn.execute(
                "SELECT COUNT(*) FROM observations WHERE timestamp > datetime('now', '-48 hours')"
            ).fetchone()
            health["capture_alive"] = (row[0] if row else 0) > 0
        finally:
            _conn.close()
    except Exception:  # noqa: BLE001
        health["capture_alive"] = False

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

    # Staleness alarm (techne#95): visible warning when last_session is stale.
    if health.get("stale"):
        last_session_str = health.get("last_session") or "unknown"
        lines.append(
            f"> ⚠️ techne capture gap: no sessions for {_format_age(last_session_str)}"
            f" — hook may have been down"
        )

    # Hook error surfacing (techne#95): surface last persisted failure.
    hook_err = health.get("hook_last_error")
    if hook_err and isinstance(hook_err, dict):
        stage = hook_err.get("stage", "?")
        msg = hook_err.get("msg", "?")
        err_ts = hook_err.get("ts", "")
        lines.append(f"> ⚠️ hook error [{stage}] {msg} (at {err_ts})")

    return "\n".join(lines)


def _format_age(last_session_str: str) -> str:
    """Return a human-readable duration string for the time since last_session_str."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            last_dt = datetime.strptime(last_session_str, fmt).replace(tzinfo=timezone.utc)
            age = datetime.now(tz=timezone.utc) - last_dt
            total_hours = int(age.total_seconds() // 3600)
            if total_hours < 48:
                return f"{total_hours}h"
            days = total_hours // 24
            return f"{days}d"
        except ValueError:
            continue
    return "unknown duration"
