"""techne CLI — human interface for inspecting and managing agent memory."""

import json
from pathlib import Path

import click

from techne import __version__
from techne.config import PLATFORMS, Config, PostureConfig, techne_home


@click.group()
@click.version_option(version=__version__)
@click.pass_context
def main(ctx):
    """techne — agentic memory and knowledge system."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = Config.load()


@main.command()
@click.pass_context
def status(ctx):
    """Show current state: active threads, session info, database stats."""
    config = ctx.obj["config"]
    from techne.core.state import db_stats, get_active_session, platform_breakdown

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    stats = db_stats(config.db_path)
    session = get_active_session(config.db_path)
    breakdown = platform_breakdown(config.db_path)

    click.echo(f"Database: {config.db_path}")
    click.echo(f"Platform: {config.platform}")
    posture_mode = getattr(getattr(config, "posture", None), "mode", "observe")
    click.echo(f"Posture: {posture_mode}")
    click.echo(f"Sync adapter: {config.sync.adapter}")
    click.echo()
    click.echo(f"Sessions: {stats['sessions']}")
    click.echo(f"Threads: {stats['threads']} ({stats['active_threads']} active)")
    click.echo(f"Observations: {stats['observations']}")
    click.echo(f"Knowledge: {stats['knowledge']}")
    click.echo(f"Identity: {stats['identity']}")
    click.echo(f"Outbox: {stats['pending_outbox']} pending")
    if stats["last_session"]:
        click.echo(f"Last session: {stats['last_session']}")
    if session:
        click.echo(f"Active session: {session['id'][:16]}... (project: {session['project_path']})")
    if breakdown:
        click.echo()
        click.echo("Platforms:")
        for plat, counts in breakdown.items():
            click.echo(f"  {plat}: {counts['sessions']} sessions, {counts['observations']} obs")


@main.group(invoke_without_command=True)
@click.option("--active", is_flag=True, help="Only show threads with unfinished resume points")
@click.option("--topic", default="", help="Search threads by topic")
@click.option("--limit", default=20, help="Max results")
@click.pass_context
def threads(ctx, active, topic, limit):
    """List recent threads across sessions (or run a subcommand)."""
    if ctx.invoked_subcommand is not None:
        return

    config = ctx.obj["config"]
    from techne.core.database import connect

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    conn = connect(config.db_path)
    try:
        if topic:
            from techne.core.search import search_fts

            results = search_fts(config.db_path, topic, table="threads", limit=limit)
            for r in results:
                status_tag = f" [{r['status']}]" if r["status"] != "active" else ""
                click.echo(f"  {r['id'][:20]}...{status_tag}  {r['label']}")
                click.echo(f"    {r['summary'][:200]}")
                if r.get("resume_points"):
                    click.echo(f"    Resume: {r['resume_points'][:200]}")
                click.echo()
        else:
            query = "SELECT id, label, summary, resume_points, status, updated_at FROM threads"
            params: list = []
            if active:
                query += (
                    " WHERE status = 'active' AND resume_points IS NOT NULL AND resume_points != ''"
                )
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            for r in rows:
                status_tag = f" [{r['status']}]" if r["status"] != "active" else ""
                click.echo(f"  {r['id'][:20]}...{status_tag}  {r['label']}")
                click.echo(f"    {r['summary'][:200]}")
                if r["resume_points"]:
                    click.echo(f"    Resume: {r['resume_points'][:200]}")
                click.echo()
    finally:
        conn.close()


@threads.command("redetect")
@click.argument("session_id")
@click.option(
    "--sync/--no-sync",
    default=False,
    help=(
        "Enqueue thread_summary outbox entries for downstream consumers. "
        "Off by default — iterative tuning would flood the outbox with "
        "stale syncs. Turn on once the detector settles."
    ),
)
@click.pass_context
def threads_redetect(ctx, session_id, sync):
    """Re-run heuristic thread detection on an already-recorded session.

    Useful after the detector itself changed (new signals, retuned weights)
    or after late observations arrived via `techne import`. Replaces existing
    threads + boundary scores for the session and refreshes the heuristic
    summary; the manual / LLM summary slots are left alone, and ended_at is
    NOT touched (this is a re-analysis, not a re-end).

    SESSION_ID can be a prefix — the first matching session is used.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core.database import connect
    from techne.lifecycle import redetect_threads

    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? || '%' ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        click.echo(f"No session matching: {session_id}")
        return

    sid = row["id"]
    result = redetect_threads(config, sid, sync=sync)
    sync_msg = f", {result['queued']} queued for sync" if sync else ""
    click.echo(
        f"Redetected {sid[:16]}... — {result['threads']} threads from "
        f"{result['observations']} observations ({result['boundaries']} scored pairs)"
        f"{sync_msg}"
    )


@threads.command("show")
@click.argument("session_id")
@click.option("--no-color", is_flag=True, help="Disable colorized output")
@click.option(
    "--detector",
    type=click.Choice(["auto", "live", "stored"]),
    default="auto",
    help=(
        "Where to source boundary scores. 'auto' uses stored if present, else "
        "runs the heuristic detector live. 'live' always re-runs. 'stored' only "
        "shows what's persisted (errors out if there's nothing)."
    ),
)
@click.pass_context
def threads_show(ctx, session_id, no_color, detector):
    """Show the observation timeline + heuristic boundary scores for a session.

    SESSION_ID can be a prefix — the first matching session is used.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core import heuristic_threads as ht
    from techne.core.database import connect

    def maybe_style(text, **kwargs):
        return text if no_color else click.style(text, **kwargs)

    conn = connect(config.db_path)
    try:
        sess = conn.execute(
            "SELECT id, project_path, started_at FROM sessions "
            "WHERE id LIKE ? || '%' ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if sess is None:
            click.echo(f"No session matching: {session_id}")
            return

        sid = sess["id"]
        click.echo(maybe_style(f"Session {sid}", bold=True))
        click.echo(f"  project: {sess['project_path']}  started: {sess['started_at']}")
        click.echo()

        obs_rows = conn.execute(
            "SELECT timestamp, tool_name, content FROM observations "
            "WHERE session_id = ? ORDER BY timestamp",
            (sid,),
        ).fetchall()
        if not obs_rows:
            click.echo("(no observations)")
            return

        observations = [
            {"tool_name": o["tool_name"], "content": o["content"], "timestamp": o["timestamp"]}
            for o in obs_rows
        ]

        stored = ht.load_boundaries(conn, sid)
        if detector == "stored" and not stored:
            click.echo("No stored boundaries for this session. Re-run with --detector=live.")
            return

        if detector == "live" or (detector == "auto" and not stored):
            segments, scores = ht.detect_threads(observations)
            boundary_set = ht.boundary_indices_from_segments(segments)
            score_rows = [
                {
                    "obs_index_a": s.a_index,
                    "obs_index_b": s.b_index,
                    "gap_seconds": s.gap_seconds,
                    "family_a": s.family_a,
                    "family_b": s.family_b,
                    "signals": s.signals,
                    "total_score": s.total,
                    "is_boundary": s.a_index in boundary_set,
                }
                for s in scores
            ]
            source_label = "live"
        else:
            score_rows = stored
            source_label = "stored"
            segments = _segments_from_boundary_rows(len(observations), stored)

        click.echo(
            maybe_style(
                f"Boundary scores ({source_label}): {len(score_rows)} pairs, "
                f"{sum(1 for r in score_rows if r['is_boundary'])} boundaries, "
                f"{len(segments)} threads",
                fg="green",
            )
        )
        click.echo()

        threads_by_num = {
            t["thread_num"]: dict(t)
            for t in conn.execute(
                "SELECT thread_num, label, summary, resume_points FROM threads "
                "WHERE session_id = ? ORDER BY thread_num",
                (sid,),
            ).fetchall()
        }

        scores_by_a = {r["obs_index_a"]: r for r in score_rows}

        for thread_num, segment in enumerate(segments, 1):
            t = threads_by_num.get(thread_num, {})
            header = f"Thread {thread_num}: {t.get('label', '(unlabeled)')} ({len(segment)} obs)"
            click.echo(maybe_style(f"━━━ {header} ━━━", fg="cyan", bold=True))
            if t.get("summary"):
                click.echo(maybe_style(f"  {t['summary']}", fg="cyan"))
            if t.get("resume_points"):
                for line in t["resume_points"].splitlines():
                    click.echo(maybe_style(f"  ⤷ {line}", fg="cyan"))
            click.echo()

            for i in segment:
                obs = observations[i]
                ts = (obs.get("timestamp") or "")[:19]
                tool = obs.get("tool_name") or "?"
                content = (obs.get("content") or "").replace("\n", " ")
                snippet = content[:80]
                click.echo(f"  [{i:3d}] {ts}  {tool:<24}  {snippet}")

                s = scores_by_a.get(i)
                if s and s["total_score"] > 0:
                    marker = "║" if s["is_boundary"] else "·"
                    sig_str = " ".join(f"{k}={v:.1f}" for k, v in sorted(s["signals"].items()))
                    line = (
                        f"       {marker} score={s['total_score']:.2f}  "
                        f"gap={s['gap_seconds']:.0f}s  [{sig_str}]"
                    )
                    color = "yellow" if s["is_boundary"] else None
                    click.echo(maybe_style(line, fg=color) if color else line)
            click.echo()
    finally:
        conn.close()


def _segments_from_boundary_rows(n_obs: int, rows: list[dict]) -> list[list[int]]:
    """Rebuild segments from persisted boundary rows."""
    bounds = {r["obs_index_a"] for r in rows if r["is_boundary"]}
    segments: list[list[int]] = []
    current: list[int] = []
    for i in range(n_obs):
        current.append(i)
        if i in bounds:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


@main.command()
@click.argument("query")
@click.option(
    "--table",
    type=click.Choice(["all", "threads", "observations", "knowledge"]),
    default="all",
)
@click.option("--limit", default=20)
@click.pass_context
def search(ctx, query, table, limit):
    """Search across all layers — threads, observations, knowledge."""
    config = ctx.obj["config"]
    from techne.core.search import search as do_search

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    results = do_search(config.db_path, query, table=table, limit=limit)
    if not results:
        click.echo("No results.")
        return

    for r in results:
        source = r.get("source", "fts")
        type_tag = r["type"]
        click.echo(f"  [{type_tag}] ({source})")
        if type_tag == "thread":
            click.echo(f"    {r.get('label', '')} — {r.get('summary', '')[:200]}")
        elif type_tag == "observation":
            click.echo(f"    [{r.get('tool_name', '')}] {r.get('content', '')[:200]}")
        elif type_tag == "knowledge":
            click.echo(f"    [{r.get('knowledge_type', '')}] {r.get('content', '')[:200]}")
        elif type_tag == "document":
            click.echo(f"    #{r.get('id')} {r.get('title', '') or r.get('source_url', '')}")
            snippet = (r.get("content", "") or "").replace("\n", " ").strip()
            click.echo(f"    {snippet[:200]}")
            click.echo(f"    ↳ full: techne library show {r.get('id')}")
        click.echo()


@main.command()
@click.option("--days", default=7, help="Number of days to show")
@click.option("--limit", default=20)
@click.pass_context
def history(ctx, days, limit):
    """Show session history with thread summaries."""
    config = ctx.obj["config"]
    from techne.core.database import connect

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core.state import effective_summary

    conn = connect(config.db_path)
    try:
        rows = conn.execute(
            """SELECT id, started_at, ended_at, project_path, agent_identity,
                      summary, summary_llm, summary_heuristic
               FROM sessions
               WHERE started_at > datetime('now', ? || ' days')
               ORDER BY started_at DESC LIMIT ?""",
            (f"-{days}", limit),
        ).fetchall()

        for s in rows:
            ended = s["ended_at"] or "active"
            click.echo(f"  {s['started_at']}  {s['id'][:16]}...  [{ended}]")
            if s["project_path"]:
                click.echo(f"    Project: {s['project_path']}")
            summary = effective_summary(s)
            if summary:
                click.echo(f"    {summary[:300]}")

            # Show threads for this session
            threads = conn.execute(
                "SELECT label, status FROM threads WHERE session_id = ? ORDER BY thread_num",
                (s["id"],),
            ).fetchall()
            if threads:
                labels = ", ".join(f"{t['label']}[{t['status']}]" for t in threads)
                click.echo(f"    Threads: {labels}")
            click.echo()
    finally:
        conn.close()


@main.group()
@click.pass_context
def knowledge(ctx):
    """Browse and manage the knowledge store."""
    pass


@knowledge.command("list")
@click.option(
    "--type", "ktype", type=click.Choice(["fact", "decision", "pattern", "evaluation", "procedure"])
)
@click.option("--tags", default="")
@click.option("--limit", default=50)
@click.pass_context
def knowledge_list(ctx, ktype, tags, limit):
    """List knowledge entries."""
    config = ctx.obj["config"]
    from techne.core.knowledge import list_entries

    entries = list_entries(config.db_path, type=ktype, tags=tags or None, limit=limit)
    for e in entries:
        click.echo(f"  [{e['id']}] [{e['type']}] ({e['confidence']}) {e['content'][:200]}")
        if e["tags"]:
            click.echo(f"    Tags: {e['tags']}")
        click.echo()


@knowledge.command("add")
@click.argument("content")
@click.option(
    "--type",
    "ktype",
    type=click.Choice(["fact", "decision", "pattern", "evaluation", "procedure"]),
    default="fact",
)
@click.option("--tags", default="")
@click.option("--confidence", type=click.Choice(["high", "medium", "low"]), default="medium")
@click.pass_context
def knowledge_add(ctx, content, ktype, tags, confidence):
    """Add a knowledge entry."""
    config = ctx.obj["config"]
    from techne.core.knowledge import store

    entry_id = store(
        config.db_path, content, type=ktype, tags=tags, confidence=confidence, source="manual"
    )
    click.echo(f"Stored knowledge entry #{entry_id}")


@main.group()
@click.pass_context
def identity(ctx):
    """View and manage identity configuration."""
    pass


@identity.command("show")
@click.option("--prefix", default="", help="Filter by key prefix")
@click.pass_context
def identity_show(ctx, prefix):
    """Display identity entries."""
    config = ctx.obj["config"]
    from techne.core.identity import list_entries

    entries = list_entries(config.db_path, prefix=prefix)
    for e in entries:
        value_preview = e["value"][:100] + "..." if len(e["value"]) > 100 else e["value"]
        click.echo(f"  {e['key']} = {value_preview}  ({e['source']})")


@main.group()
@click.pass_context
def outbox(ctx):
    """Manage the sync outbox."""
    pass


@outbox.command("status")
@click.pass_context
def outbox_status(ctx):
    """Show outbox statistics."""
    config = ctx.obj["config"]
    from techne.sync.outbox import stats

    s = stats(config.db_path)
    click.echo(
        f"Total: {s['total']}  Pending: {s['pending']}  Synced: {s['synced']}  Dead: {s['dead']}"
    )


@outbox.command("flush")
@click.pass_context
def outbox_flush(ctx):
    """Manually flush pending entries to team backend."""
    config = ctx.obj["config"]
    from techne.sync.adapter import load_adapter
    from techne.sync.outbox import flush

    adapter = load_adapter(config.sync.adapter, url=config.sync.url, path=config.sync.path)
    synced, failed = flush(config.db_path, adapter)
    click.echo(f"Flushed: {synced} synced, {failed} failed")


@outbox.command("dead")
@click.pass_context
def outbox_dead(ctx):
    """List permanently failed outbox entries."""
    config = ctx.obj["config"]
    from techne.sync.outbox import list_dead

    entries = list_dead(config.db_path)
    if not entries:
        click.echo("No dead entries.")
        return
    for e in entries:
        click.echo(f"  [{e['id']}] {e['target']}  {e['error_message']}  {e['created_at']}")


@outbox.command("flush-sinks")
@click.pass_context
def outbox_flush_sinks(ctx):
    """Flush pending rows to registered sinks (manual trigger)."""
    config = ctx.obj["config"]
    from techne.sync.outbox import flush_sinks

    summary = flush_sinks(config.db_path)
    if not summary:
        click.echo("No pending rows.")
        return
    for target, (synced, failed) in sorted(summary.items()):
        click.echo(f"  {target}: {synced} synced, {failed} failed")


@outbox.command("prune-orphans")
@click.pass_context
def outbox_prune_orphans(ctx):
    """Mark pending rows for unregistered targets as dead."""
    config = ctx.obj["config"]
    from techne.sync.outbox import prune_orphans

    pruned = prune_orphans(config.db_path)
    click.echo(f"Pruned {pruned} orphan row(s).")


@outbox.command("purge")
@click.option("--target", default=None, help="Restrict to one target (default: all).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.pass_context
def outbox_purge(ctx, target, yes):
    """Delete pending rows that no consumer will drain (e.g. noop adapter).

    Regenerable: the next session re-enqueues current threads once a real
    adapter is configured.
    """
    config = ctx.obj["config"]
    from techne.sync.outbox import pending_count, purge_pending

    n = pending_count(config.db_path)
    if n == 0:
        click.echo("No pending rows to purge.")
        return
    scope = f"target={target!r}" if target else "all targets"
    if not yes:
        click.confirm(f"Delete {n} pending row(s) ({scope})?", abort=True)
    deleted = purge_pending(config.db_path, target=target)
    click.echo(f"Purged {deleted} pending row(s).")


@main.group()
@click.pass_context
def sinks(ctx):
    """Inspect registered outbox sinks."""
    pass


@sinks.command("list")
@click.pass_context
def sinks_list(ctx):
    """Show all registered sinks: name, description, schema fields."""
    import dataclasses

    from techne.sync.sinks import list_sinks

    registered = list_sinks()
    if not registered:
        click.echo("No sinks registered.")
        return
    for s in registered:
        fields = (
            ", ".join(f.name for f in dataclasses.fields(s.payload_schema))
            if dataclasses.is_dataclass(s.payload_schema)
            else str(s.payload_schema)
        )
        click.echo(f"  {s.name}")
        if s.description:
            click.echo(f"    {s.description}")
        click.echo(f"    schema fields: {fields}")


@sinks.command("describe")
@click.argument("name")
@click.pass_context
def sinks_describe(ctx, name):
    """Show full schema field list with types for a named sink."""
    import dataclasses

    from techne.sync.sinks import get_sink

    sink = get_sink(name)
    if sink is None:
        click.echo(f"No sink registered: {name}", err=True)
        ctx.exit(1)
        return
    click.echo(f"Sink: {sink.name}")
    if sink.description:
        click.echo(f"  {sink.description}")
    click.echo("  Schema fields:")
    if dataclasses.is_dataclass(sink.payload_schema):
        for f in dataclasses.fields(sink.payload_schema):
            type_name = (
                f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
            )
            has_default = (
                f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
            )  # type: ignore[misc]
            opt = " (optional)" if has_default else " (required)"
            click.echo(f"    {f.name}: {type_name}{opt}")
    else:
        click.echo(f"    {sink.payload_schema}")


@main.command()
@click.pass_context
def compact(ctx):
    """Manually trigger tier aging on threads."""
    config = ctx.obj["config"]
    from techne.core.threads import age_threads

    aged = age_threads(config.db_path, config)
    click.echo(f"Aged: {aged['completed']} completed, {aged['stale']} stale")


@main.command("import")
@click.argument("path", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--no-prompts", is_flag=True, help="Skip user-prompt observations (tool calls only)")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Bypass the 'session already has observations' idempotency gate. "
        "Use when live-captured observations are sparse and the full "
        "transcript should be appended on top. Produces duplicates if the "
        "session is already fully imported — only use when you know the "
        "live capture was partial."
    ),
)
@click.pass_context
def import_cmd(ctx, path: "Path | None", no_prompts: bool, force: bool):
    """Backfill observations from Claude Code JSONL transcripts.

    PATH may be a single .jsonl file or a directory of transcripts. Defaults to
    ~/.claude/projects/<project>/ for the current working directory.
    """
    from techne.platforms.claude_code import (
        default_transcripts_dir,
        import_directory,
        import_transcript,
    )

    config = ctx.obj["config"]

    if path is None:
        path = default_transcripts_dir()
        if not path.exists():
            click.echo(f"No transcripts found for current project: {path}")
            return

    include_prompts = not no_prompts
    results = (
        [
            import_transcript(
                config.db_path,
                path,
                include_user_prompts=include_prompts,
                force=force,
            )
        ]
        if path.is_file()
        else import_directory(
            config.db_path,
            path,
            include_user_prompts=include_prompts,
            force=force,
        )
    )

    imported = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    tool_total = sum(r.get("tool_obs", 0) for r in results)
    prompt_total = sum(r.get("prompt_obs", 0) for r in results)
    summaries_set = sum(1 for r in results if r.get("summary_set"))

    for r in results:
        sid = (r.get("session_id") or "")[:8]
        marker = " +title" if r.get("summary_set") else ""
        if r.get("skipped"):
            click.echo(f"  skip {sid}  — {r.get('reason', 'skipped')}{marker}")
        else:
            click.echo(
                f"  ok   {sid}  — {r['tool_obs']} tool obs, "
                f"{r['prompt_obs']} prompts{marker}  ({r.get('project_path', '')})"
            )
    click.echo(
        f"\n{imported} imported, {skipped} skipped — "
        f"{tool_total} tool obs + {prompt_total} prompts added, "
        f"{summaries_set} session summaries set"
    )


@main.command("migrate-summaries")
@click.option("--dry-run", is_flag=True, help="Report what would move without writing")
@click.pass_context
def migrate_summaries_cmd(ctx, dry_run: bool):
    """One-shot: move heuristic-shaped sessions.summary content to summary_heuristic.

    Pre-dual-column rows packed every summary source into `summary`. This
    cleanup reclassifies legacy auto-generated content so the priority rule
    (manual > LLM > heuristic) picks newer LLM signals over old heuristic
    breakdowns. Conservative — only matches the auto-generator's structural
    fingerprint; leaves anything ambiguous alone.
    """
    from techne.core.database import connect
    from techne.core.state import looks_heuristic, migrate_legacy_summaries

    config = ctx.obj["config"]

    # Detect already-migrated state: schema >= v4 (typed columns exist) AND
    # no heuristic-shaped content remains pinned in the legacy `summary` slot.
    db_conn = connect(config.db_path)
    try:
        version_row = db_conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        schema_version = version_row[0] if version_row and version_row[0] is not None else 0
        if schema_version >= 4:
            legacy_rows = db_conn.execute(
                "SELECT summary FROM sessions WHERE summary IS NOT NULL AND summary != ''"
            ).fetchall()
            if not any(looks_heuristic(r["summary"]) for r in legacy_rows):
                click.echo("nothing to do (already migrated)")
                return
    finally:
        db_conn.close()

    result = migrate_legacy_summaries(config.db_path, dry_run=dry_run)
    moved_verb = "would move" if dry_run else "moved"
    cleared_verb = "would clear" if dry_run else "cleared"
    click.echo(
        f"Scanned {result['scanned']} rows — "
        f"{moved_verb} {result['moved']}, "
        f"{cleared_verb} {result['cleared']} (legacy redundant with existing), "
        f"skipped {result['skipped']}"
    )


@main.command("init")
@click.option("--platform", type=click.Choice(list(PLATFORMS)), default="claude-code")
@click.option(
    "--identity-dir",
    type=click.Path(exists=True),
    default=None,
    help="Directory with identity markdown files",
)
@click.option(
    "--posture",
    type=click.Choice(["auto", "observe", "spool-primary"]),
    default="auto",
    help=(
        "Coexistence posture with CC curated memory. "
        "'auto' detects CC memory and picks spool-primary if found, else observe. "
        "'spool-primary' forces downstream-only mode (reflect advisory). "
        "'observe' keeps techne independent."
    ),
)
@click.pass_context
def init(ctx, platform, identity_dir, posture):
    """Initialize techne — create database, register hooks, set up MCP."""
    from techne.core.database import init_db
    from techne.core.memory_doctor import find_curated_memory_files

    config = Config.load()
    config.platform = platform
    if identity_dir:
        config.identity_dir = Path(identity_dir)

    # Create database
    config.data_dir.mkdir(parents=True, exist_ok=True)
    conn = init_db(config.db_path)
    conn.close()
    click.echo(f"Database created: {config.db_path}")

    # Import identity if directory provided
    if config.identity_dir:
        from techne.core.identity import sync_from_markdown

        sync_from_markdown(config.db_path, config.identity_dir)
        click.echo(f"Identity imported from: {config.identity_dir}")

    # Platform-specific setup
    if platform == "claude-code":
        from techne.platforms.claude_code import register_hooks, register_mcp

        register_hooks(config)
        register_mcp(config)
        click.echo("Claude Code hooks registered")
        click.echo("MCP server registered")

    # Resolve coexistence posture (techne#107 PR4).
    cc = bool(find_curated_memory_files())
    if posture == "auto":
        resolved_mode = "spool-primary" if cc else "observe"
    else:
        resolved_mode = posture
    config.posture = PostureConfig(mode=resolved_mode, cc_memory_detected=cc)
    if resolved_mode == "spool-primary":
        click.echo(
            "Posture: spool-primary — CC authors curated memory; techne drains + reconciles"
            " downstream and keeps its own reflect ADVISORY (proposals only, no auto-promote)."
            " Applies to ALL techne projects on this machine (global config)."
            " Run `techne memory sync` / `techne memory doctor`."
        )
    else:
        # Be honest about why we landed on observe (detected-none vs forced-despite-CC).
        cc_note = (
            "CC curated memory WAS detected, but observe was requested"
            if cc else "no CC curated memory detected"
        )
        click.echo(
            "Posture: observe — techne captures + (if you enable reflect) may author its own"
            f" knowledge ({cc_note})."
        )

    # Save config. Report the resolved path save() actually wrote to (honors
    # TECHNE_HOME), not the import-time DEFAULT_CONFIG snapshot.
    saved_path = techne_home() / "config.yaml"
    config.save(saved_path)
    click.echo(f"Config saved: {saved_path}")

    # Platform-specific next-steps so the operator isn't left guessing.
    if platform == "standalone":
        click.echo("")
        click.echo("Standalone init complete. Next steps:")
        click.echo("")
        click.echo("  Expose techne as an MCP server (over stdio):")
        click.echo("    techne serve")
        click.echo("")
        click.echo("  Or embed techne directly in your agent runtime:")
        click.echo("    import techne")
        click.echo("    config = techne.Config.load()")
        click.echo("    session_id = techne.run_session_start(config, project_path='/work')")
        click.echo("    techne.capture_tool_use(config, session_id, ...)")
        click.echo("    techne.run_session_end(config, session_id)")
        click.echo("")
        click.echo("  Inspect what's been captured:")
        click.echo("    techne status")
        click.echo("    techne search '<query>'")
        return

    click.echo("\ntechne initialized. Run 'techne status' to verify.")


@main.command("watch")
@click.argument(
    "paths",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--include",
    "include",
    multiple=True,
    default=("*",),
    help="Glob pattern to include. Repeat for multiple. Defaults to '*' (all).",
)
@click.option(
    "--exclude",
    "exclude",
    multiple=True,
    default=(),
    help="Glob pattern to exclude. Repeat. e.g. --exclude '**/.git/**'",
)
@click.option(
    "--debounce-ms",
    type=int,
    default=500,
    help="Coalesce file-change bursts within this window (per file).",
)
@click.option(
    "--resync-hours",
    type=int,
    default=24,
    help="Run a full dir-walk every N hours to catch missed events.",
)
@click.pass_context
def watch_cmd(ctx, paths, include, exclude, debounce_ms, resync_hours):
    """Watch directories; ingest file changes as FileSync observations.

    Starts a new techne session keyed to the first watched path; every
    file create/modify within the watched tree (respecting the include/
    exclude globs) becomes an observation. A background full-walk runs
    every --resync-hours to catch events the OS-level watcher dropped.

    Long-running. Stops cleanly on SIGTERM / SIGINT.

    Example:

        techne watch ~/work/myproject \\
            --include '*.py' --include '*.md' \\
            --exclude '**/.git/**' --exclude '**/.venv/**'
    """
    from techne.core.state import start_session
    from techne.watch import WatchConfig
    from techne.watch import watch as run_watch

    config = Config.load()
    resolved_paths = [Path(p).resolve() for p in paths]
    session_id = start_session(
        config.db_path,
        project_path=str(resolved_paths[0]),
        agent_identity="techne-watch",
        platform=config.platform,
    )
    click.echo(
        f"Watching {len(resolved_paths)} path(s); session={session_id}",
        err=True,
    )

    wc = WatchConfig(
        db_path=config.db_path,
        paths=resolved_paths,
        session_id=session_id,
        include_globs=list(include),
        exclude_globs=list(exclude),
        debounce_ms=debounce_ms,
        resync_interval_seconds=resync_hours * 3600,
    )
    try:
        run_watch(wc)
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)
    click.echo("Watch terminated.", err=True)


@main.command("serve")
@click.pass_context
def serve(ctx):
    """Run techne as an MCP server over stdio.

    Exposes the recall + commit tools to any MCP-speaking client (Claude
    Desktop, Cursor, Continue, custom agent runtimes). Reads JSON-RPC
    requests on stdin, writes responses on stdout. Configured by
    ~/.techne/config.yaml.

    For Claude Code specifically, prefer `techne init --platform
    claude-code` which registers the MCP server automatically via
    Claude Code's `.mcp.json`. `techne serve` is the standalone path
    for everyone else.
    """
    from techne.mcp_server import main as run_server

    run_server()


@main.command("observe")
@click.option(
    "--session-id", required=True, help="Client session identifier (1:1 with a techne session)"
)
@click.option("--content", default=None, help="Observation text; if omitted, read from stdin")
@click.option(
    "--tool-name", default="Observation", help="Tool or event name (e.g. Edit, turn_complete)"
)
@click.option(
    "--platform", default=None, help="Originating client (e.g. opencode); stamps the session"
)
@click.option("--project-path", default="", help="Project path for the session row")
@click.option("--agent-identity", default="", help="Agent identity for the session row")
@click.option("--tier", default="hot", type=click.Choice(["hot", "warm", "cold"]))
@click.option("--event-at", default=None, help="ISO-8601 event time; omit for live capture")
@click.pass_context
def observe(
    ctx, session_id, content, tool_name, platform, project_path, agent_identity, tier, event_at
):
    """Record an observation — the platform-agnostic capture path.

    The CLI sibling of the techne_observe MCP tool, for clients that drive capture
    from a plugin/hook (e.g. an opencode TS plugin shelling out per event) rather
    than letting the model call the MCP tool. Content comes from --content or, if
    omitted, stdin — so multi-line payloads can be piped in.
    """
    import sys

    from techne.core.state import record_observation

    config = ctx.obj["config"]
    if content is None:
        content = sys.stdin.read()
    content = content.strip()
    if not content:
        raise click.ClickException("observe requires content (via --content or stdin)")

    result = record_observation(
        config,
        session_id,
        content,
        tool_name=tool_name,
        platform=platform,
        project_path=project_path,
        agent_identity=agent_identity,
        tier=tier,
        event_at=event_at,
    )
    click.echo(f"observed session={result['session_id']} tool={tool_name} ({len(content)} chars)")


@main.command("session-end")
@click.argument("session_id")
@click.option("--summary", default=None, help="Optional manual session summary")
@click.pass_context
def session_end(ctx, session_id, summary):
    """Run end-of-session processing on a session — detect threads, summarize, age.

    The CLI sibling of the Claude Code Stop hook, for clients that signal
    turn/session completion from a plugin (e.g. an opencode plugin firing on
    `session.idle`). Idempotent: thread IDs are content-addressed and written
    INSERT-OR-IGNORE, so replaying a session is a no-op — safe to call once per
    turn. Outbox enqueue stays gated on a live consumer (#79), so a noop adapter
    won't accumulate a backlog.
    """
    config = ctx.obj["config"]
    from techne.lifecycle import run_session_end

    result = run_session_end(config, session_id, summary=summary)
    click.echo(
        f"session-end {session_id}: {result['threads']} threads, "
        f"{result['linked']} linked, {result['queued']} queued"
    )


@main.command("context")
@click.option("--project-path", default="", help="Scope active threads to this project")
@click.option("--limit", default=5, help="Max threads/facts to include")
@click.pass_context
def context(ctx, project_path, limit):
    """Emit a compact memory block for injecting into a client's prompt.

    The read-side analogue of the Claude Code SessionStart banner: active threads
    (what you were working on) + durable facts for the project, as a text block a
    client plugin prepends to the model's system prompt — e.g. opencode's
    `experimental.chat.system.transform`. Prints nothing when there's no context
    to inject, so the caller can skip cleanly.
    """
    config = ctx.obj["config"]
    from techne.core.state import format_resume_context, get_resume_context

    data = get_resume_context(config.db_path, project_path=project_path, limit=limit)
    text = format_resume_context(data, project_path=project_path)
    if text:
        click.echo(text)


@main.group()
@click.pass_context
def memory(ctx):
    """Reconcile the client's curated memory against techne's raw deed-log (techne#107)."""


def _memory_doctor_claims(ctx, config, *, since_days, as_json):
    """Claim-level reconciliation branch for `memory doctor --claims` (techne#107 PR3).

    Separated from the main command handler so the non-claims path is untouched.
    Uses the resident-model emit/ingest pattern: the agent answers the emitted prompt
    with its own model (no API key). Falls back to manual instructions when no
    operator model is configured or the automated call fails.
    """
    from techne.core.memory_claims import claims_emit, claims_ingest

    _NO_MEMORY_MSG = (
        f"No curated memory found for this project (looked under the CC slug for "
        f"{Path.cwd()}). Nothing to reconcile — is this a Claude Code project with "
        "memory files?"
    )

    report = claims_emit(config, since_days=since_days)
    if not report["memory_present"]:
        click.echo(_NO_MEMORY_MSG)
        return

    # Automated path: if config.model is set, try the operator LLM (litellm — this path,
    # unlike the resident-model MCP path, may need provider credentials).
    result = None
    if getattr(config, "model", None):
        try:
            from techne.core.thread_orchestrator import _make_generate_fn

            gen = _make_generate_fn(config)
            raw = gen(report["prompt"])
            result = claims_ingest(config, report["prompt_hash"], raw)
        except Exception as exc:  # noqa: BLE001 — fall back to manual, but never silently
            click.echo(
                f"Automated path unavailable ({type(exc).__name__}: {exc}); "
                "falling back to the resident-model (MCP) instructions.",
                err=True,
            )
            result = None

    if result is not None:
        if as_json:
            click.echo(json.dumps(result, indent=2))
            return
        counts = result["counts"]
        click.echo(
            f"memory doctor --claims — {report['deed_count']} deed(s) considered"
        )
        click.echo(
            f"  {counts['corroborated']} corroborated  "
            f"{counts['uncorroborated']} uncorroborated  "
            f"{counts['contradicted']} contradicted  "
            f"(total {result['total']} claim(s))"
        )
        click.echo(
            f"  NOTE: {report['deed_count']} deed(s) — deed-log is ~500-char truncated; "
            "'uncorroborated' = no deed evidence, NOT false."
        )
        if report.get("deed_log_capped"):
            click.echo(
                "  ⚠ deed-log CAPPED — older deeds omitted; some 'uncorroborated' "
                "may just be off-window. Narrow with --since."
            )
        if counts["contradicted"]:
            click.echo("\n! contradicted — a deed conflicts with these claims:")
            for c in result["claims"]:
                if c["status"] == "contradicted":
                    click.echo(f"  - {c['claim']}")
                    if c["evidence"] and c["evidence"] != "none":
                        click.echo(f"    evidence: {c['evidence']}")
        if counts["uncorroborated"]:
            click.echo("\n? uncorroborated — no deed evidence (not necessarily false):")
            for c in result["claims"]:
                if c["status"] == "uncorroborated":
                    click.echo(f"  - {c['claim']}")
        return

    # Manual / no-model path: print caveat + instructions; do NOT dump the full prompt.
    click.echo(
        "Claim reconciliation uses the resident model (your own model — no API key).\n"
        "FIDELITY CAVEAT: deed-log is ~500-char truncated; 'uncorroborated' means no "
        "deed evidence, NOT false.\n\n"
        "To run claim reconciliation:\n"
        "  1. Call the MCP tool: techne_memory_claims_emit (optionally with since_days)\n"
        "  2. Answer the returned prompt with your model\n"
        "  3. Feed the response to: techne_memory_claims_ingest\n"
    )
    if as_json:
        click.echo(json.dumps({"memory_present": True,
                               "deed_count": report["deed_count"],
                               "memory_files": report.get("memory_files", []),
                               "note": "no-model path; use MCP tools to complete"}, indent=2))


@memory.command("doctor")
@click.option("--since", "since_days", type=int, default=None,
              help="Only consider deeds from the last N days (default: all).")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option("--claims", "do_claims", is_flag=True,
              help="Claim-level reconciliation via the resident model (techne#107 PR3).")
@click.pass_context
def memory_doctor_cmd(ctx, since_days, as_json, do_claims):
    """Diff the client's curated memory against techne's deed-log.

    Read-only. Surfaces files you worked on but never noted (omissions) and files your
    memory cites but no deed touched (unbacked). File-reference grain (techne#107 PR1).

    Add --claims for claim-level reconciliation: each discrete fact in curated memory
    is judged corroborated / uncorroborated / contradicted against the deed-log.
    Requires the resident model (uses YOUR model; no API key). Fidelity caveat: the
    deed-log is truncated to ~500 chars — "uncorroborated" means no deed evidence,
    NOT false.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)

    if do_claims:
        _memory_doctor_claims(ctx, config, since_days=since_days, as_json=as_json)
        return

    from techne.core.memory_doctor import reconcile

    report = reconcile(config.db_path, since_days=since_days)
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return
    if not report["memory_present"]:
        click.echo(
            f"No curated memory found for this project (looked under the CC slug for "
            f"{Path.cwd()}). Nothing to reconcile — is this a Claude Code project with "
            "memory files?"
        )
        return
    if report["deed_count"] == 0:
        # Distinct from "your memory is all wrong": we found no deeds to compare against.
        click.echo(
            "Found curated memory but NO deeds for this project — the deed-log scoping "
            "likely missed them (path mismatch / nothing captured yet). Not reconciling; "
            "every reference would read as unbacked."
        )
        return
    win = f"last {report['window_days']}d" if report["window_days"] else "all time"
    click.echo(
        f"memory doctor — {len(report['memory_files'])} memory file(s) vs "
        f"{report['deed_count']} deeds ({win})"
    )
    click.echo(
        f"  corroborated: {len(report['corroborated'])}  "
        f"omissions: {len(report['omissions'])}  unbacked: {len(report['unbacked'])}"
    )
    if report.get("deed_log_capped"):
        click.echo(
            "  ⚠ deed-log hit the cap — oldest deeds dropped; results may be "
            "incomplete. Narrow with --since."
        )
    if report["unbacked"]:
        click.echo("\n⚠ unbacked — cited in memory, no deed touched them:")
        for f in report["unbacked"]:
            click.echo(f"  - {f}")
    if report["omissions"]:
        click.echo("\n• omissions — touched in deeds, not noted in memory:")
        for f in report["omissions"]:
            click.echo(f"  - {f}")


@memory.command("sync")
@click.pass_context
def memory_sync_cmd(ctx):
    """Drain the current project's curated memory files into techne.db.

    Walks ~/.claude/projects/<slug>/memory/*.md and ingests each file via
    content-hash dedup (new files stored, changed files supersede the prior row,
    unchanged files are no-ops).  Reports how many files were drained (stored)
    versus found (total discovered).

    NOT read-only -- writes to the curated_memory table.  Safe to re-run:
    unchanged files are always a no-op.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
        return

    from techne.core.memory_doctor import find_curated_memory_files
    from techne.sync.cc_memory import CCMemorySource

    found = len(find_curated_memory_files())
    if found == 0:
        click.echo(
            f"No curated memory files found for this project "
            f"(looked under the CC slug for {Path.cwd()}). Nothing to drain."
        )
        return

    stored = CCMemorySource(config).initial_walk()
    click.echo(
        f"memory sync — {stored} file(s) drained (stored/updated) of {found} found"
    )


@main.group(invoke_without_command=True)
@click.pass_context
def reflect(ctx):
    """Inspect and manage end-of-session reflection candidates.

    Reflection is an opt-in 2-week-test feature: an LLM scans each detected
    thread at session_end and proposes durable knowledge / identity entries.
    While config.reflect.write_to='candidates' the proposals land here for
    review and do NOT touch the live knowledge / identity tables.
    """
    if ctx.invoked_subcommand is not None:
        return
    # Default behavior: show the same as `reflect stats`
    config = ctx.obj["config"]
    from techne.core.reflect import candidate_stats

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return
    s = candidate_stats(config.db_path)
    click.echo(f"Reflect enabled: {config.reflect.enabled}")
    click.echo(f"Write mode: {config.reflect.write_to}")
    click.echo(f"Pending: {s['pending']}")
    click.echo(f"Promoted: {s['promoted']}")
    click.echo(f"Discarded: {s['discarded']}")
    if s["by_scope"]:
        click.echo("By scope:")
        for k, n in sorted(s["by_scope"].items()):
            click.echo(f"  {k}: {n}")


@reflect.command("review")
@click.option(
    "--status",
    type=click.Choice(["pending", "promoted", "discarded", "all"]),
    default="pending",
    help="Filter by candidate status",
)
@click.option("--limit", default=50, help="Max candidates to show")
@click.option("--show-raw", is_flag=True, help="Include the raw LLM response")
@click.pass_context
def reflect_review(ctx, status, limit, show_raw):
    """List candidate knowledge / identity entries awaiting review."""
    config = ctx.obj["config"]
    from techne.core.reflect import list_candidates

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    rows = list_candidates(config.db_path, status=status, limit=limit)
    if not rows:
        click.echo(f"No candidates with status='{status}'.")
        return

    for r in rows:
        click.echo(f"#{r['id']}  [{r['status']}]  {r['scope']}/{r['type']}  conf={r['confidence']}")
        click.echo(f"  {r['content']}")
        if r["tags"]:
            click.echo(f"  tags: {r['tags']}")
        click.echo(
            f"  src: session={r['source_session_id'][:16]}  "
            f"thread={r['source_thread_id']}  model={r['model_used']}"
        )
        click.echo(f"  created: {r['created_at']}")
        if r["status"] == "promoted" and r.get("promoted_to_id"):
            click.echo(f"  promoted_to: knowledge.id={r['promoted_to_id']}")
        if show_raw and r.get("raw_response"):
            click.echo("  --- raw LLM response ---")
            click.echo("  " + (r["raw_response"][:1000].replace("\n", "\n  ")))
        click.echo()


@reflect.command("emit")
@click.option(
    "--session", "session_id", default=None, help="Session to reflect on (default: latest)."
)
@click.option("--limit", default=None, type=int, help="Max threads to emit prompts for.")
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the batch JSON here (default: stdout).",
)
@click.pass_context
def reflect_emit(ctx, session_id, limit, output):
    """Emit per-thread reflection prompts for an agent to answer (no API key).

    The agent-assisted promotion path (#92): techne renders the prompts, an
    in-session agent (on the operator's OAuth model) answers each as a JSON
    candidate array, and `techne reflect ingest` feeds them back. Output is a
    JSON array of {thread_id, session_id, thread_num, prompt_hash, prompt}.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)

    from techne.core.database import connect
    from techne.core.reflect import emit_reflection_batch, load_session_for_reflection

    if not session_id:
        conn = connect(config.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        session_id = row["id"] if row else None
    if not session_id:
        click.echo("No session to reflect on.", err=True)
        ctx.exit(1)

    threads, observations = load_session_for_reflection(config, session_id)
    batch = emit_reflection_batch(config, session_id, threads, observations)
    if limit:
        batch = batch[:limit]
    # Placeholder the agent fills in-place with its JSON candidate array per thread,
    # then feeds the SAME file to `ingest` — no id reconstruction needed.
    for it in batch:
        it["raw_response"] = ""
    payload = json.dumps(batch, indent=2)
    if output:
        output.write_text(payload, encoding="utf-8")
        click.echo(
            f"Emitted {len(batch)} thread prompt(s) for session {session_id[:16]} -> {output}",
            err=True,
        )
    else:
        click.echo(payload)


@reflect.command("ingest")
@click.argument(
    "responses_file", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.pass_context
def reflect_ingest(ctx, responses_file):
    """Ingest agent-produced reflection responses into knowledge_candidates.

    RESPONSES_FILE is a JSON array of {session_id, thread_id, prompt_hash,
    raw_response}, where thread_id is the value from the emit batch and
    raw_response is the JSON candidate array (as a string) the agent produced for
    that thread's emitted prompt. Pairs with `emit`.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)

    from techne.core.reflect import ingest_reflection_response

    items = json.loads(responses_file.read_text(encoding="utf-8"))
    totals = {
        "candidates_proposed": 0,
        "candidates_written": 0,
        "candidates_promoted": 0,
        "errors": 0,
    }
    skipped = 0
    for it in items:
        raw = it.get("raw_response")
        if not isinstance(raw, str):
            raw = json.dumps(raw) if raw else ""  # tolerate an inlined array
        if not raw.strip():
            skipped += 1  # unanswered thread (placeholder left empty) — not an error
            continue
        counts = ingest_reflection_response(
            config,
            it["session_id"],
            it["thread_id"],
            it.get("prompt_hash", ""),
            raw,
            model_used=it.get("model_used", "agent-assisted"),
        )
        for k, v in counts.items():
            totals[k] += v

    click.echo(json.dumps(totals, indent=2))
    if skipped:
        click.echo(f"skipped {skipped} thread(s) with empty raw_response")
    if config.reflect.write_to == "live":
        click.echo("write_to=live — candidates promoted straight to knowledge/identity.")
    else:
        click.echo("write_to=candidates — review with: techne reflect review")


@reflect.command("promote")
@click.argument("candidate_id", type=int)
@click.option("--by", "decided_by", default="human", help="Who/what decided (audit trail).")
@click.option("--reason", "decision_reason", default=None, help="Why (audit trail).")
@click.pass_context
def reflect_promote(ctx, candidate_id, decided_by, decision_reason):
    """Promote a pending candidate to the live knowledge or identity store."""
    config = ctx.obj["config"]
    from techne.core.reflect import promote_candidate

    try:
        new_id = promote_candidate(
            config.db_path, candidate_id,
            decided_by=decided_by, decision_reason=decision_reason,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    if new_id is not None:
        click.echo(f"Promoted candidate #{candidate_id} → knowledge.id={new_id}")
    else:
        click.echo(f"Promoted candidate #{candidate_id} → identity")


@reflect.command("discard")
@click.argument("candidate_id", type=int)
@click.option("--by", "decided_by", default="human", help="Who/what decided (audit trail).")
@click.option("--reason", "decision_reason", default=None, help="Why (audit trail).")
@click.pass_context
def reflect_discard(ctx, candidate_id, decided_by, decision_reason):
    """Discard a pending candidate. The row stays for audit; status flips to 'discarded'."""
    config = ctx.obj["config"]
    from techne.core.reflect import discard_candidate

    try:
        discard_candidate(
            config.db_path, candidate_id,
            decided_by=decided_by, decision_reason=decision_reason,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    click.echo(f"Discarded candidate #{candidate_id}")


@reflect.command("stats")
@click.pass_context
def reflect_stats(ctx):
    """Counts of pending / promoted / discarded candidates."""
    config = ctx.obj["config"]
    from techne.core.reflect import candidate_stats

    s = candidate_stats(config.db_path)
    click.echo(json.dumps(s, indent=2))


@main.command()
@click.option(
    "--runtime",
    type=click.Choice(["claude-code", "opencode"]),
    default="claude-code",
    show_default=True,
    help="Which runtime to spawn the fresh session on.",
)
@click.option(
    "--mode",
    type=click.Choice(["off-test", "pull", "differential"]),
    default="off-test",
    help=(
        "off-test: native+pull off (tests push). pull: native off, pull on "
        "(reachability). differential: native ON + native-only control (is techne the lead?)."
    ),
)
@click.option(
    "--model", default=None, help="Model for the spawned session (default: runtime default)."
)
@click.option("--timeout", default=240, show_default=True, help="Per-session timeout in seconds.")
@click.option("--keep", is_flag=True, help="Keep the canary row instead of deleting it after.")
@click.pass_context
def probe(ctx, runtime, mode, model, timeout, keep):
    """Run the differential-canary memory-attribution probe (techne#94).

    Seeds a techne-only canary, spawns a fresh session on the chosen runtime, and
    checks whether techne carried the codeword into context. `differential` also
    seeds a native-only control and leaves native memory ON. Exit 0 = pass, 1 = fail.

    Note: opencode has no native auto-memory, so `differential` there has no
    control and `off-test` already proves the only techne path (the plugin push).
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)

    from techne.probe import ClaudeCodeAdapter, OpencodeAdapter, run_differential, run_probe

    if runtime == "opencode":
        adapter = OpencodeAdapter(timeout=timeout, **({"model": model} if model else {}))
    else:
        adapter = ClaudeCodeAdapter(model=model, timeout=timeout)

    click.echo(
        f"Running probe (runtime={runtime}, mode={mode}) — spawning a fresh session, "
        "this can take a minute…"
    )
    if mode == "differential":
        verdict = run_differential(config, adapter, cleanup=not keep)
    else:
        verdict = run_probe(config, adapter, mode=mode, cleanup=not keep)
    click.echo(verdict.render())
    ctx.exit(0 if verdict.passed else 1)


@main.group(invoke_without_command=True)
@click.pass_context
def library(ctx):
    """Librarian document store — index + recall full documents (techne#91)."""
    if ctx.invoked_subcommand is not None:
        return
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return
    from techne.core.documents import list_documents
    from techne.core.librarian import librarian_db

    docs = list_documents(librarian_db(config), limit=50)
    click.echo(f"Documents: {len(docs)}")
    for d in docs[:25]:
        click.echo(f"  #{d['id']}  [{d['source_type']}]  {d.get('title') or d['source_url']}")


@library.command("index-transcripts")
@click.option(
    "--platform",
    type=click.Choice(["claude-code"]),
    default="claude-code",
    show_default=True,
    help="Which client's transcripts to index (each is a TranscriptSource adapter).",
)
@click.option(
    "--dir",
    "transcripts_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Transcript dir to index (default: the platform's default location).",
)
@click.option("--limit", type=int, default=None, help="Max transcripts to index.")
@click.pass_context
def library_index_transcripts(ctx, platform, transcripts_dir, limit):
    """Index a client's transcripts as documents — the conversation prose where
    decisions are made (the observation lens only sees tool calls)."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.transcripts import index_transcripts

    if platform == "claude-code":
        from techne.platforms.claude_code.transcript_library import ClaudeCodeTranscriptSource

        source = ClaudeCodeTranscriptSource(transcripts_dir)
    else:  # pragma: no cover - guarded by click.Choice
        raise click.ClickException(f"no transcript source for platform {platform!r}")

    counts = index_transcripts(config, source, limit=limit)
    click.echo(json.dumps(counts, indent=2))
    click.echo("Search them: techne library search <query>")


@library.command("index-markdown")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--source-type",
    default="markdown",
    show_default=True,
    help="source_type label for the documents (e.g. 'docs').",
)
@click.pass_context
def library_index_markdown(ctx, root, source_type):
    """Index every *.md under ROOT as Librarian documents (+ gist cards) — e.g. the
    repo's docs/. A second document source alongside transcripts."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.library import index_markdown_dir

    counts = index_markdown_dir(config, root, source_type=source_type)
    click.echo(json.dumps(counts, indent=2))
    click.echo("Search them: techne library search <query>")


@library.command("search")
@click.argument("query")
@click.option("--limit", default=20)
@click.pass_context
def library_search(ctx, query, limit):
    """Full-text search the Librarian's documents (librarian.db) — domain-scoped.
    Personal recall is the gist cards (techne search --table knowledge)."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.documents import search_documents
    from techne.core.librarian import librarian_db

    hits = search_documents(librarian_db(config), query, limit=limit)
    if not hits:
        click.echo("No documents matched.")
        return
    for d in hits:
        click.echo(f"  #{d['id']}  [{d['source_type']}]  {d.get('title') or d['source_url']}")
        snippet = (d.get("content", "") or "").replace("\n", " ").strip()
        click.echo(f"    {snippet[:200]}")
        click.echo(f"    ↳ full: techne library show {d['id']}")
        click.echo()


@library.command("migrate-store")
@click.pass_context
def library_migrate_store(ctx):
    """One-time: move documents from techne.db into librarian.db (db-per-domain #99).
    Preserves ids so existing cards resolve; verifies before dropping. Idempotent."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.librarian import migrate_documents_to_librarian

    result = migrate_documents_to_librarian(config)
    click.echo(json.dumps(result, indent=2))


@library.command("doctor")
@click.pass_context
def library_doctor(ctx):
    """Cross-db health check: report document cards whose pointer doesn't resolve in
    librarian.db (dangling) or whose `ver:` no longer matches the live document
    (stale). techne.db + librarian.db are a consistency set; restore them together."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.cards import find_dangling_cards

    report = find_dangling_cards(config)
    click.echo(
        f"{report['total']} document cards · "
        f"{len(report['dangling'])} dangling · {len(report['stale'])} stale"
    )
    for d in report["dangling"]:
        click.echo(f"  dangling  card #{d['card_id']}  {d['source']}  ({d.get('reason', '')})")
    for s in report["stale"]:
        click.echo(f"  stale     card #{s['card_id']}  {s['source']}  → doc #{s['doc_id']}")
    if report["dangling"] or report["stale"]:
        ctx.exit(1)  # non-zero so CI / scripts can gate on a clean bill


@library.command("show")
@click.argument("doc_id", type=int)
@click.option("--chars", default=4000, help="Max content chars to print (0 = all).")
@click.pass_context
def library_show(ctx, doc_id, chars):
    """Print a stored document's content (the fetch-on-demand half of recall)."""
    config = ctx.obj["config"]
    from techne.core.documents import get_document
    from techne.core.librarian import librarian_db

    doc = get_document(librarian_db(config), doc_id)
    if not doc:
        click.echo(f"No document #{doc_id}", err=True)
        ctx.exit(1)
    click.echo(f"# {doc.get('title') or '(untitled)'}  [{doc['source_type']}]  {doc['source_url']}")
    body = doc["content"]
    click.echo(body if chars == 0 else body[:chars])


@library.command("card")
@click.argument("card_id", type=int)
@click.option("--chars", default=2000, help="Max document chars to print (0 = all).")
@click.pass_context
def library_card(ctx, card_id, chars):
    """Show a document card's gist, then fetch its full document on demand (Option D)."""
    config = ctx.obj["config"]
    from techne.core.cards import CARD_TYPE, resolve_card
    from techne.core.database import connect

    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, content, source, tags FROM knowledge WHERE id = ? AND type = ?",
            (card_id, CARD_TYPE),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        click.echo(f"No document card #{card_id}", err=True)
        ctx.exit(1)
    click.echo(f"# card #{card_id}  →  {row['source']}")
    click.echo(f"gist: {row['content']}")
    click.echo(f"tags: {row['tags']}")
    doc = resolve_card(config, card_id)
    if doc:
        click.echo(f"\n--- full document #{doc['id']} [{doc['source_type']}] (on demand) ---")
        body = doc["content"]
        click.echo(body if chars == 0 else body[:chars])
    else:
        click.echo("\n(pointer did not resolve to a document)")


@library.group("cards")
def library_cards():
    """Document-card gist fill — the agent-assisted summarization pass (Option D)."""


@library_cards.command("emit")
@click.option("--limit", type=int, default=50, help="Max pending cards to emit.")
@click.option(
    "--source", default=None, help="Only cards whose pointer starts with this (e.g. 'docs:')."
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the batch JSON here (default: stdout).",
)
@click.pass_context
def library_cards_emit(ctx, limit, source, output):
    """Emit summarization prompts for pending cards — the agent writes each `summary`,
    then `cards ingest` feeds them back (no API key needed)."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.cards import emit_card_summary_batch

    batch = emit_card_summary_batch(config, limit=limit, source_prefix=source)
    payload = json.dumps(batch, indent=2)
    if output:
        output.write_text(payload, encoding="utf-8")
        click.echo(f"Emitted {len(batch)} card prompt(s) -> {output}", err=True)
    else:
        click.echo(payload)


@library_cards.command("ingest")
@click.argument("responses_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def library_cards_ingest(ctx, responses_file):
    """Fill cards from agent-written gists. RESPONSES_FILE is the emit batch with
    each `summary` filled in."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.cards import ingest_card_summaries

    items = json.loads(responses_file.read_text(encoding="utf-8"))
    counts = ingest_card_summaries(config.db_path, items)
    click.echo(json.dumps(counts, indent=2))


@library_cards.command("gist-transcripts")
@click.pass_context
def library_cards_gist_transcripts(ctx):
    """(Re)generate transcript card gists as a thread index (labels + timeframe).
    The honest gist for a long multi-topic session — deterministic, no prose."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.transcripts import fill_transcript_gists

    counts = fill_transcript_gists(config.db_path)
    click.echo(json.dumps(counts, indent=2))


@library_cards.command("list")
@click.option("--limit", type=int, default=50)
@click.pass_context
def library_cards_list(ctx, limit):
    """List cards still awaiting a gist (pending)."""
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.core.cards import pending_cards

    rows = pending_cards(config.db_path, limit=limit)
    click.echo(f"Pending cards: {len(rows)}")
    for r in rows[:25]:
        click.echo(f"  #{r['id']}  {r['content'][:70]}")


@main.group(invoke_without_command=True)
@click.pass_context
def architect(ctx):
    """Pattern store (architect.db) — the architectural-patterns corpus (techne#87)."""
    if ctx.invoked_subcommand is not None:
        return
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return
    from techne.core.architect import architect_db
    from techne.core.patterns import stats

    s = stats(architect_db(config))
    click.echo(
        f"Patterns: {s['patterns']}  Investigations: {s['investigations']}  Skips: {s['skips']}"
    )


@architect.command("ingest")
@click.argument("markdown_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def architect_ingest(ctx, markdown_file):
    """Seed architect.db from an architectural-patterns.md corpus."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import ingest_patterns_md

    counts = ingest_patterns_md(
        architect_db(config), markdown_file.read_text(encoding="utf-8")
    )
    click.echo(json.dumps(counts, indent=2))


@architect.command("export")
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the markdown export here (default: stdout).",
)
@click.pass_context
def architect_export(ctx, output):
    """Regenerate a shareable architectural-patterns.md from architect.db."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import export_patterns_md

    md = export_patterns_md(architect_db(config))
    if output:
        output.write_text(md, encoding="utf-8")
        click.echo(f"Exported to {output} ({md.count(chr(10))} lines)", err=True)
    else:
        click.echo(md)


@architect.command("search")
@click.argument("query")
@click.option("--limit", default=20)
@click.pass_context
def architect_search(ctx, query, limit):
    """Full-text search the pattern corpus (name + body)."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import search_patterns

    hits = search_patterns(architect_db(config), query, limit=limit)
    if not hits:
        click.echo("No patterns matched.")
        return
    for h in hits:
        click.echo(f"  {h['id']}  {h['name']}")
        click.echo(f"    {h['source_url']}")
        click.echo(f"    {(h['body'][:160] or '').strip()}")
        click.echo()


@architect.command("show")
@click.argument("pattern_id")
@click.pass_context
def architect_show(ctx, pattern_id):
    """Print a pattern's full body + which investigation it came from."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import get_pattern

    p = get_pattern(architect_db(config), pattern_id)
    if not p:
        click.echo(f"No pattern {pattern_id}", err=True)
        ctx.exit(1)
    click.echo(f"# {p['name']}  [{p['source_url']}]")
    click.echo(p["body"])


@architect.command("related")
@click.argument("pattern_id")
@click.pass_context
def architect_related(ctx, pattern_id):
    """Show the patterns linked to/from PATTERN_ID via [[wikilink]] edges."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import related_patterns

    rel = related_patterns(architect_db(config), pattern_id)
    if not rel["outbound"] and not rel["inbound"]:
        click.echo("No relations.")
        return
    if rel["outbound"]:
        click.echo("→ outbound:")
        for r in rel["outbound"]:
            if r["resolution_status"] == "resolved":
                click.echo(f"  [{r['kind']}] {r['dst_pattern_id']}  {r['dst_pattern_name']}")
            else:
                click.echo(f"  [{r['kind']}] (unresolved) {r['dst_name']}")
    if rel["inbound"]:
        click.echo("← inbound:")
        for r in rel["inbound"]:
            click.echo(f"  [{r['kind']}] {r['src_pattern_id']}  {r['src_pattern_name']}")


@architect.command("applies")
@click.argument("target")
@click.option("--limit", default=100)
@click.pass_context
def architect_applies(ctx, target, limit):
    """List patterns tagged as applying to TARGET (techne|cabal|limenal|agent-infra)."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import patterns_for_target

    hits = patterns_for_target(architect_db(config), target, limit=limit)
    if not hits:
        click.echo(f"No patterns apply to {target!r}.")
        return
    for h in hits:
        click.echo(f"  {h['id']}  {h['name']}")


@architect.command("backfill")
@click.pass_context
def architect_backfill(ctx):
    """Re-derive the relation/applies graph from pattern bodies (idempotent)."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import backfill_graph

    counts = backfill_graph(architect_db(config))
    click.echo(json.dumps(counts, indent=2))


@architect.command("retire-legacy")
@click.option("--dry-run", is_flag=True, help="Report the count without deleting.")
@click.pass_context
def architect_retire_legacy(ctx, dry_run):
    """Delete the superseded type='pattern' rows from techne.db (now owned by
    architect.db). They otherwise leak team patterns into personal recall."""
    config = ctx.obj["config"]
    from techne.core.patterns import retire_legacy_pattern_knowledge

    n = retire_legacy_pattern_knowledge(config.db_path, dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    click.echo(f"{verb} {n} legacy type='pattern' knowledge rows from techne.db")


@main.group(invoke_without_command=True)
@click.pass_context
def codex(ctx):
    """Code-graph store (codex.db) — symbols + edges, graph queries (techne#89).

    EXPERIMENTAL (techne#89): indexing is manual — the graph can go stale as code
    changes; re-run `codex index` to refresh.
    """
    if ctx.invoked_subcommand is not None:
        return
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return
    from techne.core.codegraph import stats
    from techne.core.codex import codex_db

    s = stats(codex_db(config))
    click.echo(f"Symbols: {s['symbols']}  Edges: {s['edges']}  Files: {s['files']}")


@codex.command("index")
@click.argument("repo", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--language",
    "languages",
    default="python,typescript",
    show_default=True,
    help="Comma-separated list of languages to index (python,typescript).",
)
@click.pass_context
def codex_index(ctx, repo, languages):
    """Walk REPO and index Python + TypeScript symbols into codex.db.

    Uses tree-sitter for parsing; requires `pip install techne[codex]`.
    Incremental: files whose SHA-256 hash is unchanged are skipped.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.codex.source import RepoSource
    from techne.core.codex import codex_db

    lang_list = [lang.strip() for lang in languages.split(",") if lang.strip()]
    db_path = codex_db(config)
    src = RepoSource(db_path, repo, languages=lang_list)
    count = src.initial_walk()
    from techne.core.codegraph import stats

    s = stats(db_path)
    click.echo(f"Indexed: {count} symbols from {repo}")
    click.echo(f"Totals:  {s['symbols']} symbols  {s['edges']} edges  {s['files']} files")


@codex.command("stats")
@click.pass_context
def codex_stats(ctx):
    """Show codex.db symbol/edge/file counts."""
    config = ctx.obj["config"]
    from techne.core.codegraph import stats
    from techne.core.codex import codex_db

    s = stats(codex_db(config))
    click.echo(json.dumps(s, indent=2))


@codex.command("search")
@click.argument("query")
@click.option("--limit", default=20)
@click.pass_context
def codex_search(ctx, query, limit):
    """FTS over symbol names + signatures."""
    config = ctx.obj["config"]
    from techne.core.codegraph import annotate_freshness, search_symbols
    from techne.core.codex import codex_db

    db_path = codex_db(config)
    results = annotate_freshness(db_path, search_symbols(db_path, query, limit=limit))
    for s in results:
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        click.echo(f"  {s['id']}  [{s['kind']}] {s['name']}  {s.get('signature') or ''}{marker}")
        click.echo(f"    {s['file']}")


@codex.command("callers")
@click.argument("symbol_id")
@click.pass_context
def codex_callers(ctx, symbol_id):
    """Who calls this symbol (direct)."""
    config = ctx.obj["config"]
    from techne.core.codegraph import annotate_freshness, callers
    from techne.core.codex import codex_db

    db_path = codex_db(config)
    for s in annotate_freshness(db_path, callers(db_path, symbol_id)):
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        click.echo(f"  {s['id']}  {s['name']}  ({s['file']}){marker}")


@codex.command("blast-radius")
@click.argument("symbol_id")
@click.option("--direction", type=click.Choice(["out", "in"]), default="out", show_default=True)
@click.option("--depth", default=5, show_default=True)
@click.pass_context
def codex_blast(ctx, symbol_id, direction, depth):
    """Transitive reachability via recursive CTE — out=dependencies, in=dependents."""
    config = ctx.obj["config"]
    from techne.core.codegraph import annotate_freshness, reachable
    from techne.core.codex import codex_db

    db_path = codex_db(config)
    raw = reachable(db_path, symbol_id, direction=direction, max_depth=depth)
    rows = annotate_freshness(db_path, raw)
    if not rows:
        click.echo("(nothing reachable)")
        return
    for s in rows:
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        click.echo(f"  d{s['depth']}  {s['name']}  [{s['kind']}]  ({s['file']}){marker}")


@codex.command("watch")
@click.argument("repo", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--language",
    "languages",
    default="python,typescript",
    show_default=True,
    help="Comma-separated list of languages to watch (python,typescript).",
)
@click.option(
    "--debounce-ms",
    default=300,
    show_default=True,
    help="Debounce interval in milliseconds for file-change events.",
)
@click.pass_context
def codex_watch(ctx, repo, languages, debounce_ms):
    """Watch REPO for file changes and re-index incrementally (Ctrl-C to stop).

    Runs an initial index first (fresh baseline + persists repo_root), then
    starts the watchdog observer. Requires `pip install techne[codex]` for
    watchdog support.
    """
    import time

    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.codex.source import RepoSource
    from techne.core.codegraph import stats
    from techne.core.codex import codex_db

    lang_list = [lang.strip() for lang in languages.split(",") if lang.strip()]
    db_path = codex_db(config)
    src = RepoSource(db_path, repo, languages=lang_list, debounce_ms=debounce_ms)

    click.echo(f"indexing {repo} ...")
    count = src.initial_walk()
    s = stats(db_path)
    click.echo(f"indexed: {count} new symbols  ({s['symbols']} total, {s['edges']} edges)")

    try:
        src.start_watch()
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(1)

    click.echo(f"watching {repo} (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        src.stop_watch()
    click.echo("watch stopped")


# ---------------------------------------------------------------------------
# Evidence gate (techne#105) — read-only view over the observation ledger
# ---------------------------------------------------------------------------


@main.command("evidence")
@click.argument("session_id")
@click.option(
    "--since",
    default=None,
    help="ISO-8601 timestamp; only observations after this time are included.",
)
@click.pass_context
def evidence_cmd(ctx, session_id, since):
    """Show what an agent actually did in a session window.

    Queries the observation ledger and prints the evidence view: any tool
    calls made, files written/edited, git commits found, bash actions run,
    and test results parsed from output.  Useful for humans and CI to eyeball
    what an agent did before trusting its claims.

    SESSION_ID is the techne session identifier to inspect.
    """
    from techne.core.evidence import query_turn_evidence

    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        raise SystemExit(1)

    ev = query_turn_evidence(
        config.db_path, session_id, since_iso=since,
        bash_captured=config.hooks.capture_bash,
    )

    click.echo(f"Session: {session_id}")
    if since:
        click.echo(f"Since:   {since}")
    click.echo(f"Observations: {ev.observation_count}")
    click.echo(f"Any tool call: {ev.any_tool_call}")
    if not config.hooks.capture_bash:
        click.echo(
            "NOTE: Bash capture is OFF (config.hooks.capture_bash=False) — commits, "
            "actions, and test results are not recorded and will show empty; enable "
            "capture_bash to verify those.",
        )
    click.echo()

    if ev.artifacts:
        click.echo(f"Artifacts ({len(ev.artifacts)}):")
        for a in ev.artifacts:
            click.echo(f"  {a}")
    else:
        click.echo("Artifacts: (none)")

    if ev.commits:
        click.echo(f"Commits ({len(ev.commits)}):")
        for c in ev.commits:
            click.echo(f"  {c}")
    else:
        click.echo("Commits: (none)")

    click.echo(f"Actions: {len(ev.actions)}")
    for a in ev.actions[:3]:
        click.echo(f"  $ {a}")
    if len(ev.actions) > 3:
        click.echo(f"  ... ({len(ev.actions) - 3} more)")

    t = ev.tests
    if t.suite_green is None:
        tests_str = "unparsed (no test output found)"
    else:
        parts = []
        if t.passed is not None:
            parts.append(f"passed={t.passed}")
        if t.failed is not None:
            parts.append(f"failed={t.failed}")
        if t.errors is not None:
            parts.append(f"errors={t.errors}")
        tests_str = f"{'GREEN' if t.suite_green else 'RED'} ({', '.join(parts)})"
    click.echo(f"Tests: {tests_str}")


# ---------------------------------------------------------------------------
# Cross-DB maintenance: fsck + all-DBs-together backup/restore (techne#139)
# ---------------------------------------------------------------------------


@main.command("fsck")
@click.pass_context
def fsck_cmd(ctx):
    """Run every cross-DB consistency check (dangling cards, …) as one pass.

    Exits non-zero if any inconsistency is found, so it's usable in scripts/CI.
    """
    from techne.core.maintenance import run_fsck

    config = ctx.obj["config"]
    report = run_fsck(config)

    present = ", ".join(d for d, p in report.db_present.items() if p) or "(none)"
    click.echo(f"databases present: {present}")
    for c in report.checks:
        click.echo(f"  [{'OK  ' if c.ok else 'FAIL'}] {c.name}: {c.summary}")
    if report.ok:
        click.echo("fsck: all cross-DB checks passed.")
    else:
        click.echo("fsck: inconsistencies found.", err=True)
        raise SystemExit(1)


@main.command("reconcile")
@click.pass_context
def reconcile_cmd(ctx):
    """Force the schema drift-reconcile passes on techne.db (re-add a dropped
    index/trigger/column, fix FTS desync).

    connect()-time reconcile is gated to skip an already-current, already-reconciled
    db (techne#140 — the hot-path optimisation); this re-runs the passes
    unconditionally and re-stamps the sentinel. Use it if you suspect schema drift.
    """
    from techne.core.database import force_reconcile

    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        raise SystemExit(1)
    force_reconcile(config.db_path)
    click.echo(f"reconcile: drift-reconcile passes re-run on {config.db_path}")


@main.command("backup")
@click.option("--out", default=None, help="Destination dir (default: <data_dir>/backups/)")
@click.pass_context
def backup_cmd(ctx, out):
    """Snapshot ALL present domain DBs together into one timestamped set.

    Cross-DB invariants span files, so a single-DB backup desyncs pointers — always back up
    (and restore) the whole set. Each DB is copied via the online backup API (consistent
    even under concurrent writes); absent domains are skipped.
    """
    from pathlib import Path as _Path

    from techne.core.maintenance import backup_all_dbs

    config = ctx.obj["config"]
    res = backup_all_dbs(config, _Path(out) if out else None)
    click.echo(f"backed up [{', '.join(res.backed_up) or '(none)'}] → {res.dest}")
    if res.skipped_absent:
        click.echo(f"skipped (absent): {', '.join(res.skipped_absent)}")


@main.command("restore")
@click.argument("backup_dir")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option("--no-pre-backup", is_flag=True, help="Don't snapshot the current DBs first")
@click.pass_context
def restore_cmd(ctx, backup_dir, yes, no_pre_backup):
    """Restore ALL domain DBs from a backup set, overwriting the live DBs as a unit.

    The set is validated before any live file is touched, and the CURRENT state is
    snapshotted first (unless --no-pre-backup) so the restore is itself reversible. Ensure no
    other techne process is running against these DBs.
    """
    from pathlib import Path as _Path

    from techne.core.maintenance import RestoreError, read_manifest, restore_all_dbs

    config = ctx.obj["config"]
    src = _Path(backup_dir)
    try:
        manifest = read_manifest(src)  # validate the whole set BEFORE prompting/touching
    except RestoreError as e:
        click.echo(f"restore aborted: {e}", err=True)
        raise SystemExit(1) from e

    dbs = ", ".join(manifest["databases"].keys())
    click.echo(
        f"This OVERWRITES the live DBs [{dbs}] from {src} "
        f"(set created {manifest.get('created_at', '?')})."
    )
    if not yes and not click.confirm("Proceed?"):
        click.echo("restore cancelled.")
        return
    try:
        res = restore_all_dbs(config, src, pre_restore_backup=not no_pre_backup)
    except RestoreError as e:
        click.echo(f"restore aborted: {e}", err=True)
        raise SystemExit(1) from e
    click.echo(f"restored: [{', '.join(res.restored)}]")
    if res.skipped_unknown:
        click.echo(f"skipped (domains not in this techne): {', '.join(res.skipped_unknown)}")
    if res.pre_restore_backup:
        click.echo(f"prior state snapshotted at: {res.pre_restore_backup}")


if __name__ == "__main__":
    main()
