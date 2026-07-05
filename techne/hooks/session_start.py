#!/usr/bin/env python3
"""SessionStart hook — injects relevant context at session beginning.

Fail-safe design (techne#95, techne#119):
  1. Session registration runs first in a minimal, isolated block.
  2. Every subsequent feature runs in its own try/except.
  3. Risky imports (egress, sync adapter) are lazy — an ImportError degrades
     to "no injection" instead of crashing the hook entirely.
  4. The hook NEVER exits non-zero. Worst case: minimal banner noting the failure.
  5. Failures AFTER config load are persisted to extension_state so they survive
     stderr-swallowing. A config-load failure itself cannot be persisted (no loaded
     config means no db path) and is surfaced only via stderr + the degraded banner.
  6. A hard SIGALRM timeout (bounded_hook) ensures the hook cannot wedge the
     agent turn — phases 2-9 run inside the budget; any overrun exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _persist_hook_state(db_path, *, last_run: str, error: dict | None) -> None:
    """Write hook health to extension_state. Swallows all exceptions."""
    try:
        from techne.core.database import connect
        from techne.core.extension_state import set_state

        conn = connect(db_path)
        try:
            set_state(conn, "hook:session_start:last_run", {"ts": last_run})
            if error is not None:
                set_state(conn, "hook:session_start:last_error", error)
            else:
                # Clear any stale error on a clean run.
                from techne.core.extension_state import get_state

                if get_state(conn, "hook:session_start:last_error") is not None:
                    set_state(conn, "hook:session_start:last_error", {})
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"techne: could not persist hook state: {exc}", file=sys.stderr)


def main() -> None:
    run_ts = _now_iso()
    db_path = None
    output_parts: list[str] = []
    _failures: list[str] = []  # accumulate non-fatal degradation notes

    # ------------------------------------------------------------------
    # Phase 0: parse stdin (non-fatal; missing data just limits context)
    # ------------------------------------------------------------------
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        data = {}

    # ------------------------------------------------------------------
    # Phase 1: Config load (required; if this fails nothing else works)
    # ------------------------------------------------------------------
    try:
        from techne.config import Config

        config = Config.load()
        db_path = config.db_path
    except Exception as exc:  # noqa: BLE001
        msg = f"Config load failed: {exc}"
        print(f"techne: {msg}", file=sys.stderr)
        # Emit a minimal degraded banner and exit cleanly.
        print(
            json.dumps(
                {
                    "systemMessage": "\n## techne ⚠️\n> Config load failed — capture disabled",
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": f"techne: {msg}",
                    },
                }
            )
        )
        return

    # ------------------------------------------------------------------
    # Phases 2-9: All potentially-blocking work runs inside bounded_hook.
    #
    # If the wall-clock budget expires (SQLite contention, slow network,
    # etc.) HookTimeout propagates to the outer except, which exits 0.
    # The agent turn is never wedged.  (techne#119)
    # ------------------------------------------------------------------
    from techne.hooks._timeout import bounded_hook

    try:
        with bounded_hook(config.hooks.timeout_s):
            _run_phases_2_to_9(
                config,
                data,
                run_ts=run_ts,
                db_path=db_path,
                output_parts=output_parts,
                failures=_failures,
            )
    except Exception as exc:  # noqa: BLE001  — catches HookTimeout too
        print(f"techne: session_start degraded: {exc}", file=sys.stderr)
        sys.exit(0)


def _run_phases_2_to_9(
    config,
    data: dict,
    *,
    run_ts: str,
    db_path,
    output_parts: list[str],
    failures: list[str],
) -> None:
    """Inner helper containing phases 2-9 so bounded_hook wraps them cleanly."""
    hook_error: dict | None = None
    session_id: str | None = None

    project_path = data.get("cwd", os.getcwd())
    agent_identity = os.environ.get("TECHNE_IDENTITY", "")
    platform = config.platform
    cc_session_id = data.get("session_id") or None

    # ------------------------------------------------------------------
    # Phase 2: Capture prior last_session BEFORE inserting the new one, so the
    # banner's "last session" line shows when work last happened rather than the
    # row this hook is about to insert (which would read as "now").
    # ------------------------------------------------------------------
    prior_last_session: str | None = None
    try:
        from techne.core.state import db_stats

        prior_stats = db_stats(config.db_path)
        prior_last_session = prior_stats.get("last_session")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"prior last_session read: {exc}")

    # ------------------------------------------------------------------
    # Phase 3: Session registration (required; must succeed or we bail)
    # ------------------------------------------------------------------
    try:
        from techne.core.database import connect as _connect
        from techne.core.database import init_db
        from techne.core.state import start_session

        init_db(config.db_path).close()

        if cc_session_id:
            _conn = _connect(config.db_path)
            try:
                _conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, project_path, agent_identity, platform)"
                    " VALUES (?, ?, ?, ?)",
                    (cc_session_id, project_path, agent_identity, platform),
                )
                _conn.commit()
            finally:
                _conn.close()
            session_id = cc_session_id
        else:
            session_id = start_session(
                config.db_path,
                project_path=project_path,
                agent_identity=agent_identity,
                platform=platform,
            )
    except Exception as exc:  # noqa: BLE001
        msg = f"Session registration failed: {exc}"
        print(f"techne: {msg}", file=sys.stderr)
        hook_error = {"ts": run_ts, "stage": "session_register", "msg": str(exc)[:200]}
        if db_path:
            _persist_hook_state(db_path, last_run=run_ts, error=hook_error)
        print(
            json.dumps(
                {
                    "systemMessage": "\n## techne ⚠️\n> Session registration failed",
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": f"techne: {msg}",
                    },
                }
            )
        )
        return

    # Store session_id for downstream hooks immediately after registration.
    print(json.dumps({"session_id": session_id}), file=sys.stderr)

    # ------------------------------------------------------------------
    # Phase 4: Outbox flush (non-fatal degradation)
    # ------------------------------------------------------------------
    try:
        from techne.sync.adapter import load_adapter
        from techne.sync.outbox import flush

        adapter = load_adapter(config.sync.adapter, url=config.sync.url, path=config.sync.path)
        synced, _failed = flush(config.db_path, adapter)
        if synced:
            print(f"techne: flushed {synced} outbox entries", file=sys.stderr)
    except ImportError as exc:
        failures.append(f"outbox flush (import): {exc}")
        print(f"techne: outbox flush skipped (import error): {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"outbox flush: {exc}")
        print(f"techne: outbox flush failed: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Phase 5: Health banner (non-fatal; falls back to minimal banner)
    # ------------------------------------------------------------------
    try:
        from techne.core.health import collect_health, format_banner

        health = collect_health(config)
        # Display the pre-insert session time as "last session" — this hook just
        # inserted the current row, which would otherwise read as "now".
        health["last_session"] = prior_last_session
        # Staleness is capture-based (last observation), computed inside
        # collect_health. It is independent of the session row just inserted,
        # so no recompute is needed here.
        banner = format_banner(health)
        output_parts.append(banner)
        output_parts.append("")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"health banner: {exc}")
        print(f"techne: health banner failed: {exc}", file=sys.stderr)
        output_parts.append("\n## techne ⚠️\n> Banner generation failed")
        output_parts.append("")

    # ------------------------------------------------------------------
    # Phase 6: Resume context (threads) — bounded (techne#95)
    # ------------------------------------------------------------------
    try:
        from techne.core.state import get_resume_context
        from techne.core.state import provenance_tag as _provenance_tag

        ctx = get_resume_context(config.db_path, project_path=project_path)
        inj = config.injection

        active_threads = ctx.get("active_threads", [])[: inj.max_active_threads]
        recent_threads = ctx.get("recent_threads", [])[: inj.max_recent_threads]
        per_item = inj.thread_per_item_chars
        budget = inj.thread_char_budget
        chars_used = 0

        if active_threads:
            header = "## Active Threads\n"
            chars_used += len(header)
            thread_lines: list[str] = [header]
            for t in active_threads:
                label = (t.get("label") or "")[:per_item]
                resume = (t.get("resume_points") or "")[:per_item]
                updated = t.get("updated_at", "")
                # Provenance tag (techne#81): which client/agent authored this thread,
                # surfaced — not filtered — so cross-platform threads are visibly attributed.
                item = f"**{label}** {_provenance_tag(t)} (updated {updated})"
                if resume:
                    item += f"\nResume: {resume}"
                item += "\n"
                if chars_used + len(item) > budget:
                    break
                thread_lines.append(item)
                chars_used += len(item)
            output_parts.extend(thread_lines)

        if recent_threads:
            header = "## Recent Threads (48h)\n"
            chars_used += len(header)
            recent_lines: list[str] = [header]
            for t in recent_threads:
                status = f"[{t['status']}]" if t.get("status") != "active" else ""
                summary = (t.get("summary") or "")[:per_item]
                label = (t.get("label") or "")[:per_item]
                item = f"- **{label}** {_provenance_tag(t)} {status} — {summary}\n"
                if chars_used + len(item) > budget:
                    break
                recent_lines.append(item)
                chars_used += len(item)
            output_parts.extend(recent_lines)
            output_parts.append("")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"resume context: {exc}")
        print(f"techne: resume context failed: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Phase 7: Tier-0 knowledge injection (non-fatal)
    # ------------------------------------------------------------------
    try:
        if config.injection.enabled:
            from techne.core.egress import render_tier0_block, select_tier0_injection

            tier0_rows = select_tier0_injection(
                config.db_path,
                char_budget=config.injection.char_budget,
                recent_reserve_frac=config.injection.recent_reserve_frac,
                correction_reserve_frac=config.injection.correction_reserve_frac,
                half_life_days=config.knowledge_confidence_half_life_days,
            )
            knowledge_block = render_tier0_block(tier0_rows)
            if knowledge_block:
                output_parts.append(knowledge_block)
    except ImportError as exc:
        failures.append(f"knowledge injection (import): {exc}")
        print(
            f"techne: tier-0 injection skipped (import error): {exc}",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"knowledge injection: {exc}")
        print(
            f"techne: tier-0 injection failed (degraded to no-injection): {exc}",
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Phase 7.5: Reflection nudge (non-fatal; opt-in via reflect.enabled)
    #   The agent-assisted reflect path runs only while an agent is live, so the
    #   trigger fires at the start of the NEXT session — nudging the agent to
    #   consolidate the prior one with its own model (techne#19).
    # ------------------------------------------------------------------
    try:
        if config.reflect.enabled:
            from techne.core.reflect import pending_reflection

            pend = pending_reflection(config)
            if pend:
                sid = pend[0]["session_id"]
                extra = f" (+{len(pend) - 1} more)" if len(pend) > 1 else ""
                output_parts.append(
                    "\n## techne — pending reflection\n"
                    f"> Session {sid[:16]} has unreflected work "
                    f"({pend[0]['observations']} observations){extra}. Call "
                    f'techne_reflect_emit(session_id="{sid}"), answer each prompt with '
                    "your own model, then techne_reflect_ingest."
                )
                output_parts.append("")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"reflection nudge: {exc}")

    # ------------------------------------------------------------------
    # Phase 8: Determine hook error status and persist state
    # ------------------------------------------------------------------
    if failures:
        # Non-fatal failures — record the first one as the error signal.
        hook_error = {"ts": run_ts, "stage": "degraded", "msg": "; ".join(failures)[:200]}
    else:
        hook_error = None  # clean run — clear any prior error

    _persist_hook_state(db_path, last_run=run_ts, error=hook_error)

    # ------------------------------------------------------------------
    # Phase 9: Emit output
    # ------------------------------------------------------------------
    if output_parts:
        summary = "\n".join(output_parts)
        banner = output_parts[0] if output_parts else summary
        print(
            json.dumps(
                {
                    "systemMessage": banner,
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": summary,
                    },
                }
            )
        )


if __name__ == "__main__":
    main()
