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
    """Write hook health to extension_state. Swallows all exceptions.

    Thin wrapper over the shared helper (techne#468). This hook's markers came
    first (techne#95) and were the template the other hooks now copy; delegating
    keeps ONE implementation so `techne status` can enumerate every hook
    uniformly instead of special-casing whichever ones diverged.

    Key names and payload shape are unchanged — `hook:session_start:last_run`
    still carries `{"ts": ...}`, now alongside an `outcome`.
    """
    from techne.hooks._liveness import record_hook_run

    record_hook_run(
        db_path,
        "session_start",
        outcome="failed" if error else "ok",
        error=error,
        ts=last_run,
    )


def render_drift_block(config) -> str:
    """Render the hook-registration-drift warning for the banner, '' when in sync.

    A ``hooks.*`` flag can be true in config.yaml while its entry is absent from
    settings.json — init not re-run, or re-run while the flag was off, which STRIPS
    the group. The rail then silently never fires: no error, no log, no symptom.

    ``check_hook_drift`` (techne#287) has detected this since 2026-07-09, but it was
    surfaced ONLY by ``techne status`` — a command you have to remember to run, and
    which had gone 17 days uninvoked when this was found. In that window
    ``per_turn_recall`` and ``codex_grep_assist`` were both dead for three days with
    their flags still reading ``true``. So the check moves to where the operator
    already looks: a detector wired to a command nobody runs is worth no more than
    no detector at all. (techne#452)

    Claude-code only — settings.json registration is that platform's mechanism.
    """
    # Local import, matching this module's deliberate lazy-import style (hook
    # startup latency): a constant, not a subsystem.
    from techne.config import CLAUDE_CODE

    if config.platform != CLAUDE_CODE:
        return ""
    from techne.platforms.claude_code import check_hook_drift
    from techne.platforms.claude_code.hooks import SETTINGS_PATH

    # Unreadable / wrong-shaped settings.json is reported BY check_hook_drift, not
    # re-probed here. An earlier cut of this block did its own _load_json check,
    # which caught only the unparseable shape — valid-but-not-an-object JSON
    # (`[]`, `"hello"`, `{"hooks": []}`) still sailed through and produced a green
    # tick with nothing registered. Putting the guard at the source fixes this
    # surface AND `techne status`, which had the same hole. (Prudence, PR #457.)
    drift = check_hook_drift(config)
    if not drift:
        return ""
    # Two distinct conditions share this reporter, and mislabelling them is its own
    # small lie: "enabled in config but not registered" is a precise claim, and it is
    # NOT what an unreadable/wrong-shaped settings.json means (there, we cannot tell).
    #
    # Ask the CONDITION, never grep the prose. The previous cut used
    # `any("cannot be verified" in w ...)`, which (a) silently reverted to the wrong
    # heading whenever a message in hooks.py was reworded — with the whole suite
    # green — and (b) let a hook group NAMED "cannot be verified" in settings.json
    # relabel six genuine drift warnings, because the drift message embeds the
    # matcher read from that file. User data must not steer control flow.
    # (Prudence, PR #457.)
    from techne.platforms.claude_code.hooks import settings_unusable_reason

    # TOCTOU, known and benign: check_hook_drift already read settings.json, and
    # this reads it again. If the file changes in between, heading and body come
    # from different snapshots — worst case a mislabelled heading, never a crash
    # (_load_json converts OSError to RuntimeError, which this catches). Threading
    # the reason out of check_hook_drift would remove it; not worth the signature
    # churn for a mislabel. Note this second read happens ONLY when something is
    # already wrong — the happy path returns above without it. (Prudence, PR #457.)
    unverifiable = settings_unusable_reason(config) is not None
    if unverifiable:
        lines = [
            "\n## techne ⚠️ settings.json unusable",
            "> Hook registration cannot be verified — every techne hook may be",
            "> unregistered and silently not firing:",
        ]
    else:
        lines = [
            "\n## techne ⚠️ hook drift",
            "> Enabled in config.yaml but NOT registered in settings.json —",
            "> these are silently dead (no error, no log; they simply never fire):",
        ]
    lines.extend(f">   • {w}" for w in drift)
    if unverifiable:
        # `techne init` is the WRONG remedy here and would be a second lie:
        # register_hooks walks the same structure without guarding it, so for most
        # unusable shapes init itself raises (e.g. AttributeError on {"hooks": []}).
        # Repair the file first; only then is re-registering meaningful.
        # (Prudence, PR #457.)
        lines.append(
            f"> Repair or move aside {SETTINGS_PATH}, then run "
            f"`techne init --posture {config.posture.mode}`."
        )
    else:
        # Echo the CURRENT posture into the suggested command: `techne init` defaults
        # --posture to `auto`, which re-resolves and can silently change posture (and
        # with it whether reflect is advisory). The fix for one silent no-op must not
        # hand the operator another.
        lines.append(
            f"> Reconcile: `techne init --posture {config.posture.mode}` "
            "(pin --posture — the `auto` default can change it)"
        )
    return "\n".join(lines)


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
    except Exception as exc:  # noqa: BLE001
        print(
            f"techne: session_start received invalid JSON on stdin — context limited: {exc}",
            file=sys.stderr,
        )
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
        with bounded_hook(config.hooks.timeout_s, label="session_start"):
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
            # `last_run=run_ts` here, NOT `_now_iso()` as on the success path — and
            # deliberately left that way. It is the same shape as the critical-1
            # bug (a marker stamped from the hook's START rather than its
            # COMPLETION, which can read older than the session it registered),
            # but it cannot produce a false green on this branch: it always sets
            # `hook_error`, and check_hooks short-circuits to BROKEN on a
            # recorded error before it ever compares marker to session. Cosmetic,
            # verified rather than assumed, and pinned by no test on purpose.
            # (#469 review 6, mutation M6.)
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
    # Phase 5.4: Hook-registration drift (techne#452) — non-fatal.
    #
    # A `hooks.*` flag can be true in config.yaml while its entry is absent from
    # settings.json — init not re-run, or re-run while the flag was off, which
    # STRIPS the group. The rail then silently never fires: no error, no log, no
    # symptom. check_hook_drift (#287) has detected this since 2026-07-09, but it
    # was surfaced ONLY by `techne status` — a command you must remember to run,
    # and which had not been invoked in 17 days when this was found. In that gap
    # per_turn_recall and codex_grep_assist were both dead for three days with
    # their flags reading `true`.
    #
    # So the check moves to where you already look. A detector wired to a command
    # nobody runs has the same practical value as no detector.
    # ------------------------------------------------------------------
    # Held in its own variable, NOT just appended to output_parts: Phase 9 sends
    # output_parts[0] (always the health banner) as `systemMessage` — the only
    # OPERATOR-facing surface (core/health.py:179) — and the joined remainder as
    # `additionalContext`, which goes to the model. Appending alone therefore put
    # this warning in front of the agent while the operator saw an unqualified
    # green tick: a rail whose whole purpose is reporting silent no-ops, itself
    # silently not reaching the person who has to run `techne init`.
    # (Caught by Prudence on PR #457 — it is the exact class this PR is about.)
    operator_drift = ""
    try:
        operator_drift = render_drift_block(config)
        if operator_drift:
            output_parts.append(operator_drift)
            output_parts.append("")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"hook drift check: {exc}")
        print(f"techne: hook drift check failed: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Phase 5.5: Coordination presence + unread (techne#376)
    # ------------------------------------------------------------------
    try:
        from techne.core import coord

        live = coord.live_sessions(exclude_session=session_id)
        unread = coord.unread_count(config.db_path, session_id) if session_id else 0
        collisions = coord.working_tree_collisions(session_id) if session_id else []
        if unread or live or collisions:
            bits = []
            for c in collisions:
                peer = c.get("name") or c["session_id"][:8]
                bits.append(
                    f"⚠️ COLLISION: {peer} — {c['reason']} (branch {c.get('branch')}). "
                    f"Coordinate via techne_send before you touch shared git state."
                )
            if unread:
                bits.append(f"📬 {unread} unread coordination message(s) — techne_inbox to read")
            if live:
                who = ", ".join(
                    f"{s.get('name') or (s['session_id'][:8])} ({s.get('status') or '?'})"
                    for s in live
                )
                bits.append(f"live peer session(s): {who}")
            output_parts.append("## Coordination (techne#376)\n> " + "\n> ".join(bits))
            output_parts.append("")
    except Exception as exc:  # noqa: BLE001 — coord banner must never break session start
        print(f"techne: coord banner failed: {exc}", file=sys.stderr)

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
            # techne#329 — instrument the session-start injection (the memory
            # read-path that's actually live by default). Best-effort; the
            # helper never raises. Logs even an empty selection (signal).
            from techne.hooks._injection_log import log_context_injection

            log_context_injection(
                config.db_path,
                source="session_start",
                session_id=session_id,
                trigger=None,
                rows=tier0_rows,
                injected_text=knowledge_block,
            )
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
        # Non-fatal failures — record the first one as the error signal. The
        # error payload keeps `run_ts` deliberately: it answers "when did the
        # run that failed BEGIN".
        hook_error = {"ts": run_ts, "stage": "degraded", "msg": "; ".join(failures)[:200]}
    else:
        hook_error = None  # clean run — clear any prior error

    # STAMP AT WRITE TIME, NOT AT ENTRY (#469 review 4, critical 1). `run_ts` is
    # captured as the first statement of main(), but the session row is INSERTed
    # in Phase 3 with DEFAULT CURRENT_TIMESTAMP — so a marker carrying `run_ts`
    # is PROVABLY older than the row `check_hooks` compares it against. Measured
    # window ~70ms; at one-second timestamp granularity that is roughly a 7%
    # chance per session of the second ticking between them, which rendered a
    # perfectly healthy hook as "a session started at X and left no marker".
    #
    # The marker answers "when did this hook last COMPLETE a run", so write time
    # is not merely safer, it is the correct semantic. With this ordering,
    # marker >= session_row holds for every healthy run and the comparison in
    # check_hooks becomes sound.
    _persist_hook_state(db_path, last_run=_now_iso(), error=hook_error)

    # ------------------------------------------------------------------
    # Phase 9: Emit output
    # ------------------------------------------------------------------
    if output_parts:
        summary = "\n".join(output_parts)
        banner = output_parts[0] if output_parts else summary
        # Hook-registration drift rides the OPERATOR surface, not just the agent's.
        # `systemMessage` is what the operator sees; `additionalContext` goes to the
        # model. Drift is only actionable by a human (`techne init`), so telling the
        # model alone is telling the wrong party. Everything else in output_parts is
        # context FOR the model and stays where it is. (techne#452 / PR #457.)
        if operator_drift:
            banner = f"{banner}\n{operator_drift}"
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
