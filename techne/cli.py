"""techne CLI — human interface for inspecting and managing agent memory."""

import json
from pathlib import Path

import click

from techne import __version__
from techne.config import DEFAULT_CONFIG, PLATFORMS, Config


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
    from techne.core.state import db_stats, get_active_session

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    stats = db_stats(config.db_path)
    session = get_active_session(config.db_path)

    click.echo(f"Database: {config.db_path}")
    click.echo(f"Platform: {config.platform}")
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
    "--table", type=click.Choice(["all", "threads", "observations", "knowledge"]), default="all"
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
@click.pass_context
def init(ctx, platform, identity_dir):
    """Initialize techne — create database, register hooks, set up MCP."""
    from techne.core.database import init_db

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

    # Save config
    config.save()
    click.echo(f"Config saved: {DEFAULT_CONFIG}")

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


@main.command("import-patterns")
@click.argument("markdown_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    help="Parse the markdown and print what would be imported; do not write to the corpus.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Import even when a knowledge entry with the same source URL already exists. "
    "Otherwise duplicates are skipped with a log line.",
)
@click.pass_context
def import_patterns(ctx, markdown_file: Path, dry_run: bool, force: bool):
    """Import an architectural-patterns markdown corpus into techne knowledge.

    The expected markdown shape is the one this repo's
    architectural-patterns memory file uses: `## <Pattern Name>` sections
    with `- **Source**:` and `- **Surveyed**:` preambles, plus a final
    `## Surveyed (no patterns worth saving)` bullet list.

    Each pattern lands as a knowledge entry with type=pattern and source
    set to its URL. No-lift bullets land with confidence=low and tag
    `surveyed-no-lift` so the corpus still remembers we surveyed them.
    """
    from techne.import_md import import_into, parse

    config = Config.load()
    text = markdown_file.read_text(encoding="utf-8")
    parsed = parse(text)

    click.echo(f"Parsed: {len(parsed.patterns)} patterns, {len(parsed.no_lifts)} no-lift entries")

    result = import_into(config.db_path, parsed, dry_run=dry_run, force=force)

    verb = "would add" if dry_run else "added"
    skip_verb = "would skip" if dry_run else "skipped"
    click.echo(
        f"{verb}: {result.patterns_added} patterns, {result.no_lifts_added} no-lifts; "
        f"{skip_verb} (already present): {result.patterns_skipped_existing} patterns, "
        f"{result.no_lifts_skipped_existing} no-lifts"
    )
    if dry_run:
        click.echo("\n[dry-run] no changes written. Re-run without --dry-run to import.")


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


@reflect.command("promote")
@click.argument("candidate_id", type=int)
@click.pass_context
def reflect_promote(ctx, candidate_id):
    """Promote a pending candidate to the live knowledge or identity store."""
    config = ctx.obj["config"]
    from techne.core.reflect import promote_candidate

    try:
        new_id = promote_candidate(config.db_path, candidate_id)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    if new_id is not None:
        click.echo(f"Promoted candidate #{candidate_id} → knowledge.id={new_id}")
    else:
        click.echo(f"Promoted candidate #{candidate_id} → identity")


@reflect.command("discard")
@click.argument("candidate_id", type=int)
@click.pass_context
def reflect_discard(ctx, candidate_id):
    """Discard a pending candidate. The row stays for audit; status flips to 'discarded'."""
    config = ctx.obj["config"]
    from techne.core.reflect import discard_candidate

    try:
        discard_candidate(config.db_path, candidate_id)
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


if __name__ == "__main__":
    main()
