"""Health summary — the single source of truth for "is techne running & current?".

Both the MCP ``techne_status`` tool and the SessionStart login banner (#76)
build from :func:`collect_health`, so the two surfaces can never drift in what
they report. :func:`format_banner` renders the terse operator-facing banner.
"""

from pathlib import Path

from techne import __version__
from techne.config import Config


def collect_health(config: Config) -> dict:
    """Assemble the full health/status payload.

    Always returns a dict. When the DB can't be opened the payload carries
    ``db_ok: False`` and an ``error`` string instead of raising — the banner
    turns that into the alert rather than letting the hook die silently.
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
    }

    try:
        health.update(db_stats(config.db_path))
        health["db_ok"] = True
    except Exception as e:  # noqa: BLE001 — banner must survive any DB failure
        health["db_ok"] = False
        health["error"] = f"{type(e).__name__}: {e}"

    return health


def format_banner(health: dict) -> str:
    """Render the terse login banner. Warnings only when something is off."""
    if not health.get("db_ok", False):
        return (
            f"## techne ⚠️\n"
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
    lines = [f"## techne {mark}"]

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

    return "\n".join(lines)
