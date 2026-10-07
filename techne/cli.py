"""techne CLI — human interface for inspecting and managing agent memory."""

import json
from pathlib import Path

import click

from techne import __version__
from techne.config import CLAUDE_CODE, PLATFORMS, Config, PostureConfig, techne_home


class _AliasedGroup(click.Group):
    """Resolve legacy command names WITHOUT listing them twice in `--help`.

    techne#744. The 0.16.0 board's DONE MEANS is that an operator can address the
    archivist, librarian, architect and codex BY NAME. Three of the four already
    could; the librarian was `techne library` — and `library` is the STORAGE word,
    naming the document store, which is exactly the layout framing this release
    demotes the four names out of.

    An alias rather than a rename because every existing script, `docs/librarian-guide.md`
    and the transcript-command tests say `library`, and breaking them to teach a word
    is a bad trade. Resolved in `get_command` rather than registered a second time with
    `add_command`, because a second registration puts BOTH spellings in `--help` — and a
    help listing that offers two words for one domain teaches the ambiguity this issue
    exists to remove.
    """

    #: legacy spelling -> canonical domain name. Keep this SMALL; it is a compatibility
    #: shim, not a synonym system. Every entry is a word an operator might already have
    #: in a script, not a word we would choose today.
    ALIASES = {"library": "librarian"}

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self.ALIASES.get(cmd_name, cmd_name))

    def resolve_command(self, ctx, args):
        # Report the CANONICAL name, not the alias the operator typed. click's default
        # echoes back what was typed, so `techne library --badopt` would print usage for
        # a command name that does not appear in `--help` — which is worse than either
        # spelling alone.
        #
        # NARROW ON PURPOSE, BOTH WAYS (Prudence, #745 r1 Important 1). An earlier form
        # returned `cmd.name` unconditionally and CRASHED: click's `resolve_command`
        # ends `return cmd_name if cmd else None, cmd, args[1:]`, and `cmd` is None
        # whenever `ctx.resilient_parsing` is set — the NoSuchCommand raise is guarded
        # by `and not ctx.resilient_parsing`. So shell completion over an unknown word
        # died on `None.name`, and `_resolve_context`'s own `if cmd is None` handling
        # never ran because it checks AFTER the call. Reproduced against click 8.4.2:
        #     main.resolve_command(Context(main, resilient_parsing=True), ["bogus"])
        #     -> AttributeError: 'NoneType' object has no attribute 'name'
        # techne neither documents nor installs completion, which is why nothing caught
        # it — but the standard click one-liner reaches it, and this override turned a
        # working path into a crashing one.
        #
        # Gating on `name in ALIASES` also keeps the rewrite off every OTHER command,
        # which is a blast radius the feature never needed.
        name, cmd, rest = super().resolve_command(ctx, args)
        if cmd is not None and name in self.ALIASES:
            name = cmd.name
        return name, cmd, rest


def _version_string() -> str:
    """Version, plus the build identity when there is one (techne#965).

    `techne --version` printed `0.16.1` on a build installed from a sha carrying
    seven merged PRs, two schema migrations and new CLI surface -- because
    `__version__` names the last RELEASE, not the code. We tell external
    consumers to install from a sha, so this is the one surface that has to
    answer "which build am I on" for them, and it could not.

    `0.16.1+21813c1` borrows PEP 440's LOCAL LABEL shape -- sortable and
    unmistakably not a release -- but THE RETURNED STRING IS NOT A PEP 440
    VERSION and must not be fed to a parser. It carries a ` (checkout)` /
    ` (installed)` suffix, which `packaging.version.Version` rejects. That is a
    deliberate trade: this is a human-facing line, and the build word is worth
    more here than machine-parseability. Nothing in this repo parses it.

    THE DEGRADED VERSION IS GUARDED, because it already contains a local label:
    `_resolve_version` returns `0.0.0+unknown` when it cannot read a version, and
    appending a second `+` produces `0.0.0+unknown+21813c1`, which is illegal
    even as a local label. When the version already carries one, the revision is
    shown separately rather than concatenated.

    `build` disambiguates the two ways a revision can be absent: a source tree
    that is NOT a checkout (vendored, extracted sdist -- no readable .git or
    HEAD), versus a wheel with no VCS provenance. An editable checkout is not one
    of them; it reads HEAD like any other checkout and prints its sha.
    """
    # Derived in core/health.py so the MCP handshake gets the SAME string, from
    # the same RESOLVED package path the banner uses (techne#982).
    from techne.core.health import build_identity

    return build_identity(__version__)


@click.group(cls=_AliasedGroup)
@click.version_option(version=_version_string())
@click.pass_context
def main(ctx):
    """techne — agentic memory and knowledge system."""
    ctx.ensure_object(dict)
    # `setdefault`, NOT assignment (techne#766). `ensure_object` creates the dict when the
    # caller did not supply one, which reads as "honour what was passed in" — and the next
    # line then threw it away, so a Config injected via `CliRunner(..., obj={"config": cfg})`
    # was SILENTLY DISCARDED and the command ran against the ambient store instead.
    #
    # Found the hard way: a reviewer believed they were scoped to a temp store, and four
    # `techne backup` runs went against a live one under a mutation setting `--keep 1`.
    # Nothing was lost that time. The failure is this repo's recurring shape — two states
    # ("scoped" and "not") sharing one observable, since the command runs and exits 0
    # either way — and it points at `backup` and `restore`, which write and delete.
    ctx.obj.setdefault("config", Config.load())


@main.group(invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw report.")
@click.pass_context
def capabilities(ctx, as_json):
    """What techne can do here, and what is switched off (techne#638).

    The operator-facing twin of the techne_capabilities MCP tool. Same report;
    this one renders it, the agent narrates it. Exists so "what is off on this
    box" is answerable without an agent in the loop.
    """
    if ctx.invoked_subcommand is not None:
        return
    import json as _json

    from techne.core.capabilities import capability_report

    report = capability_report(ctx.obj["config"])
    if as_json:
        click.echo(_json.dumps(report, indent=2, default=str))
        return

    store = report["store"]
    click.echo(f"techne {report['version']}")
    click.echo(
        f"  {store['observations']} observations · {store['knowledge']} knowledge · "
        f"{store['sessions']} sessions · {store['identity']} identity"
    )
    if store["codex_repos"]:
        click.echo(
            f"  {store['codex_symbols']} symbols indexed across {store['codex_repos']} repo(s)"
        )
    click.echo(f"  search: {store['vectors']}")
    click.echo("")

    on = [c for c in report["capabilities"] if c["on"]]
    off = [c for c in report["capabilities"] if not c["on"] and not c["acknowledged"]]
    acked = [c for c in report["capabilities"] if not c["on"] and c["acknowledged"]]
    if on:
        click.echo(f"ON ({len(on)})")
        for c in on:
            click.echo(f"  ✓ {c['flag']}  — {c['title']}")
        click.echo("")
    if acked:
        click.echo(f"OFF ON PURPOSE ({len(acked)})  — undo with: techne capabilities unack <flag>")
        for c in acked:
            reason = c["acknowledged"].get("reason")
            click.echo(f"  - {c['flag']}  — {c['title']}" + (f"  ({reason})" if reason else ""))
        click.echo("")
    if off:
        click.echo(f"OFF ({len(off)})")
        for c in off:
            click.echo(f"  ✗ {c['flag']}  — {c['title']}")
            click.echo(f"      {c['buys']}")
            if c["evidence"]:
                click.echo(f"      on this install: {c['evidence']}")
            click.echo("      to enable:")
            for line in c["enable"].splitlines():
                click.echo(f"        {line}")
        click.echo("")

    click.echo("Bundled workflows")
    for w in report["workflows"]:
        mark = "✓" if w["state"] == "current" else "✗"
        detail = f"  ({w['detail']})" if w["detail"] else ""
        click.echo(f"  {mark} {w['name']}: {w['state']}{detail}")
    if report["install_hint"]:
        click.echo(f"\n{report['install_hint']}")
    if off:
        click.echo("\nLeaving one off on purpose? techne capabilities ack <flag>")


def _capability_flags() -> list[str]:
    from techne.core.capabilities import CAPABILITIES

    return [c.flag for c in CAPABILITIES]


@capabilities.command("ack")
@click.argument("flag", type=click.Choice(_capability_flags()))
@click.option("--reason", default="", help="Why it stays off, kept with the acknowledgement.")
@click.pass_context
def capabilities_ack(ctx, flag, reason):
    """Mark FLAG as deliberately off (techne#1022).

    It stays in the report, marked as your choice, and stops counting toward the
    session-start invitation. It does not expire; `unack` reverses it.
    """
    from techne.core.capabilities import acknowledge
    from techne.core.database import connect

    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return
    conn = connect(config.db_path)
    try:
        acknowledge(conn, config, flag, reason)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    click.echo(f"{flag}: acknowledged as deliberately off. Undo: techne capabilities unack {flag}")


@capabilities.command("unack")
@click.argument("flag", type=click.Choice(_capability_flags()))
@click.pass_context
def capabilities_unack(ctx, flag):
    """Clear FLAG's acknowledgement, so it counts as off again."""
    from techne.core.capabilities import clear_acknowledgement
    from techne.core.database import connect

    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return
    conn = connect(config.db_path)
    try:
        cleared = clear_acknowledgement(conn, flag)
    finally:
        conn.close()
    click.echo(f"{flag}: " + ("acknowledgement cleared." if cleared else "was not acknowledged."))


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

    # Hook-registration drift (claude-code only): a hooks.* flag can be enabled in
    # config.yaml but never written to settings.json (init not re-run) — a silent
    # no-op the user has no other way to notice. Surface it here (techne#287).
    if config.platform == CLAUDE_CODE:
        from techne.core.checks import init_remedy
        from techne.platforms.claude_code import check_hook_drift

        drift = check_hook_drift(config)
        if drift:
            click.echo()
            # Pin --posture: it defaults to `auto`, which RE-DETECTS CC memory on
            # every re-init, so a bare `techne init` can change posture (and whether
            # reflect is advisory) as a side effect of fixing hook registration.
            # Same reasoning as the session-start banner (techne#452).
            # Same two-heading split as the session-start banner: "enabled in config
            # but not registered" is a precise claim and is NOT what an unusable
            # settings.json means — there we cannot tell. The banner got this
            # treatment; status did not, leaving the same small lie on the other
            # surface. Asks the CONDITION, never greps the prose. (Prudence, PR #457.)
            from techne.platforms.claude_code import settings_unusable_reason

            if settings_unusable_reason(config) is not None:
                from techne.platforms.claude_code.hooks import SETTINGS_PATH

                click.echo(
                    "⚠ settings.json unusable — hook registration cannot be verified; "
                    "every techne hook may be silently not firing:"
                )
                click.echo(
                    f"  Repair or move aside {SETTINGS_PATH}, then run '{init_remedy(config)}'."
                )
            else:
                click.echo(f"⚠ Hook registration drift — run '{init_remedy(config)}' to reconcile:")
            for w in drift:
                click.echo(f"  {w}")


# Tables whose rows must be unique by a logical key. A table here with
# COUNT(*) > COUNT(DISTINCT key) is silently accumulating duplicate rows — a
# class of bug with no functional symptom (queries still work), invisible to
# behavior tests and code review, only visible as storage growth. thread_boundaries
# hit exactly this (recompute-and-reinsert per turn without a dedup guard). Add a
# (table, key-columns) pair here whenever a table gains a uniqueness invariant so
# the audit catches regressions instead of us noticing the DB got fat. Only list
# tables with an UNCONDITIONAL uniqueness invariant — partial/scoped uniqueness
# (e.g. knowledge_candidates' WHERE origin='reflect') would false-positive here.
_DEDUP_CHECKS = [
    ("thread_boundaries", ("session_id", "obs_index_a", "obs_index_b")),
]


#: `techne search` prints this much of a row, and keeps at most this much of a
#: windowed row's first line, so a long head cannot crowd out the match (#1162).
_PREVIEW_CHARS = 200
_PREVIEW_HEAD = 60
#: How much of a windowed row to show before its matched span.
_PREVIEW_LEAD = 40


def _preview(row: dict) -> str:
    """A row's content cut to `_PREVIEW_CHARS`, showing the match when search
    windowed it (techne#1162).

    A windowed observation (`content_head`, `content_window`, `content_span` from
    `search._window_matched_row`) reads `<first line> … <window>` at 500 chars,
    and the span can sit anywhere in the window: near a row's end the window is
    pulled back to fill the width. A plain 200-char cut then shows the head and
    not the match. So the preview keeps up to `_PREVIEW_HEAD` chars of the first
    line, then the separator, then the window from `_PREVIEW_LEAD` chars before
    the span. Any other row is a plain cut, as before.
    """
    content = row.get("content", "") or ""
    head = row.get("content_head")
    if head is None or "content_window" not in row or "content_span" not in row:
        return content[:_PREVIEW_CHARS]
    from techne.core.search import _WINDOW_SEP

    lo = row["content_window"][0]
    window = content[head + len(_WINDOW_SEP) :]
    shown = content[: min(head, _PREVIEW_HEAD)]
    budget = _PREVIEW_CHARS - len(shown) - len(_WINDOW_SEP)
    at = max(0, row["content_span"][0] - lo - _PREVIEW_LEAD)
    return shown + _WINDOW_SEP + window[at : at + budget]


def _render_liveness(config) -> tuple[int, int]:
    """Print the subsystem-liveness table. Returns (failing checks, drift failures).

    TWO counts, not one. They answer different questions and must not be added
    together: the checks ask "did this rail RUN?", drift asks "is it even WIRED?".
    Folding drift into the first made a single run print, seven lines apart,
    "Nothing failing, but 0 of 12 subsystems confirmed live" and "5 subsystem(s)
    not confirmed working" — zero subsystems were failing, five WARNINGS were.
    That is the honesty rule below ("NEVER claim more than was observed") leaking
    through a different door. (#469 review 6, issue 4.)

    Separate from the storage checks below because they answer different
    questions: those ask "is the store internally consistent?", this asks "did
    each subsystem actually RUN?". On 2026-08-03 `techne doctor` printed "All
    checks passed" while consolidate and dream had been cold for four weeks —
    a green health check that did not check the things that break.
    """
    from techne.core.checks import Verdict, init_remedy, run_checks

    results = run_checks(config)
    if not results:
        click.echo("No subsystem checks registered.")
        return 0, 0

    marks = {
        Verdict.LIVE: "✓",
        Verdict.DORMANT: "·",
        Verdict.STALE: "!",
        Verdict.UNOBSERVABLE: "?",
        Verdict.BROKEN: "✗",
        Verdict.UNKNOWN: "?",
    }
    click.echo("\nSubsystem liveness (techne#468):\n")
    width = max(len(r.name) for r in results)
    failing = 0
    for r in sorted(results, key=lambda r: (r.ok, r.subsystem, r.name)):
        if not r.ok:
            failing += 1
        click.echo(f"  {marks[r.verdict]} {r.name:<{width}}  {r.verdict.value:<12} {r.detail}")
        if r.remedy:
            click.echo(f"    {' ' * width}    remedy: {r.remedy}")

    if failing:
        # UNOBSERVABLE counts as failing by decision (techne#468): "if we built a
        # rail, shouldn't it have something to tell us?" A rail that cannot
        # report is unfinished, so it must never render as passing.
        click.echo(f"\n{failing} subsystem(s) not confirmed working.")
    else:
        # NEVER claim more than was observed. `failing == 0` is not the same
        # claim as "everything is live" — on a store where every check is
        # DORMANT this previously printed "All subsystems confirmed live",
        # confirming the absence of evidence (#469 review, finding 1).
        live = sum(1 for r in results if r.verdict is Verdict.LIVE)
        dormant = len(results) - live
        if live == 0:
            click.echo(f"\nNothing failing, but 0 of {len(results)} subsystems confirmed live.")
        elif dormant:
            click.echo(f"\n{live} live, {dormant} dormant — none failing.")
        else:
            click.echo(f"\nAll {live} subsystems confirmed live.")

    # REGISTRATION DRIFT BELONGS IN `doctor`, NOT ONLY IN THE BANNER (#469
    # review 5). Drift was surfaced by `techne status` and by the SessionStart
    # banner — but the banner is rendered BY the hook, so the one failure it is
    # meant to report (a hook that never fires) is the one that silences it. The
    # operator's deliberate "is this working?" invocation must carry it.
    #
    # It answers a different question from everything above: the checks ask "did
    # this rail RUN?", drift asks "is it even WIRED?". A hook enabled in config
    # and absent from settings.json can never leave a marker, so it reads
    # UNOBSERVABLE with no indication of why — this supplies the why.
    #
    # REPORTED ALWAYS, COUNTED ONLY ON AN INSTALLED SYSTEM. A machine that has
    # never run `techne init` has nothing registered, so EVERY config-enabled hook
    # reads as drifted — and counting that would make a fresh install exit 1
    # with nothing wrong, which is precisely the critical-2 mistake fixed
    # earlier in this same PR (a verdict the operator cannot act on teaches them
    # to ignore the signal). On an INSTALLED system the same warnings are real:
    # a flag enabled in config that never made it into settings.json is a hook
    # that can never fire.
    if config.platform != CLAUDE_CODE:
        # settings.json registration is CLAUDE-CODE's mechanism, and both sibling
        # surfaces already guard on it (`techne status` above; render_drift_block,
        # session_start.py). check_hook_drift does NOT self-guard —
        # _expected_registrations returns SessionStart/PreCompact/PostCompact/Stop
        # for any config at all. Without this guard, an operator on another
        # platform who merely HAS a ~/.claude/settings.json on the machine (the
        # ordinary case, not a corner one) gets a doctor that exits 1 on a
        # correctly-configured system and a remedy telling them to reinstall onto
        # a platform they do not use. doctor's docstring promises it is safe to
        # run in CI, so that is a red they cannot clear. (#469 review 6.)
        return failing, 0

    drift_failures = 0
    # BOUND BEFORE THE TRY, because the import itself can raise and everything
    # imported inside a try is a function-local for the WHOLE function. The first
    # cut referenced SETTINGS_PATH in the handler and died with UnboundLocalError
    # on a real invocation (CLAUDE_CONFIG_DIR='~nosuchuser/claude' — hooks.py
    # resolves the config dir at MODULE scope, and Path.expanduser() raises on an
    # unresolvable ~user). The operator lost the whole rest of the report —
    # storage checks, reclaimable space, summary — to a traceback, from the block
    # written so that a failing diagnostic "must not VANISH". It survived a
    # missing-name ImportError only because SETTINGS_PATH sorts first in the
    # import list: luck, not design. (#469 review 7, critical 1.)
    settings_path = None
    launcher_name = None
    probe_failed = False
    try:
        from techne.platforms.claude_code.hooks import (
            SETTINGS_PATH,
            _launcher_path,
            check_hook_drift,
            settings_unusable_reason,
        )

        settings_path = SETTINGS_PATH
        launcher_name = _launcher_path().name
        drift = check_hook_drift(config)
        # "HAVE WE INSTALLED HOOKS HERE?", not "does a settings file exist?".
        # techne-hook.sh is written by register_hooks on every init and DELETED by
        # unregister, so its presence means exactly "techne installed hooks here
        # and has not uninstalled them" — the predicate this branch wants.
        # SETTINGS_PATH.exists() was the wrong file: a system that WAS initialised
        # and then lost settings.json is demonstrably installed yet reads as
        # fresh, so a genuinely dead capture rail went uncounted and was told to
        # "run `techne init` to install" — advice for a machine that has
        # provably already run. Residual limit, stated rather than implied: an
        # operator who deletes ~/.claude wholesale loses both files and still
        # reads as fresh. That one is genuinely indistinguishable, and benign.
        # Second residual: the launcher landed in 1fe7cac (#276/#279, 2026-07-07),
        # so a machine last initialised BEFORE that has registered hooks and no
        # techne-hook.sh, and reads as not-installed — drift reported, uncounted.
        # Any re-init closes it. (#469 reviews 6 and 7.)
        installed = _launcher_path().exists()
        unverifiable = settings_unusable_reason(config) is not None
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never break the diagnosis
        # ...but it must not VANISH either. This previously set `drift, installed
        # = [], False`, which printed nothing at all — and an absent check reads
        # as "nothing wrong", the exact failure mode this module exists to
        # prevent. run_checks holds the opposite policy twelve hundred lines
        # earlier: a check that raises becomes an UNKNOWN result CARRYING the
        # exception, never an absent one. Counted, because a registration we
        # could not verify is unverified, not fine. (#469 review 6, nit 5.)
        drift = [f"the drift check itself failed ({exc}) — registration NOT verified"]
        installed, unverifiable, probe_failed = True, True, True

    if drift:
        # Two distinct conditions share this reporter, and mislabelling them is
        # its own small lie — the same one PR #457 removed from the banner and
        # from `techne status`, reintroduced here on the third surface.
        # "enabled in config but NOT registered" is a precise claim; an unreadable
        # or wrong-shaped settings.json does not mean that, it means we cannot
        # tell. Ask the CONDITION (settings_unusable_reason), never grep the
        # prose. (#469 review 6, issue 2.)
        if unverifiable:
            click.echo("\nHook registration cannot be verified:")
        else:
            click.echo("\nHook registration drift:")
        for warning in drift:
            click.echo(f"  ! {warning}")
        if installed:
            if probe_failed:
                # A THIRD CASE, not a flavour of "unusable". Routing our own bug
                # to the settings.json remedy tells an operator whose settings
                # are FINE to move aside the file holding the rest of their
                # Claude Code configuration — the issue-2 mislabel arriving
                # through the exception door. The heading is honest; the remedy
                # must not name a file we have no evidence against.
                # (#469 review 7, issue 5.)
                # "with the message above", NOT "with the traceback": this path
                # formats str(exc) and nothing else prints a traceback, so asking
                # for one names an artefact we withheld. (#469 review 8, nit 5.)
                click.echo(
                    "  remedy: none — this is a techne bug, not a problem with "
                    "your settings. Please report it with the message above."
                )
            elif unverifiable:
                # `techne init` is the WRONG remedy for an unusable file and would
                # be a second lie: register_hooks walks the same structure without
                # guarding it, so init itself raises on most unusable shapes
                # (RuntimeError on `{ not json`, AttributeError on `{"hooks": []}`
                # and on `[]` — measured, not quoted). Handing the operator a
                # command that crashes, at the moment every hook may be dead, is
                # worse than saying nothing. Repair the file first.
                # SKIP THE COMMAND WHEN THERE ISN'T ONE. init_remedy returns None
                # off claude-code, and f-stringing it raw would print
                # "remedy: None" — the platform guard is inherited only because
                # the caller honours the None. The early return above makes that
                # unreachable HERE, but the docstring's promise ("a new surface
                # cannot be added without inheriting both guards") is only true
                # if callers behave this way. (#469 review 8, nit 7.)
                remedy = init_remedy(config)
                repair = f"  remedy: repair or move aside {settings_path}"
                click.echo(f"{repair}, then run `{remedy}`" if remedy else f"{repair}.")
            else:
                remedy = init_remedy(config)
                if remedy:
                    click.echo(f"  remedy: {remedy}")
            drift_failures = len(drift)
        else:
            # THE LAST BARE `techne init` ON THIS SURFACE. It re-resolves
            # --posture from `auto` and SAVES the result, which is the silent
            # demotion this PR has now fixed at three other sites; an install
            # instruction is not exempt from it. (#469 review 9.)
            install = init_remedy(config) or "techne init"
            click.echo(
                f"  (not counted — techne hooks are not installed here "
                f"(no {launcher_name}); run `{install}` to install)"
            )

    return failing, drift_failures


_DECLARED_SUPERSESSIONS_SHOWN = 20


def _render_declared_supersessions(db_path) -> int:
    """Print entries whose prose says they replace a still-live entry (techne#887).

    ADVISORY, NOT COUNTED toward doctor's exit code. The finding is detectable but
    not always remediable: `techne#N` in prose can name a GitHub issue, not a
    knowledge row, and a false match could only be cleared by retiring a row that
    should stay live. A condition you can detect but not remedy belongs in the
    detail, never the verdict (docs/arch/liveness.md).

    A pair matched only through a bare `techne#N` reference is printed as a CHECK
    item, never as a ready-to-run command (techne#1065): on 2026-09-28 doctor
    printed two such pairs as finished `techne knowledge supersede` commands, and
    both would have retired live, unrelated knowledge if pasted as-is. Returns the
    count found (ready + check items).
    """
    from techne.core.knowledge import find_declared_supersessions

    click.echo()
    click.echo("Supersessions declared in prose but never recorded (advisory):")
    try:
        pairs = find_declared_supersessions(db_path)
    except Exception as exc:  # a probe failure is reported, never swallowed
        click.echo(f"  ? could not scan: {exc}")
        return 0
    if not pairs:
        click.echo("  ✓ none")
        return 0

    ready = [p for p in pairs if not p["ambiguous"]]
    ambiguous = [p for p in pairs if p["ambiguous"]]

    if ready:
        click.echo(
            f"  ⚠ {len(ready)} live entr{'y' if len(ready) == 1 else 'ies'} named as replaced "
            "by a newer entry. Check each, then run the command, in this order:"
        )
        for p in ready[:_DECLARED_SUPERSESSIONS_SHOWN]:
            click.echo(
                f"    techne knowledge supersede {p['target']} --by {p['declarer']}"
                f"   # {p['target_preview']}"
            )
        if len(ready) > _DECLARED_SUPERSESSIONS_SHOWN:
            click.echo(f"    … and {len(ready) - _DECLARED_SUPERSESSIONS_SHOWN} more")

    if ambiguous:
        click.echo(
            f"  ? {len(ambiguous)} pair{'s' if len(ambiguous) != 1 else ''} matched only "
            "through a bare `techne#N` reference — CHECK, do not run as printed "
            "(`techne#N` may name a GitHub issue, not knowledge #N):"
        )
        for p in ambiguous[:_DECLARED_SUPERSESSIONS_SHOWN]:
            # Quote the text as written: an unmarked list continuation says `#N`,
            # and its ambiguity comes from the `techne#M` before it.
            via = p.get("ambiguous_via")
            said = (
                f"techne#{p['target']}"
                if via in (None, p["target"])
                else f"#{p['target']} (listed after techne#{via})"
            )
            click.echo(
                f"    CHECK: entry {p['declarer']} says it replaces {said} — "
                f"confirm that means knowledge #{p['target']} (not GitHub issue #{p['target']}) "
                f"before running `techne knowledge supersede {p['target']} --by {p['declarer']}`"
                f"   # {p['target_preview']}"
            )
        if len(ambiguous) > _DECLARED_SUPERSESSIONS_SHOWN:
            click.echo(f"    … and {len(ambiguous) - _DECLARED_SUPERSESSIONS_SHOWN} more")

    click.echo(
        "  (a `#N` in prose may name an issue, not a knowledge entry; not counted as a failure)"
    )
    return len(pairs)


def _render_config_pins(config) -> None:
    """Which config keys are pinned, and whether a legacy file is being migrated (#1070).

    Advisory: never changes doctor's exit code and never writes. config.yaml is
    sparse, so the pinned list is the file's own keys; everything else follows
    the defaults of the running version.
    """
    if config.loaded_from is None:
        click.echo("Config: no config.yaml; every key follows the shipped defaults.")
        return
    pinned = config.pinned_keys()
    listed = ", ".join(pinned) if pinned else "none"
    click.echo(
        f"Config: {config.loaded_from} pins {len(pinned)} key(s): {listed}. "
        "Everything else follows the shipped defaults."
    )
    if config.config_migration is not None:
        click.echo(
            "  "
            + config.config_migration.summary()
            + ". In effect now; `techne init` rewrites the file and keeps a backup."
        )
        pending = config.config_migration.pending_line()
        if pending:
            click.echo(f"  ⚠ {pending}")


@main.command()
@click.option("--vacuum", is_flag=True, help="Reclaim freelist space (VACUUM) after the checks.")
@click.option(
    "--liveness/--no-liveness",
    default=True,
    help=(
        "Include subsystem liveness checks (did each rail actually run?). "
        "Also carries the hook-registration drift report, which is a different "
        "question (is it WIRED?) — --no-liveness drops both."
    ),
)
@click.pass_context
def doctor(ctx, vacuum, liveness):
    """Health checks for the memory store.

    Two independent questions:

    \b
      STORAGE  — silent-accumulation bugs (duplicate rows a table shouldn't
                 have) and reclaimable space: failure modes with no functional
                 symptom, invisible to behaviour tests.
      LIVENESS — did each subsystem actually RUN? (techne#468) Configuration
                 being correct proves nothing: in every one of the six
                 built-but-never-wired failures found on 2026-08-02, the config
                 was right and the rail was dead.

    Exits non-zero if duplicate-row debt is found OR any subsystem is not
    confirmed working, so it is safe to run in CI / a scheduled audit.

    Also lists entries whose prose declares they replace a still-live entry
    (techne#887), with the `techne knowledge supersede` command for each.
    Advisory: it never changes the exit code and never writes.
    """
    import sqlite3
    import sys

    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    liveness_failures, drift_failures = _render_liveness(config) if liveness else (0, 0)

    conn = sqlite3.connect(config.db_path)
    # Wait for the write lock like the rest of techne (connect() sets the same), so
    # `doctor --vacuum` against a live agent queues instead of erroring "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    issues = 0
    click.echo(f"Database: {config.db_path}")
    _render_config_pins(config)
    click.echo()
    click.echo("Duplicate-row checks (total rows vs distinct logical key):")
    for table, key_cols in _DEDUP_CHECKS:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        key = ", ".join(key_cols)
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        distinct = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT DISTINCT {key} FROM {table})"
        ).fetchone()[0]
        dupes = total - distinct
        if dupes > 0:
            # distinct is never 0 here: the key columns are NOT NULL, so dupes > 0 implies distinct > 0.
            ratio = total / distinct if distinct else float("inf")
            click.echo(
                f"  ⚠ {table}: {total} rows, {distinct} distinct ({key}) "
                f"— {dupes} duplicate rows ({ratio:.1f}×)"
            )
            issues += 1
        else:
            click.echo(f"  ✓ {table}: {total} rows, no duplicates")

    _render_declared_supersessions(config.db_path)

    # Freelist: dedup/deletes free pages but SQLite only returns them to the OS on VACUUM.
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    reclaimable_mb = (page_size * freelist) / 1048576
    click.echo()
    if reclaimable_mb >= 1.0:
        click.echo(f"Reclaimable space: {reclaimable_mb:.1f} MB on the freelist.")
        if not vacuum:
            click.echo("  Run 'techne doctor --vacuum' to return it to the OS.")
    else:
        click.echo(f"Reclaimable space: {reclaimable_mb:.1f} MB (nothing to reclaim).")

    if vacuum:
        before = config.db_path.stat().st_size
        conn.isolation_level = None  # VACUUM cannot run inside a transaction
        conn.execute("VACUUM")
        after = config.db_path.stat().st_size
        click.echo(
            f"  VACUUM: {before / 1048576:.1f} MB → {after / 1048576:.1f} MB "
            f"({(before - after) / 1048576:.1f} MB reclaimed)."
        )

    conn.close()
    click.echo()
    if issues or liveness_failures or drift_failures:
        parts = []
        if issues:
            parts.append(f"{issues} storage issue(s)")
        if liveness_failures:
            parts.append(f"{liveness_failures} subsystem(s) not confirmed working")
        if drift_failures:
            # NOT folded into the subsystem count: a drift warning is not a
            # subsystem that failed to run, and the two conditions arrive from
            # different evidence. "registration issue(s)" rather than "not
            # registered" because this line also covers the unverifiable case,
            # where the precise claim cannot be made. (#469 review 6, issue 4.)
            parts.append(f"{drift_failures} hook registration issue(s)")
        click.echo(" · ".join(parts))
        sys.exit(1)
    # Deliberately does NOT say "all checks passed" unless liveness ran too — a
    # green line that covers only storage is what let consolidate and dream sit
    # cold for four weeks under a passing doctor (techne#468).
    click.echo("All checks passed." if liveness else "Storage checks passed (liveness skipped).")


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
        sid = _resolve_session_prefix(conn, session_id)
    finally:
        conn.close()

    if sid is None:
        click.echo(f"No session matching: {session_id}")
        return

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
        resolved = _resolve_session_prefix(conn, session_id)
        sess = (
            conn.execute(
                "SELECT id, project_path, started_at FROM sessions WHERE id = ?",
                (resolved,),
            ).fetchone()
            if resolved is not None
            else None
        )
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


def _resolve_session_prefix(conn, prefix: str) -> str | None:
    """The newest session whose id starts with *prefix* LITERALLY, or None.

    One definition for every command that takes a session-id prefix (issue #902):
    the three copies of `LIKE ? || '%'` passed the operator's text through as a
    pattern, so `%` or `_` matched some other session — `reflect undecline
    --session '%'` retracted every decline in the newest one and exited 0.
    """
    from techne.core.database import like_escape

    row = conn.execute(
        "SELECT id FROM sessions WHERE id LIKE ? || '%' ESCAPE '\\' "
        "ORDER BY started_at DESC LIMIT 1",
        (like_escape(prefix),),
    ).fetchone()
    return row["id"] if row else None


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

    # techne#501: `techne search` is an EXPLICIT query, so untrusted rows are
    # SHOWN and tagged rather than filtered. The automatic injection rails do the
    # opposite — see the contract note in core/search.py.
    # `curated_head_slots` FROM CONFIG, like the MCP sites (Prudence, #966 r2). This
    # surface defaults to `--table all`, so without it the knob read as inert exactly
    # where an operator would check whether it took effect — two surfaces answering
    # one query differently, which is the defect #727's connection seam exists to
    # remove.
    results = do_search(
        config.db_path,
        query,
        table=table,
        limit=limit,
        include_uncertain=True,
        curated_head_slots=config.search.curated_head_slots,
        identifier_head_slots=config.search.identifier_head_slots,
        identifier_rare_fraction=config.search.identifier_rare_fraction,
        phrase_seat=True,  # techne#1142: explicit query, like techne_search
    )
    if not results:
        click.echo("No results.")
        return

    for r in results:
        source = r.get("source", "fts")
        # `source_table` since techne#579 — the row's own kind, when it has one,
        # is `knowledge_type`. This surface wants the TABLE, because it is
        # branching on which shape the row is (thread/observation/knowledge).
        type_tag = r["source_table"]
        # An untrusted row is LABELLED, not silently ranked lower (techne#501).
        # `status` is the trust STATE; `confidence` answers the different question
        # of how sure we were when we wrote it.
        trust = " ⚠ UNCERTAIN" if r.get("status") == "uncertain" else ""
        click.echo(f"  [{type_tag}]{trust} ({source})")
        if type_tag == "thread":
            click.echo(f"    {r.get('label', '')} — {r.get('summary', '')[:200]}")
        elif type_tag == "observation":
            click.echo(f"    [{r.get('tool_name', '')}] {_preview(r)}")
        elif type_tag == "knowledge":
            click.echo(f"    [{r.get('knowledge_type', '')}] {r.get('content', '')[:200]}")
        elif type_tag == "document":
            click.echo(f"    #{r.get('id')} {r.get('title', '') or r.get('source_url', '')}")
            snippet = (r.get("content", "") or "").replace("\n", " ").strip()
            click.echo(f"    {snippet[:200]}")
            click.echo(f"    ↳ full: techne librarian show {r.get('id')}")
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


# The --type choices are DERIVED from core's VALID_TYPES (techne#877). They were a
# hardcoded list that offered `pattern`, which store() always refuses (techne#137:
# patterns live in architect.db), and omitted `document`/`document_card`, which it
# accepts. Neither list was the truth; now there is only one.
_KNOWLEDGE_TYPE_HELP = (
    "Knowledge type. Architectural patterns are not stored here: "
    "use `techne architect ingest` (techne#137)."
)


def _knowledge_types() -> click.Choice:
    from techne.core.knowledge import VALID_TYPES

    return click.Choice(list(VALID_TYPES))


@knowledge.command("list")
@click.option("--type", "ktype", type=_knowledge_types(), help=_KNOWLEDGE_TYPE_HELP)
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
    type=_knowledge_types(),
    default="fact",
    help=_KNOWLEDGE_TYPE_HELP,
)
@click.option("--tags", default="")
@click.option("--confidence", type=click.Choice(["high", "medium", "low"]), default="medium")
@click.pass_context
def knowledge_add(ctx, content, ktype, tags, confidence):
    """Add a knowledge entry."""
    config = ctx.obj["config"]
    from techne.core.knowledge import store

    # Core's refusals carry the useful message (which store to use instead); print
    # it as a CLI error, not a traceback (techne#877).
    try:
        entry_id = store(
            config.db_path, content, type=ktype, tags=tags, confidence=confidence, source="manual"
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Stored knowledge entry #{entry_id}")


@knowledge.command("supersede")
@click.argument("old_id", type=int)
@click.argument("content", required=False)
@click.option(
    "--by",
    "survivor_id",
    type=int,
    default=None,
    help="Retire OLD_ID into this EXISTING live entry instead of writing new content.",
)
@click.pass_context
def knowledge_supersede(ctx, old_id, content, survivor_id):
    """Retire a knowledge entry so recall stops treating it as current (techne#887).

    Two forms, one of them per call:

    \b
      techne knowledge supersede OLD_ID "new content"
          Write a new revision. It inherits OLD_ID's type, tags and
          confidence, and its decay clock starts fresh.
      techne knowledge supersede OLD_ID --by NEW_ID
          NEW_ID already says what OLD_ID should have said (often it
          declares "SUPERSEDES #OLD_ID" in prose, which recall never
          reads). Points OLD_ID at it; no new row is written.

    Either way OLD_ID is kept, not deleted, and entries that depended on it
    are marked uncertain until reconfirmed. `techne doctor` lists entries that
    declare a replacement in prose that was never recorded.
    """
    if (content is None) == (survivor_id is None):
        raise click.UsageError("give either CONTENT or --by NEW_ID, not both and not neither")
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core.knowledge import retire, supersede

    try:
        if survivor_id is not None:
            retire(config.db_path, old_id, survivor_id)
            click.echo(f"Knowledge entry #{old_id} is now superseded by #{survivor_id}")
        else:
            new_id = supersede(config.db_path, old_id, content, source="manual")
            click.echo(f"Knowledge entry #{old_id} is now superseded by new entry #{new_id}")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


@knowledge.command("export")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default="techne-knowledge",
    show_default=True,
    help="Output directory for the markdown tree.",
)
@click.option(
    "--include-superseded",
    is_flag=True,
    default=False,
    help="Include superseded (archived) entries in addition to live knowledge.",
)
@click.pass_context
def knowledge_export(ctx, output, include_superseded):
    """Export live knowledge as a browseable, git-diffable markdown tree (techne#218).

    Writes an index file plus one file per knowledge type to OUTPUT. Only live
    entries (superseded_by IS NULL) are exported by default. Output is
    deterministic — stable ordering by type then id — so re-exports on an
    unchanged DB produce byte-identical files and meaningful git diffs.

    Example layout:

    \b
        OUTPUT/index.md        — summary table + entry stubs by type
        OUTPUT/fact.md         — all facts
        OUTPUT/decision.md     — all decisions
        OUTPUT/procedure.md    — all procedures
        OUTPUT/evaluation.md   — all evaluations
    """
    config = ctx.obj["config"]
    from techne.core.knowledge_export import export_knowledge

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    result = export_knowledge(config.db_path, output, include_superseded=include_superseded)
    total = result["total"]
    scope = "(including superseded)" if include_superseded else "(live only)"
    click.echo(f"Exported {total} {'entry' if total == 1 else 'entries'} {scope} → {output}/")
    for ktype, count in sorted(result["by_type"].items()):
        click.echo(f"  {output}/{ktype}.md  ({count} {'entry' if count == 1 else 'entries'})")
    click.echo(f"  {output}/index.md")


@knowledge.command("archive-scan")
@click.option(
    "--floor",
    "confidence_floor",
    type=float,
    default=None,
    help=(
        "Effective-confidence floor (0..1). Entries below this are candidates. "
        "Defaults to config.archive.confidence_floor (0.1)."
    ),
)
@click.option(
    "--window",
    "reaffirmation_window_days",
    type=int,
    default=None,
    help=(
        "Reaffirmation window in days. Entries not reaffirmed within this period qualify. "
        "Defaults to config.archive.reaffirmation_window_days (180)."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show which entries would be archived without writing to the DB.",
)
@click.pass_context
def knowledge_archive_scan(ctx, confidence_floor, reaffirmation_window_days, dry_run):
    """Soft-archive stale, uncertain, unreaffirmed knowledge entries (techne#216).

    Entries meeting ALL of these criteria are archived (excluded from default
    session-start injection, still returned by explicit search, NEVER deleted):

    \b
      - status = 'uncertain'  (a dependency it rests on was superseded)
      - effective_confidence() < floor  (decayed below the confidence floor)
      - not reaffirmed within the window  (last_confirmed_at is old)

    Archived entries can be restored with:  techne knowledge unarchive ID

    Defaults are conservative (floor=0.1, window=180d) to avoid false positives.
    """
    config = ctx.obj["config"]

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core.knowledge import archive_scan

    floor = confidence_floor if confidence_floor is not None else config.archive.confidence_floor
    window = (
        reaffirmation_window_days
        if reaffirmation_window_days is not None
        else config.archive.reaffirmation_window_days
    )

    archived = archive_scan(
        config.db_path,
        confidence_floor=floor,
        reaffirmation_window_days=window,
        half_life_days=config.knowledge_confidence_half_life_days,
        dry_run=dry_run,
    )

    if not archived:
        click.echo("No entries qualify for archiving.")
        return

    tag = " (dry run — not written)" if dry_run else ""
    click.echo(
        f"{'Would archive' if dry_run else 'Archived'} {len(archived)} {'entry' if len(archived) == 1 else 'entries'}{tag}:"
    )
    for entry in archived:
        snippet = entry["content"][:80].replace("\n", " ")
        click.echo(f"  #{entry['id']}  eff_conf={entry['effective_confidence']:.4f}  {snippet!r}")


@knowledge.command("unarchive")
@click.argument("entry_id", type=int)
@click.pass_context
def knowledge_unarchive(ctx, entry_id):
    """Restore an archived knowledge entry to 'uncertain' status (techne#216).

    Reverses a soft-archive — the entry returns to default recall visibility.
    Does NOT reconfirm the entry (the dependency that caused it to become
    uncertain is still unresolved). Use 'techne knowledge reconfirm' separately
    if the dependency has been resolved.
    """
    config = ctx.obj["config"]

    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core.knowledge import unarchive

    restored = unarchive(config.db_path, entry_id)
    if restored:
        click.echo(f"Restored knowledge entry #{entry_id} → status='uncertain'.")
    else:
        click.echo(
            f"Entry #{entry_id} was not found or is not in 'archived' status.",
            err=True,
        )
        # Exit non-zero on a no-op so scripts can distinguish success from miss.
        ctx.exit(1)


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
def lesson(ctx):
    """Agent self-lessons, standing grants and charter (techne#1195)."""
    pass


def _lesson_errors(fn):
    """A refused lesson write is an operator answer, not a crash: print the reason."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


def _lesson_agent(config, agent):
    from techne.core.lessons import agent_for

    return agent if agent is not None else agent_for(config)


_AGENT_HELP = (
    "Stable agent id the row belongs to. Default: config instance_name; "
    "'' for every agent on this store."
)


@lesson.command("add")
@click.argument("rule")
@click.option("--tier", type=click.Choice(["principle", "entry"]), default="principle")
@click.option("--body", default="", help="Incident, mechanism and why.")
@click.option("--parent", "parent_id", type=int, help="Principle this entry belongs to.")
@click.option("--session", "evidence_session", help="Session the mistake happened in.")
@click.option("--agent", default=None, help=_AGENT_HELP)
@click.pass_context
@_lesson_errors
def lesson_add(ctx, rule, tier, body, parent_id, evidence_session, agent):
    """Add a principle or an entry."""
    from techne.core import lessons

    config = ctx.obj["config"]
    new_id = lessons.add(
        config.db_path,
        rule=rule,
        tier=tier,
        body=body,
        agent_id=_lesson_agent(config, agent) or None,
        parent_id=parent_id,
        evidence_session=evidence_session,
        source="operator",
    )
    click.echo(f"stored {tier} {new_id}")


@lesson.command("grant")
@click.argument("rule")
@click.option("--body", default="")
@click.option("--agent", default=None, help=_AGENT_HELP)
@click.pass_context
@_lesson_errors
def lesson_grant(ctx, rule, body, agent):
    """Add a standing permission the agent always sees."""
    from techne.core import lessons

    config = ctx.obj["config"]
    new_id = lessons.add_operator(
        config.db_path,
        kind="grant",
        rule=rule,
        body=body,
        agent_id=_lesson_agent(config, agent) or None,
    )
    click.echo(f"stored grant {new_id}")


@lesson.command("charter")
@click.argument("rule")
@click.option("--body", default="")
@click.option("--agent", default=None, help=_AGENT_HELP)
@click.pass_context
@_lesson_errors
def lesson_charter(ctx, rule, body, agent):
    """Add a trust boundary the agent always sees and cannot change."""
    from techne.core import lessons

    config = ctx.obj["config"]
    new_id = lessons.add_operator(
        config.db_path,
        kind="charter",
        rule=rule,
        body=body,
        agent_id=_lesson_agent(config, agent) or None,
    )
    click.echo(f"stored charter {new_id}")


@lesson.command("refine")
@click.argument("knowledge_id", type=int)
@click.argument("rule")
@click.option("--body", default=None, help="Replace the body (default: keep it).")
@click.pass_context
@_lesson_errors
def lesson_refine(ctx, knowledge_id, rule, body):
    """Rewrite a principle, grant or charter; its family moves to the new revision."""
    from techne.core import lessons

    config = ctx.obj["config"]
    new_id = lessons.refine(
        config.db_path, knowledge_id, rule, body=body, operator=True, source="operator"
    )
    click.echo(f"{knowledge_id} -> {new_id}")


@lesson.command("link")
@click.argument("entry_id", type=int)
@click.argument("principle_id", type=int)
@click.pass_context
@_lesson_errors
def lesson_link(ctx, entry_id, principle_id):
    """File an entry under a principle's family."""
    from techne.core import lessons

    lessons.link(ctx.obj["config"].db_path, entry_id, principle_id)
    click.echo(f"linked {entry_id} -> {principle_id}")


@lesson.command("enforced-by")
@click.argument("knowledge_id", type=int)
@click.argument("mechanism", required=False, default="")
@click.pass_context
@_lesson_errors
def lesson_enforced_by(ctx, knowledge_id, mechanism):
    """Record the hook, test or guard that now enforces a principle ('' clears)."""
    from techne.core import lessons

    lessons.set_enforced_by(ctx.obj["config"].db_path, knowledge_id, mechanism)
    click.echo(f"{knowledge_id}: enforced_by={mechanism or None}")


@lesson.command("retire")
@click.argument("knowledge_id", type=int)
@click.pass_context
@_lesson_errors
def lesson_retire(ctx, knowledge_id):
    """Stop injecting a principle, grant or charter. It stays searchable."""
    from techne.core import lessons

    lessons.retire(ctx.obj["config"].db_path, knowledge_id)
    click.echo(f"retired {knowledge_id}")


@lesson.command("list")
@click.option("--tier", type=click.Choice(["principle", "entry"]))
@click.option("--kind", type=click.Choice(["lesson", "grant", "charter"]))
@click.option("--agent", default=None, help=_AGENT_HELP)
@click.pass_context
@_lesson_errors
def lesson_list(ctx, tier, kind, agent):
    """List live lessons with their family counts."""
    from techne.core import lessons

    config = ctx.obj["config"]
    rows = lessons.list_lessons(
        config.db_path, agent_id=_lesson_agent(config, agent) or None, tier=tier, kind=kind
    )
    for r in rows:
        label = r["tier"] or r["type"]
        extra = ""
        if r["tier"] == "principle":
            extra = f"  recurrences={r['recurrences']} loaded_failures={r['loaded_failures']}"
            if r["enforced_by"]:
                extra += f" enforced_by={r['enforced_by']}"
        elif r["parent_id"]:
            extra = f"  parent={r['parent_id']}"
        click.echo(f"  {r['id']:>6} {label:<9} {r['rule'][:100]}{extra}")
    click.echo(f"{len(rows)} row(s)")


@lesson.command("show")
@click.option("--agent", default=None, help=_AGENT_HELP)
@click.pass_context
@_lesson_errors
def lesson_show(ctx, agent):
    """Print the principle band exactly as a session would receive it."""
    from techne.core import lessons

    config = ctx.obj["config"]
    block = lessons.select_principles(
        config.db_path,
        agent_id=_lesson_agent(config, agent) or None,
        char_budget=config.injection.principle_char_budget,
        clauses=config.injection.principle_clauses,
    )
    click.echo(block.text or "(empty)")
    click.echo(f"-- {block.chars}/{config.injection.principle_char_budget} chars")


@lesson.command("review")
@click.option("--agent", default=None, help=_AGENT_HELP)
@click.option("--cold-days", default=30, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
@_lesson_errors
def lesson_review(ctx, agent, cold_days, as_json):
    """What to look at: over budget, failing while loaded, cold, long, unlinked."""
    import json as _json

    from techne.core import lessons

    config = ctx.obj["config"]
    report = lessons.review(
        config.db_path,
        agent_id=_lesson_agent(config, agent) or None,
        char_budget=config.injection.principle_char_budget,
        clauses=config.injection.principle_clauses,
        cold_days=cold_days,
    )
    if as_json:
        click.echo(_json.dumps(report, indent=2, default=str))
        return
    b, c = report["budget"], report["counts"]
    click.echo(
        f"agent={report['agent_id']}  principles={c['principles']} entries={c['entries']} "
        f"grants={c['grants']} charter={c['charter']}"
    )
    click.echo(f"band: {b['chars_used']}/{b['char_budget']} chars, {b['injected']} injected")
    if b["dropped"]:
        click.echo(f"  OVER BUDGET — not injected: {b['dropped']}")
    sections = (
        ("enforce_candidates", "Failing while loaded — enforce with a hook/test instead"),
        ("cold", f"Cold (no reaffirm or recurrence in {cold_days}d) — keep, merge or retire?"),
        ("long", "Long — split into a one-line rule plus entries"),
        ("unlinked_entries", "Entries with no principle — `techne lesson link`"),
        ("enforced", "Enforced elsewhere — candidates to retire"),
    )
    for key, title in sections:
        if report[key]:
            click.echo(f"\n{title}:")
            for r in report[key]:
                click.echo(f"  {r['id']:>6}  {r['rule'][:100]}")


@lesson.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--agent", default=None, help=_AGENT_HELP)
@click.option("--dry-run", is_flag=True)
@click.pass_context
@_lesson_errors
def lesson_import(ctx, path, agent, dry_run):
    """Import a personal-observations markdown file (principles, grants, dated entries)."""
    from pathlib import Path as _Path

    from techne.core import lessons

    config = ctx.obj["config"]
    result = lessons.import_markdown(
        config.db_path,
        _Path(path).read_text(encoding="utf-8"),
        agent_id=_lesson_agent(config, agent) or None,
        dry_run=dry_run,
    )
    counts = result["parsed"] if dry_run else result["imported"]
    click.echo(
        ("would import: " if dry_run else "imported: ")
        + ", ".join(f"{k}={v}" for k, v in counts.items())
    )
    if result["skipped"]:
        click.echo(f"skipped sections (not recognised): {result['skipped']}")
    if not dry_run and counts.get("entry"):
        click.echo("entries arrive unlinked: `techne lesson review` lists them for `link`")


@main.group()
@click.pass_context
def archivist(ctx):
    """Interrogate the team Archivist (read-only)."""
    pass


@archivist.command("schema")
@click.pass_context
def archivist_schema(ctx):
    """Show the tables and columns `archivist query` can read."""
    from techne.core.archivist_query import readable_schema

    try:
        report = readable_schema(ctx.obj["config"])
    except ValueError as e:
        # Same techne#346 misconfiguration, same reason it must not traceback.
        click.echo(f"error: {e}")
        raise SystemExit(1) from e
    if not report["tables"]:
        click.echo(report["outcome"]["summary"])
        return
    for table, cols in report["tables"].items():
        click.echo(f"{table}")
        click.echo(f"  {', '.join(cols)}")
    limits = report["row_limit"]
    click.echo(f"\nrows: {limits['default']} by default, {limits['max']} max")


@archivist.command("query")
@click.argument("sql")
@click.option("--limit", type=int, default=None, help="Max rows (default 100).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw result.")
@click.pass_context
def archivist_query(ctx, sql, limit, as_json):
    """Run one read-only SELECT against the Archivist (techne#554).

    The operator-facing twin of the techne_archivist_query MCP tool — same guarded
    path, same three outcomes. Exists so "what does the team store actually hold"
    is answerable without an agent in the loop, which is how you check whether the
    agent's answer was true.
    """
    import json as _json
    import sqlite3

    from techne.core.archivist_query import QueryDenied, query

    try:
        result = query(ctx.obj["config"], sql=sql, limit=limit)
    except QueryDenied as e:
        # Exit 2, not 1: a denial is a statement about the SURFACE — learn it and
        # re-ask — where exit 1 below means the SQL itself was wrong. A script that
        # pipes this wants to tell those two apart, and so does a person.
        click.echo(f"denied: {e}")
        raise SystemExit(2) from e
    except sqlite3.Error as e:
        # The caller's SQL is malformed. sqlite's own message is the best available
        # description of it, so print that rather than a traceback — a stack dump
        # tells the operator about OUR call stack when the mistake was in their query.
        click.echo(f"error: {e}")
        raise SystemExit(1) from e
    except ValueError as e:
        # CONFIGURATION, not query: `_resolve_archivist_path` raises when
        # archivist_db_path resolves to the personal db (techne#346). The message is
        # good and says exactly what is wrong; only the packaging was broken — click
        # let it out, so the command printed NOTHING to stdout and dumped a traceback
        # to stderr, contradicting the card's "neither error path prints a traceback".
        # CliRunner intercepts it, which is why 31 tests could not see this and a
        # terminal could. (Prudence, #707 review.)
        click.echo(f"error: {e}")
        raise SystemExit(1) from e

    if as_json:
        click.echo(_json.dumps(result, indent=2, default=str))
        return

    # The outcome line prints FIRST and ALWAYS — including for zero rows, which is
    # the whole point of #554's first design constraint. A silent empty table is
    # exactly the "no results reads as nothing exists" failure.
    click.echo(result["outcome"]["summary"])
    if not result["rows"]:
        return
    click.echo("")
    for row in result["rows"]:
        for k, v in row.items():
            text = "" if v is None else str(v)
            if len(text) > 300:
                text = text[:300] + f"… (+{len(text) - 300} chars)"
            click.echo(f"  {k}: {text}")
        click.echo("")
    tail = f"{result['row_count']} row(s)"
    if result["truncated"]:
        tail += " — TRUNCATED, more rows matched than were returned; raise --limit"
    click.echo(tail)


@main.command()
@click.argument("name", required=False)
@click.pass_context
def guide(ctx, name):
    """Print a bundled guide, or list them. The docs an installed techne carries.

    WHY THIS COMMAND EXISTS (techne#754). techne's agent education is markdown —
    the `Agent: raise this unprompted` blocks are how an agent learns to surface a
    silent failure before the operator meets it. Until 0.16.0 none of it shipped:
    the wheel held code, two skills and two grammars, so every trigger reached
    agents working ON techne and reached an installed user's agent never. Those
    guides now ride in the package, and this is what reads them — no repo checkout,
    no network, works on all five platforms `init --platform` advertises.

    AN AGENT IS THE EXPECTED CALLER, which is why the output is the raw markdown
    rather than a rendered page: it is meant to be read into context, not admired.
    """
    from techne.docs import bundled_guide, guide_names

    if not name:
        click.echo("Bundled guides — `techne guide <name>`:\n")
        for n in guide_names():
            click.echo(f"  {n}")
        click.echo(
            "\nStart with four-functions: it walks the archivist, librarian, architect"
            "\nand codex, and shows how to read the `binding` an answer resolved to."
        )
        return

    try:
        click.echo(bundled_guide(name))
    except KeyError:
        raise SystemExit(
            f"No bundled guide named {name!r}. Run `techne guide` for the list."
        ) from None


@main.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw report.")
@click.pass_context
def bindings(ctx, as_json):
    """What each knowledge domain is bound to (techne#552 part 4).

    The "same feel" question, answered on the axis a larger deployment answers it
    on: the CONNECTION is uniform, whatever is behind it. One question — what is this
    bound to, and can I reach it — asked identically of the archivist, librarian,
    architect and codex.

    Deliberately does NOT change what `techne_recall` queries. Patterns and codex are
    not recall rungs, because measured retrieval today would put noise in a response
    shape that open-sourcing freezes; see core/bindings.py for the numbers.
    """
    import json as _json

    from techne.core.bindings import all_bindings

    reports = all_bindings(ctx.obj["config"])
    if as_json:
        click.echo(_json.dumps([r.as_dict() for r in reports], indent=2))
        return

    width = max(len(r.domain) for r in reports)
    for r in reports:
        # FLAGS ONLY THE FAULT STATE. An earlier version marked anything whose file did
        # not exist, so a healthy fresh install reported "3 domain(s) not reachable":
        # `absent` is unreachable by definition, and librarian/architect create their
        # dbs lazily on first write. An operator would have chased four non-problems.
        # `r.label` FUSES the two axes for the human — an operator scanning four rows
        # wants "is anything wrong", and `store` beside a `!` would send them hunting
        # for the fault state. `--json` above keeps them separate. (Prudence, #717 r3.)
        mark = " " if r.ok else "!"
        click.echo(f"{mark} {r.domain.ljust(width)}  {r.label:11s}  {r.detail}")

    broken = [r for r in reports if not r.ok]
    if broken:
        click.echo("")
        click.echo(f"{len(broken)} domain(s) configured but unreadable — marked ! above")


@main.group()
@click.pass_context
def custodian(ctx):
    """Run the emit→ingest rails headlessly (techne#555)."""
    pass


@custodian.command("status")
@click.pass_context
def custodian_status(ctx):
    """What is bound, what each rail does, and what it last did.

    Answers the question the #555 finding turned on: not "is the rail configured"
    but "did it last run, and did that run work". A rail that has never had a
    model-bound run says so, rather than reading as healthy off work a resident
    agent did.
    """
    from techne.core.custodian import (
        MARKER_PREFIX,
        RAILS,
        effective_model,
        resolve_generate_fn,
    )
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    config = ctx.obj["config"]
    cu = config.custodian
    # "(unset)", NOT "(unbound — rails stay resident)". `rail_models` WITHOUT a global
    # `custodian.model` is a legal, validated config — config.py checks the keys against
    # the registry and nothing requires a base model alongside them — so the old
    # parenthetical made a BEHAVIOURAL claim about rails from a fact about one key, and
    # the runner agreed with the per-rail block four lines below rather than with this
    # line. Reading it as the config echo it is ("custodian model:" is the key name,
    # sitting beside "rails enabled:" and "max per run:"), the value was true; only the
    # gloss overreached. The per-rail `bound:` lines below are the authority.
    # (Prudence, #709 round 5.)
    click.echo(f"custodian model: {cu.model or '(unset — per-rail overrides may still bind)'}")
    if cu.rails:
        click.echo(f"rails enabled:   {', '.join(cu.rails)}")
    click.echo(f"max per run:     {cu.max_items_per_rail}")
    click.echo("")

    conn = connect(config.db_path)
    try:
        for name in sorted(RAILS):
            rail = RAILS[name]
            bound = resolve_generate_fn(config, rail=name) is not None
            # THE MODEL THE RUNNER WILL ACTUALLY USE, per rail — not the global one.
            # `custodian status` is THE surface built to answer "what is bound", and it
            # never read `rail_models`: a rail carrying a per-rail override reported the
            # global model and a bare `bound: yes`. In this feature specifically that is
            # a wrong belief about `model_used`, the one column that records which
            # producer judged a candidate and the column the whole techne#555 finding
            # turned on. `effective_model` already computes the right answer; status was
            # re-deriving a less specific one, which is rounds 2 and 3's shape again.
            # (Prudence, #709 round 4.)
            model = effective_model(config, rail=name)
            marker = get_state(conn, f"{MARKER_PREFIX}:{name}:last_run") or {}
            click.echo(f"{name}")
            click.echo(f"  {rail.what}")
            click.echo(f"  bound: {f'yes — {model}' if bound else 'no (resident)'}")
            if marker:
                click.echo(
                    f"  last run: {marker.get('ts', '?')} "
                    f"{marker.get('outcome', '?')} — {marker.get('detail', '')}"
                )
            else:
                click.echo("  last run: never (no model-bound run recorded)")
    finally:
        conn.close()


@custodian.command("run")
@click.option("--rail", "rail_names", multiple=True, help="Rail to run (repeatable). Default: all.")
@click.option("--limit", type=int, default=None, help="Max prompts per rail this run.")
@click.option("--dry-run", is_flag=True, default=False, help="Emit prompts; call no model.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw report.")
@click.pass_context
def custodian_run(ctx, rail_names, limit, dry_run, as_json):
    """Run the rails with the bound model. The headless entry point.

    THIS IS WHAT A SCHEDULER CALLS. Installing that schedule is deliberately not done
    here: switching it on means spending tokens or GPU on a timer with nobody watching,
    which is the operator's decision to make once, explicitly.
    """
    import json as _json

    from techne.core.custodian import RAILS, run_custodian

    config = ctx.obj["config"]
    selected = list(rail_names) or None
    # `limit` and the rail selection both pass straight through to `custodian`'s
    # resolvers — `effective_limit` and `effective_rails` — so neither the cap nor the
    # selection is re-derived here. The dry-run branch below calls the SAME two
    # functions rather than falling back to its own defaults, which is what makes
    # "one owner" true on both axes (Prudence, #709 rounds 2-3).
    cap = limit

    # A TYPO'D --rail IS AN OPERATOR ERROR, NOT A CRASH (Prudence, #709). Both paths
    # raised a bare KeyError traceback — RAILS[name] on the dry-run path, run_custodian's
    # own raise otherwise. The message was good; the presentation was a stack trace, in
    # a command the operator is expected to wire into cron.
    unknown = [r for r in (selected or []) if r not in RAILS]
    if unknown:
        raise click.BadParameter(
            f"unknown rail(s): {', '.join(unknown)}. Known: {', '.join(sorted(RAILS))}.",
            param_hint="--rail",
        )

    # The bound guard lives in `effective_limit` so programmatic callers get it too;
    # here it is re-raised as a click error so the operator sees the message rather
    # than a traceback. Both paths below (dry-run and real) resolve through it.
    from techne.core.custodian import effective_limit as _effective_limit

    try:
        _effective_limit(config, cap)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--limit") from e

    if dry_run:
        # Emit only — shows exactly what WOULD be sent, and to which model, without
        # spending anything. The honest way to answer "what will this cost me".
        #
        # THE CAP IS APPLIED HERE TOO, through the same `effective_limit` the runners
        # use (Prudence, #709 round 2). This branch bypasses both run_custodian and
        # run_rail, so it was the one surface that disagreed with the runner about what
        # the runner would do — and it is the surface an operator reads BEFORE wiring
        # cron. It over-reported (cap 5, dry-run said 50) so nobody would have
        # overspent, but a number you would trust being wrong is worse than a missing
        # one. Read from the shared function rather than re-derived here, so the
        # ownership consolidated above is not quietly re-copied.
        from techne.core.custodian import (
            effective_limit,
            effective_model,
            effective_rails,
            resolve_generate_fn,
        )

        allowed = effective_limit(config, cap)
        for name in effective_rails(config, selected):
            bound = resolve_generate_fn(config, rail=name)
            try:
                batch = RAILS[name].emit(config)[:allowed]
            except Exception as e:  # noqa: BLE001
                # The real run catches emit failures, records an error marker, and
                # carries on to the next rail. Dry-run did not — one raising rail
                # exited 1 with a traceback and the remaining rails went unreported,
                # on a command meant for cron. It fails loudly rather than lying, which
                # is why this is the smaller of the two. (Prudence, #709 round 3.)
                click.echo(f"{name}: emit failed — {type(e).__name__}: {e}")
                continue
            # NAMES THE MODEL, because the docstring above claims it does ("shows
            # exactly what WOULD be sent, and to which model") and it did not — it
            # printed bound/unbound only. A claim a surface does not honour is the
            # same defect as a number it gets wrong. (Prudence, #709 round 4.)
            target = effective_model(config, rail=name) if bound else ""
            click.echo(
                f"{name}: {len(batch)} prompt(s) would be sent"
                f"{f' to {target}' if bound else ' — but NO MODEL IS BOUND, so nothing would run'}"
            )
        return

    report = run_custodian(config, rails=selected, limit=cap)

    if as_json:
        click.echo(_json.dumps(report, indent=2, default=str))
        return

    if not report["bound"]:
        # Exit 0: unbound is the DEFAULT posture, not a failure. The rails are answered
        # by a resident agent and that has always been the working path.
        click.echo(
            "No model bound — every rail stays resident. Set custodian.model to run headless."
        )
        return

    failures = 0
    for run in report["rails"]:
        line = (
            f"{run['rail']}: emitted {run['emitted']}, "
            f"answered {run['answered']}, failed {run['failed']}"
        )
        if run["counts"]:
            line += "  " + ", ".join(f"{k}={v}" for k, v in sorted(run["counts"].items()))
        click.echo(line)
        # ANSWERED-ZERO-OF-N IS A FAILURE AND MUST EXIT NON-ZERO. A scheduled run whose
        # only signal is exit status has to be able to say "I did nothing" — reporting
        # success while producing nothing is the exact #555 failure, one layer up.
        if run["error"]:
            click.echo(f"  error: {run['error']}")
        if run["emitted"] and run["answered"] == 0:
            failures += 1

    if failures:
        raise SystemExit(1)


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

    adapter = load_adapter(
        config.sync.adapter,
        url=config.sync.url,
        path=config.sync.path,
        allow_unlisted=config.sync.allow_unlisted_adapter,
    )
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
    from techne.sync.sinks import ensure_sinks_registered

    # Register sink:archivist and installed packages' sinks so a fresh CLI process
    # can drain pending rows that failed immediate delivery (techne#346, #1172).
    ensure_sinks_registered(config)
    summary = flush_sinks(config.db_path)
    if not summary:
        click.echo("No pending rows.")
        return
    for target, (synced, failed) in sorted(summary.items()):
        click.echo(f"  {target}: {synced} synced, {failed} failed")


@outbox.command("prune-orphans")
@click.option(
    "--target",
    "targets",
    multiple=True,
    help="Prune only rows for this target (repeatable). Allowed while an entry point "
    "is refused: naming a target asserts its rows are dead, so name a refused "
    "package's own target only to give up its rows.",
)
@click.pass_context
def outbox_prune_orphans(ctx, targets):
    """Mark pending rows for unregistered targets as dead."""
    config = ctx.obj["config"]
    from techne.sync.outbox import prune_orphans
    from techne.sync.sinks import ensure_sinks_registered

    # REGISTER FIRST, exactly as flush-sinks above does (#484). Sinks are
    # per-process state: in a fresh CLI process there are none, so every
    # sink:archivist_knowledge row looks like a row for an unregistered target and
    # prune_orphans marks the whole live backlog dead. The command's help is
    # accurate — the defect was that a registered lane presented as unregistered.
    found = ensure_sinks_registered(config)
    # A REFUSED OR FAILED entry point's target names are unknowable (the gate runs
    # before import), so its pending rows cannot be told from true orphans. Pruning
    # now would mark them dead for good; refusing leaves them pending (#1172).
    blocked = sorted(found.refused) + sorted(found.failed)
    if blocked and not targets:
        raise click.ClickException(
            "Not pruning: outbox sink entry point(s) were not loaded, so their "
            f"pending rows would be marked dead: {', '.join(blocked)}. Allow them in "
            "`sync.allowed_sinks` and re-run, or prune only targets you know are dead "
            "with `--target sink:<name>`. Uninstalling the package gives up its pending "
            "rows: the next prune marks them dead. `techne sinks list` shows why each "
            "entry point was not loaded."
        )
    pruned = prune_orphans(config.db_path, list(targets) or None)
    click.echo(f"Pruned {pruned} orphan row(s).")


@outbox.command("sweep")
@click.option("--retain-days", default=7, show_default=True, help="Age bound for non-ledger lanes.")
@click.option("--apply", "apply_", is_flag=True, help="Actually delete (default: dry run).")
@click.pass_context
def outbox_sweep(ctx, retain_days, apply_):
    """Drop delivered rows nothing will read again (#518).

    Collapses ledger lanes to one row per content hash and ages out the rest.
    Never touches pending or dead rows. DRY RUN BY DEFAULT — pass --apply.
    """
    config = ctx.obj["config"]
    from techne.sync.outbox import sweep_delivered, sweep_preview

    if not apply_:
        preview = sweep_preview(config.db_path, retain_days=retain_days)
        # PRINT THE SCOPE, NOT JUST A COUNT. A dry run that reports only a total
        # looks equally correct whether the scope is right or has swallowed
        # everything — Prudence's Critical on #432's sweep-legacy-verdicts.
        click.echo(f"Dry run (retain_days={retain_days}). Would delete:")
        click.echo(f"  collapse (ledger lanes): {preview['collapsed']} redundant row(s)")
        click.echo(f"  age out (other lanes):   {preview['aged_out']} row(s)")
        for target, n in sorted(preview["by_target"].items()):
            click.echo(f"    {target}: {n}")
        click.echo(f"  retained: {preview['retained']} row(s). Re-run with --apply.")
        return

    # Reuse the PREVIEW's boundary, so --apply cannot act on a different row set
    # than THIS PROCESS's dry run. Deliberately not "the dry run the operator just
    # read" — that ran in a separate process and its boundary is gone; the rows
    # can differ. sweep_preview's docstring is careful about this and the comment
    # here was not (r5 nit 3).
    #
    # `retain_days` is threaded even though `cutoff` wins, so the two arguments
    # cannot disagree: the debug line inside sweep_delivered reports its own
    # `retain_days`, and it read the 7 default under `--retain-days 30` — the one
    # place an operator looks to confirm what they asked for.
    preview = sweep_preview(config.db_path, retain_days=retain_days)
    result = sweep_delivered(config.db_path, retain_days=retain_days, cutoff=preview["boundary"])
    click.echo(
        f"Swept {result['collapsed'] + result['aged_out']} row(s) "
        f"(collapsed {result['collapsed']}, aged out {result['aged_out']})."
    )


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

    from techne.sync.sinks import ensure_sinks_registered, list_sinks

    # Register first: in a fresh process the registry is empty, so listing without
    # it showed nothing, not even the built-in Archivist lanes (#1172).
    found = ensure_sinks_registered(ctx.obj["config"])
    registered = list_sinks()
    for name in sorted(found.refused):
        click.echo(f"  refused entry point {name} (not in sync.allowed_sinks)")
    for name, why in sorted(found.failed.items()):
        click.echo(f"  failed entry point {name}: {why}")
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

    from techne.sync.sinks import ensure_sinks_registered, get_sink

    # Register first, as `sinks list` does: a fresh process has none (#1172).
    ensure_sinks_registered(ctx.obj["config"])
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

    def _import_skip_is_benign(result: dict) -> bool:
        """True when a skip means "already done", not "went wrong" (techne#630).

        `import_transcript` is idempotent by design: a session that already has
        observations is skipped with reason "already has N observations". That is
        the feature working, and it must not become a non-zero exit — otherwise
        re-running an import, the safest possible habit, starts reporting failure.

        A metadata-only transcript (techne#941) is benign for the same reason: the
        file is valid, there was simply no conversation in it to import.
        """
        from techne.platforms.claude_code.transcript_import import NO_EVENTS_REASON

        reason = result.get("reason") or ""
        return "already has" in reason or reason == NO_EVENTS_REASON

    def _import_one(p):
        """Single-file import with the SAME net as import_directory's per-file handler.

        Without it the two entry points disagree on the same file: a corrupt archive
        passed as a directory reports `skip … reason: error: …` and exits 0, while the
        identical file passed directly raised a raw traceback — or, for a TRUNCATED
        archive, whose EOFError Click reads as an abort, printed a bare `Aborted!` with
        no filename and no reason.

        The filename goes in the REASON, not in ``session_id``: that field renders
        truncated to 8 chars (it holds uuids), which would clip a name to "sess-bro".
        And it is ``p.name`` rather than ``session_id_for(p)`` because here the path is
        whatever the caller typed rather than a glob hit — ``session_id_for`` is itself
        one of the raisers, so calling it in the handler would fault a second time
        mid-exception. ``session_id`` stays None, matching ``import_transcript``'s own
        no-records case.
        """
        try:
            return import_transcript(
                config.db_path,
                p,
                include_user_prompts=include_prompts,
                force=force,
            )
        except Exception as e:
            return {
                "session_id": None,
                "skipped": True,
                "reason": f"error reading {p.name}: {e}",
            }

    results = (
        [_import_one(path)]
        if path.is_file()
        else import_directory(
            config.db_path,
            path,
            include_user_prompts=include_prompts,
            force=force,
        )
    )

    # PERSIST THREADS FOR WHAT WAS IMPORTED (techne#954). Import used to stop at
    # observations, so every imported thread was unpersisted: reflect's emit cursor
    # fell back to prompt_hash and re-offered ingested threads after any knowledge
    # write. A benign skip ("already has N observations") is included, with
    # replace=False, so re-running `techne import` repairs a store imported before
    # this fix and leaves hook-detected sessions alone.
    from techne.lifecycle import ensure_threads

    threads_total = 0
    threaded_sessions = 0
    thread_errors: list[str] = []
    for r in results:
        sid = r.get("session_id")
        if not sid or (r.get("skipped") and not _import_skip_is_benign(r)):
            continue
        try:
            detected = ensure_threads(config, sid, replace=not r.get("skipped"))
        except Exception as e:
            thread_errors.append(sid)
            click.echo(f"  threads {sid[:8]}  — detection failed: {e}", err=True)
            continue
        if detected:
            threads_total += detected["threads"]
            threaded_sessions += 1

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
            # SAY WHEN TURNS WERE SKIPPED AS REPLAYS (techne#962). Without this an
            # all-replay resume printed "0 tool obs, 0 prompts" -- byte-identical to
            # an empty transcript -- so the one thing an operator needs to tell
            # those apart was in the return dict and nowhere they would see it.
            replayed = r.get("replayed_skipped") or 0
            replay_note = f", {replayed} replayed turn(s) skipped" if replayed else ""
            click.echo(
                f"  ok   {sid}  — {r['tool_obs']} tool obs, "
                f"{r['prompt_obs']} prompts{replay_note}{marker}"
                f"  ({r.get('project_path', '')})"
            )
    click.echo(
        f"\n{imported} imported, {skipped} skipped — "
        f"{tool_total} tool obs + {prompt_total} prompts added, "
        f"{summaries_set} session summaries set, "
        f"{threads_total} threads detected in {threaded_sessions} sessions"
    )
    if thread_errors:
        click.echo(
            f"thread detection failed for {len(thread_errors)} session(s); retry one with "
            "`techne threads redetect <session>`",
            err=True,
        )
    # A SINGLE FILE THAT FAILED EXITS NON-ZERO (techne#630, Richard: "we should fix").
    #
    # The partial-success argument that justifies exit 0 for a DIRECTORY — some
    # imported, some skipped, the run did useful work — does not apply to one file,
    # where a failure is the whole outcome. Reporting that as success is the
    # "no silent failures" rule broken at the exit code while the stdout is honest.
    #
    # GATED ON FAILURE, NOT ON `imported == 0`, and the distinction is the reason
    # this is not a one-line change. `skipped` covers four different outcomes:
    #
    #   "already has N observations"  -> IDEMPOTENT SUCCESS. Re-importing a file
    #                                    techne already holds is the designed
    #                                    behaviour; failing here would break every
    #                                    re-run and punish the safe habit.
    #   NO_EVENTS_REASON              -> SUCCESS (techne#941). A valid metadata-only
    #                                    transcript: a titled session that never
    #                                    took a turn. Nothing to import is correct.
    #   "empty"                       -> failure: nothing to import from a file the
    #                                    caller explicitly named.
    #   "error: ..." / "error reading" -> failure.
    #
    # A flat `not imported` test would have turned the first two of those into
    # errors. Measured before changing anything: `import_transcript`/
    # `import_directory` are called from cli.py ONLY (the automatic capture path
    # uses backfill_uncaptured from session_end), and nothing on this machine
    # consumes the exit code — no scripts, no cron, no hooks, and zero invocations
    # across 2,648 lines of shell history. So the blast radius is a human at a
    # prompt, which is exactly who benefits from a non-zero exit.
    #
    # The DIRECTORY branch is untouched: #622's `test_both_entry_points_agree_on_
    # the_same_broken_file` asserted equal exit codes, and that test is updated
    # rather than deleted — the entry points still agree on OUTPUT, which is what
    # made the original claim worth making.
    if path.is_file() and skipped and not _import_skip_is_benign(results[0]):
        raise click.ClickException(f"{path.name}: {results[0].get('reason', 'import failed')}")
    # Same single-file rule for detection: the observations landed, but a session
    # with no threads is the #954 state, and that is the whole outcome for one file.
    if path.is_file() and thread_errors:
        raise click.ClickException(f"{path.name}: imported, but thread detection failed")


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


@main.command()
@click.option(
    "--since",
    "since_days",
    type=int,
    default=None,
    help="Window to the last N days (default: every retained row; injections keep 180 days).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def audit(ctx, since_days, as_json):
    """Utilization audit (techne#329): injection read-path + ledger read/write ratios.

    Two descriptive panels — which injection sources fire and at what token cost,
    and the read-vs-write balance per memory domain — plus two attribution panels
    ("was the injected item actually used"), one per rail family. Neither is
    causation; the true counterfactual is UNMEASURED and tracked by techne#682.
    """
    config = ctx.obj["config"]
    from techne.core.utilization import (
        edit_rail_attribution,
        injection_utilization,
        read_write_ledger,
        recall_attribution,
    )

    inj = injection_utilization(config.db_path, since_days=since_days)
    ledger = read_write_ledger(config.db_path, since_days=since_days)
    attrib = edit_rail_attribution(config.db_path, since_days=since_days)
    recall = recall_attribution(config.db_path, since_days=since_days)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "since_days": since_days,
                    "injection": inj,
                    "ledger": ledger,
                    "edit_rail_attribution": attrib,
                    "recall_attribution": recall,
                },
                indent=2,
            )
        )
        return

    window = f"last {since_days}d" if since_days else "all-time"
    click.echo(f"techne utilization audit — {window} (N={ledger['total_observations']:,} obs)\n")

    click.echo("INJECTION READ-PATH (context_injections)")
    if not inj:
        click.echo("  (no injections logged yet)")
    else:
        click.echo(
            f"  {'source':22} {'events':>7} {'hits':>6} {'avg_tok':>8} "
            f"{'sessions':>9} {'empty':>6} {'radius':>7}"
        )
        for r in inj:
            # avg_radius (pre-cap blast-radius) only applies to capping sources
            # like codex_edit_rail; '—' where the source doesn't cap. (techne#343)
            radius = "—" if r.get("avg_radius") is None else f"{r['avg_radius']:g}"
            click.echo(
                f"  {r['source']:22} {r['events']:>7} {r['total_hits']:>6} "
                f"{r['avg_tokens']:>8} {r['sessions']:>9} {r['empty_events']:>6} {radius:>7}"
            )
    click.echo("")

    m, cx = ledger["memory"], ledger["codex"]
    mem_ratio = (
        f"{m['writes_per_read']} writes/read"
        if m["writes_per_read"] is not None
        else "no reads yet"
    )
    codex_ratio = (
        f"1 query / {cx['edits_per_read']:.0f} edits"
        if cx["edits_per_read"] is not None
        else "no codex reads"
    )
    arch = ledger["architect"]
    lib = ledger["librarian"]
    arch_iv = ledger["archivist"]
    arch_note = "" if arch["read"] else "   → never queried"
    click.echo("LEDGER READ/WRITE")
    click.echo(f"  memory:    store {m['write']} · recall+search {m['read']}   → {mem_ratio}")
    click.echo(f"  codex:     reads {cx['read']} · Edit {cx['edit']:,}   → {codex_ratio}")
    click.echo(f"  architect: pattern reads {arch['read']}{arch_note}")
    # THE `else` HERE WAS UNREACHABLE ONCE #724 SHIPPED `librarian_search` and #727
    # made `mcp_read_tool` a real derivation — it printed "CLI-only store, no MCP read
    # tool" for a tool the docs now tell operators to call. Kept as a branch rather than
    # deleted, because the DISTINCTION is still live for any future domain that has no
    # MCP surface: "0 because nobody called it" and "0 because there is nothing to call"
    # must not print the same line. (Prudence, #725 r4.)
    for name, row in (("librarian", lib), ("archivist", arch_iv)):
        if row["mcp_read_tool"]:
            note = "" if row["read"] else "   → never queried"
            click.echo(f"  {name}: reads {row['read']}{note}")
        else:
            click.echo(f"  {name}: no MCP read tool — not visible to this ledger")
    click.echo("")

    # Edit-rail proximity-attribution (techne#374).
    click.echo("EDIT-RAIL ATTRIBUTION (codex_edit_rail)")
    if not attrib["injections"]:
        click.echo("  (no codex_edit_rail injections logged yet)")
    else:
        pay = attrib["payload"]

        # In this branch injections > 0, so the *_pct fields are non-None; payload
        # pcts can still be None if every injection had an empty payload.
        def _p(v):
            return f"{v:g}%" if v is not None else "—"

        click.echo(
            f"  window: next {attrib['next_edits']} edits ≤ {attrib['window_minutes']:g} min "
            f"· N={attrib['injections']} injections"
        )
        click.echo(
            f"  a dependent's FILE re-edited   : {attrib['file_attributed']:>3} "
            f"({_p(attrib['file_attributed_pct'])} of all · "
            f"{_p(attrib['file_attributed_pct_attributable'])} of those w/ a follow-up edit)"
        )
        click.echo(
            f"  a dependent's NAME in the edit : {attrib['symbol_attributed']:>3} "
            f"({_p(attrib['symbol_attributed_pct'])} of all · "
            f"{_p(attrib['symbol_attributed_pct_attributable'])} w/ follow-up)   ← stronger, name-based"
        )
        click.echo(f"  no edit followed in window     : {attrib['no_edits_in_window']:>3}")
        click.echo(
            f"  payload: real {_p(pay['real_pct'])} · module-noise {_p(pay['module_pct'])} "
            f"· test {_p(pay['test_pct'])}   (100%-noise injections: {_p(attrib['noise_only_pct'])})"
        )
    click.echo("")

    # Knowledge-rail citation-attribution (techne#329 step 1's deferred join).
    click.echo("RECALL ATTRIBUTION (" + " + ".join(recall["sources"]) + ")")
    if not recall["injections"]:
        click.echo("  (no knowledge-rail injections logged yet)")
    else:

        def _q(v):
            return f"{v:g}%" if v is not None else "—"

        click.echo(
            f"  window: next {recall['next_actions']} actions ≤ "
            f"{recall['window_minutes']:g} min · N={recall['injections']} injections"
        )
        click.echo(
            f"  an injected id CITED in window : {recall['cited_in_window']:>4} "
            f"({_q(recall['cited_in_window_pct'])} of all · "
            f"{_q(recall['cited_in_window_pct_attributable'])} of those w/ a follow-up action)"
        )
        click.echo(
            f"  …cited anywhere later in session: {recall['cited_in_session']:>4} "
            f"({_q(recall['cited_in_session_pct'])})   ← ceiling, over-credits"
        )
        click.echo(f"  no action followed in window   : {recall['no_actions_in_window']:>4}")
        # Ledger-blind ≠ ignored (the librarian distinction, again): a session the
        # ledger never captured can't show a citation either way, so it is excluded
        # from the attributable base rather than scored as a miss.
        click.echo(
            f"  session not in ledger (excluded): {recall['unobserved_sessions']:>4}"
            "   ← unmeasurable, not ignored"
        )
        for src, s in recall["per_source"].items():
            if not s["injections"]:
                continue
            click.echo(
                f"    {src:18} N={s['injections']:>4}  "
                f"window {s['cited_in_window']:>3} · session {s['cited_in_session']:>3} "
                f"· blind {s['unobserved_session']:>3}"
            )
        # Which rail wrote the rows being shown (techne#995): is reflect earning its
        # injections? Per injected ROW, since one injection mixes origins.
        click.echo(
            "  by origin (per injected row; rows/never cited count every row, "
            "shown/cited exclude ledger-blind sessions):"
        )
        for name, o in recall["per_origin"].items():
            if not o["pairs"]:
                continue
            click.echo(
                f"    {name:12} rows {o['items']:>5} · never cited {o['items_never_cited']:>5} "
                f"· shown {o['pairs'] - o['unobserved']:>5} · cited in window "
                f"{o['cited_in_window']:>4} ({_q(o['cited_in_window_pct'])})"
            )
        click.echo(
            f"  items injected: {recall['items_injected']} · "
            f"never cited: {recall['items_never_cited']} "
            f"({_q(recall['items_never_cited_pct'])})"
        )
        top = [r for r in recall["items"] if r["cited_in_session"] == 0][:5]
        if top:
            worst = ", ".join(f"#{r['id']}×{r['encounters']}" for r in top)
            click.echo(f"  most-shown, never cited: {worst}")
    click.echo("")
    click.echo(
        "Both attribution panels are PROXY, not causation, and biased in OPPOSITE "
        "directions: the edit rail's is PROXIMITY (over-credits — the edit may have "
        "happened anyway); recall's is CITATION (under-credits — memory is usually "
        'used without naming an id). The counterfactual ("would it have happened '
        'WITHOUT the injection") is unmeasured — techne#682.'
    )


@main.group()
def prompts():
    """Operator-editable maintenance prompts (reflect/consolidate/dream) — techne#219.

    The prompts that drive knowledge maintenance ship with baked-in defaults but
    are overridable per-install: edit a file under `techne prompts path` and the
    change is picked up on the next reflect/consolidate/dream run — no code edit.
    A broken override falls back to the built-in default (see docs/prompts.md).
    """


@prompts.command("export")
@click.option("--overwrite", is_flag=True, help="Replace existing prompt files (discards edits).")
@click.pass_context
def prompts_export(ctx, overwrite):
    """Write the built-in maintenance prompts to <data_dir>/prompts/*.md to edit from."""
    config = ctx.obj["config"]
    from techne.core.prompts import export_defaults, prompts_dir

    res = export_defaults(config, overwrite=overwrite)
    click.echo(f"prompts dir: {prompts_dir(config)}")
    for name, status in res.items():
        click.echo(f"  {name}.md: {status}")
    if any(s == "skipped" for s in res.values()):
        click.echo("(existing files kept; pass --overwrite to replace them)")


@prompts.command("path")
@click.pass_context
def prompts_path(ctx):
    """Print the directory where prompt overrides live."""
    from techne.core.prompts import prompts_dir

    click.echo(str(prompts_dir(ctx.obj["config"])))


def _vector_next_steps(db_path: Path) -> list[str]:
    """What `init` should say about semantic recall (techne#944).

    A fresh install has no embedding backend unless `[vectors]` (or `[recommended]`)
    was installed, and a mistyped extra such as `[vec]` is only a pip WARNING, so
    the install "succeeds" and every search runs FTS-only. `capabilities` reports
    that plainly, but nothing sent a new user there. Uses the import probe and
    `vector_row_counts`, which never builds the backend — no model load, no
    download. (`vector_channel_status` is NOT usable here: it loads the model
    whenever a backend stamp exists, e.g. on a second `init`.)
    """
    from techne.core.capabilities import vectors_install_command
    from techne.core.embeddings import backend_available

    if not backend_available():
        return [
            "  Semantic recall is OFF: no embedding backend is installed, so search is",
            "  keyword-only (FTS). Install the extra, then embed what is stored:",
            f"    {vectors_install_command(__version__)}",
            "    techne vectors backfill",
        ]

    from techne.core.vector_status import vector_row_counts

    try:
        rows = sum((n or 0) for n in vector_row_counts(db_path).values())
    except Exception:
        # Reporting only; a status failure must not fail init. `rows=0` still
        # prints the backfill hint, which is harmless if vectors do exist.
        rows = 0
    if rows == 0:
        return [
            "  Semantic recall: backend installed, nothing embedded yet. Once there is",
            "  something stored (e.g. after `techne import`), run:",
            "    techne vectors backfill",
        ]
    return []


def _interactive() -> bool:
    """Both ends a terminal: with `techne init > log` the prompt would go into the log
    and init would look hung (#1030 r1)."""
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


# What an unreadable cwd costs, said once for init and the memory commands (#1153).
_UNREADABLE_CWD = (
    "The working directory can't be read (getcwd failed; on macOS a parent directory "
    "without read permission does this)."
)


def _readable_cwd() -> Path | None:
    """The cwd, or None when getcwd() itself fails (#1153). macOS's getcwd() raises
    EACCES under an unreadable ancestor, where Linux's returns the path regardless.
    None means "no project", not the home directory: finding the project needs the
    cwd, and home is a guess that would read another project's memory."""
    try:
        return Path.cwd()
    except OSError:
        return None


def _offer_auto_memory_off(spool_primary: bool) -> None:
    """Claude Code's auto-memory is a second memory layer beside techne (techne#1023).

    Running both drifts and double-injects (knowledge #426), so a new user is asked
    whether to turn it off. It is never switched off silently: it is another
    product's setting. Keyed on the RESOLVED posture, not on whether memory files
    were detected (#1030 r1): under spool-primary techne works WITH Claude Code's
    memory, so nothing is offered and the message says how to move to techne-only.
    """
    from techne.core.checks import posture_switch_command
    from techne.platforms.claude_code.hooks import auto_memory_enabled, disable_auto_memory

    if not auto_memory_enabled():
        return
    click.echo("")
    if spool_primary:
        click.echo(
            "Claude Code auto-memory is on, and techne is working alongside it "
            "(spool-primary). techne does not take over Claude Code's memory: your "
            "MEMORY.md files stay where they are, and techne writes memory of its own "
            "only with `reflect.enabled: true` in ~/.techne/config.yaml. To use techne "
            "alone: turn reflect on, set `autoMemoryEnabled: false` in "
            f"~/.claude/settings.json, then run `{posture_switch_command('observe')}`."
        )
        return
    why = (
        "Claude Code auto-memory is on. It is a second memory layer beside techne: both "
        "write and both inject, which drifts and duplicates context."
    )
    if not _interactive():
        click.echo(why)
        click.echo("Recommended: set `autoMemoryEnabled: false` in ~/.claude/settings.json.")
        return
    click.echo(why)
    if click.confirm("Turn Claude Code auto-memory off (only that setting changes)?", default=True):
        disable_auto_memory()
        click.echo("Claude Code auto-memory: off (autoMemoryEnabled: false). Next session on.")
    else:
        click.echo("Left on. Change it with `autoMemoryEnabled` in ~/.claude/settings.json.")


def _warm_vectors(config) -> list[str] | None:
    """Download the embedding model once and embed what is stored (techne#1021).

    Without this a fresh install never gets semantic search: the SessionStart
    backfill passes `--no-download` so a cold cache cannot hang startup (#815), and
    nothing else fetches the model. `init` is where a one-time wait is expected.

    Returns None when no backend is installed (the caller prints the install hint),
    [] when the model is ready, or lines explaining a failure. Never raises: a
    failed download must not fail the install.
    """
    from techne.core.embeddings import (
        _get_embedding_model,
        _select_backend,
        backend_available,
        model_cached,
    )

    if not backend_available():
        return None
    # ONE LOAD, NEVER AN OFFLINE PROBE FIRST (#1029 r1, Critical). Probing with
    # allow_download=False sets HF_HUB_OFFLINE for model2vec, and huggingface_hub
    # keeps its offline state for the life of the process after the env var is
    # restored, so the download that followed always failed. model_cached() answers
    # by looking at the cache directory: no load, no env, no network.
    cached = model_cached(config)
    if not cached:  # False, or None = cannot tell
        _, model = _select_backend(config)
        click.echo(f"Downloading the embedding model ({model}) — one time, then cached...")
    try:
        _get_embedding_model(config, allow_download=not cached)
    except Exception as e:
        return [
            f"  Semantic recall: the embedding model could not be loaded ({e}).",
            "  Search is keyword-only until you run, with network access:",
            "    techne vectors backfill",
        ]
    from techne.core.search import backfill_vectors

    try:
        counts = backfill_vectors(config.db_path, allow_download=False)
    except Exception as e:
        return [
            f"  Semantic recall: the model is ready but embedding failed ({e}). Run:",
            "    techne vectors backfill",
        ]
    click.echo(f"Semantic recall ready: embedded {sum(counts.values())} stored row(s).")
    return []


def _vector_lines(config, setup_vectors: bool) -> list[str]:
    if setup_vectors:
        lines = _warm_vectors(config)
        if lines is not None:
            return lines
    return _vector_next_steps(config.db_path)


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
@click.option(
    "--skills/--no-skills",
    "install_skills",
    default=True,
    show_default=True,
    help=(
        "Install techne's operational procedures (self-care, techne-promote) into the "
        "platform's command directory, and the research-guide stubs (investigate-url, "
        "survey-field) as skills. They are version-coupled to this techne, which is "
        "why they ship with it rather than from a marketplace (techne#641)."
    ),
)
@click.option(
    "--force-skills",
    is_flag=True,
    default=False,
    help="Overwrite a procedure you have edited. Off by default: techne reports a "
    "diverged copy rather than silently restoring its own.",
)
@click.option(
    "--vectors/--no-vectors",
    "setup_vectors",
    default=True,
    show_default=True,
    help="Download the embedding model (one time, tens of MB) and embed what is stored, "
    "so search is semantic from the start. --no-vectors keeps search keyword-only until "
    "`techne vectors backfill` (techne#1021).",
)
@click.option(
    "--allow-worktree",
    is_flag=True,
    default=False,
    help="Register even though techne runs from a git worktree (e.g. a bare repo's "
    "worktree is your only checkout). Prints a warning: pruning that worktree breaks "
    "the MCP server and every hook until you re-run `techne init`.",
)
def init(
    ctx,
    platform,
    identity_dir,
    posture,
    install_skills,
    force_skills,
    setup_vectors,
    allow_worktree,
):
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
    # Not "does ~/.claude exist": registration creates it, so every init after the
    # first would claim Claude Code is here (#1123, #1137). `claude` on PATH or its
    # projects/ dir (techne never creates it) says it has actually run.
    cc_present = False
    if platform == "claude-code":
        from techne.platforms.claude_code import register_hooks, register_mcp
        from techne.platforms.claude_code.paths import claude_code_has_run

        cc_present = claude_code_has_run()

        try:
            register_hooks(config, allow_worktree=allow_worktree)
            register_mcp(config)
        except RuntimeError as exc:
            # register_hooks refuses to wire an interpreter it cannot validate
            # (techne#458) or a path inside a git worktree (techne#434). Both are
            # operator-actionable and expected, not crashes — render them as one
            # clear message instead of a traceback. ClickException exits 1, so init
            # still fails loudly; nothing has been written to ~/.claude by then,
            # because both guards run before the launcher/settings are touched.
            raise click.ClickException(str(exc)) from exc
        click.echo("Claude Code hooks registered")
        click.echo("MCP server registered")
        if not cc_present:
            # Still wired (#1123): the README's first command is a bare `techne init`,
            # and a user who installs Claude Code afterwards gets a working setup.
            click.echo(
                "Claude Code not found; techne's hooks are registered and take effect "
                "when you install it. For no Claude Code integration, run "
                "`techne init --platform standalone`."
            )

        # Operational procedures (techne#641). Deliberately AFTER the two guards
        # above: if register_hooks refuses this install, we have not scattered
        # markdown into ~/.claude for an install that is about to fail.
        if install_skills:
            from techne.platforms.claude_code.agents import register_agents
            from techne.platforms.claude_code.skills import (
                register_guide_stubs,
                register_skills,
            )

            for r in register_skills(config, force=force_skills):
                line = f"Skill {r.name}: {r.action}"
                if r.detail:
                    line += f" — {r.detail}"
                click.echo(line)

            # The research guides' stubs (techne#1040). Without them the guides reach
            # a host only if an agent notices the offer inside the guide, and #988 was
            # what happened instead: hand-kept copies that drifted both ways.
            for r in register_guide_stubs(config, force=force_skills):
                line = f"Skill {r.name}: {r.action}"
                if r.detail:
                    line += f" — {r.detail}"
                click.echo(line)

            # The research sandbox rides the same flag as the procedures (techne#754).
            # It is not optional in a different way: `techne guide investigating` tells
            # an agent to research inside it, so an install that has the guide and not
            # the definition ships an instruction nobody can follow.
            for r in register_agents(config, force=force_skills):
                line = f"Agent {r.name}: {r.action}"
                if r.detail:
                    line += f" — {r.detail}"
                click.echo(line)

    # Resolve coexistence posture (techne#107 PR4).
    cwd_readable = _readable_cwd() is not None
    if not cwd_readable:
        click.echo(
            f"{_UNREADABLE_CWD} Skipped curated-memory detection; run `techne init` "
            "from your project directory to detect it."
        )
    cc = cwd_readable and bool(find_curated_memory_files())
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
        if cc:
            cc_note = "CC curated memory WAS detected, but observe was requested"
        elif not cwd_readable:
            cc_note = "CC curated memory detection skipped"
        else:
            cc_note = "no CC curated memory detected"
        click.echo(
            "Posture: observe — techne captures + (if you enable reflect) may author its own"
            f" knowledge ({cc_note})."
        )

    # Save config. Report the resolved path save() actually wrote to (honors
    # TECHNE_HOME), not the import-time DEFAULT_CONFIG snapshot.
    saved_path = techne_home() / "config.yaml"
    config.save(saved_path)
    click.echo(f"Config saved: {saved_path}")
    if config.config_migration is not None:
        # One line naming what the #1070 migration un-pinned; the debug log has
        # the same list from load.
        click.echo(config.config_migration.summary(config.config_backup))

    # After the save, so an Abort (EOF, Ctrl-C) at the prompt cannot leave a
    # registered install with no config.yaml (#1030 r1).
    # Not when Claude Code is absent: its auto-memory "defaults on" only in the
    # sense that no settings file says otherwise, and there is nothing to turn off.
    if platform == "claude-code" and cc_present:
        _offer_auto_memory_off(spool_primary=resolved_mode == "spool-primary")

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
        vector_steps = _vector_lines(config, setup_vectors)
        if vector_steps:
            click.echo("")
            for line in vector_steps:
                click.echo(line)
        click.echo("")
        click.echo("  See which features are on: techne capabilities")
        return

    click.echo("\ntechne initialized. Run 'techne status' to verify.")
    for line in _vector_lines(config, setup_vectors):
        click.echo(line)


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
    from techne.exit_codes import EX_CONFIG, StartupConfigError
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
    except StartupConfigError as exc:
        # Ordered BEFORE RuntimeError: StartupConfigError subclasses it, and a
        # broad handler first would swallow the exit contract. Exiting
        # EX_CONFIG tells the supervisor this failure is not transient, so
        # `RestartPreventExitStatus=78` in the unit stops the loop on the
        # FIRST failure instead of respawning forever.
        click.echo(f"error: {exc}", err=True)
        ctx.exit(EX_CONFIG)
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


@main.command("deposit")
@click.option(
    "--target",
    required=True,
    help="Foreign techne home directory to deposit into (e.g. /home/agent/.techne)",
)
@click.option(
    "--session-id", required=True, help="Session identifier for the deposited observation"
)
@click.option("--content", default=None, help="Observation text; if omitted, read from stdin")
@click.option(
    "--tool-name", default="Observation", help="Tool or event name (e.g. Edit, turn_complete)"
)
@click.option("--agent-identity", default="", help="Agent identity for the session row")
@click.option("--tier", default="hot", type=click.Choice(["hot", "warm", "cold"]))
@click.pass_context
def deposit(ctx, target, session_id, content, tool_name, agent_identity, tier):
    """Deposit a raw observation into a foreign techne store (federation brick #221).

    The explicit 'ephemeral producer deposits raw into a store it doesn't own'
    surface — the techne side of the accept-fast / distill-async pattern.  The
    caller's own techne store is NOT touched.

    The foreign store's distiller (drain.py, pointed at --target) picks up the
    deposited observations asynchronously and distills them into knowledge
    candidates.  No LLM call happens here; deposit is a fast, durable write.

    --target is the foreign techne home directory (the directory that contains
    techne.db, i.e. the value drain.py's TECHNE_HOME points at).  The db is
    auto-created if the directory exists but the db does not.
    """
    import sys

    from techne.config import Config
    from techne.core.state import record_observation

    if content is None:
        content = sys.stdin.read()
    content = content.strip()
    if not content:
        raise click.ClickException("deposit requires content (via --content or stdin)")

    # Build a config targeting the foreign store.  Only db_path matters for deposit —
    # distillation config lives in the distiller's own environment, not here.
    # The caller's ctx.obj["config"] (local store) is deliberately NOT used as the
    # base, keeping local and foreign configs fully independent.
    foreign_config = Config()
    foreign_config.db_path = Path(target) / "techne.db"

    result = record_observation(
        foreign_config,
        session_id,
        content,
        tool_name=tool_name,
        agent_identity=agent_identity,
        tier=tier,
    )
    click.echo(
        f"deposited session={result['session_id']} store={foreign_config.db_path} "
        f"tool={tool_name} ({len(content)} chars)"
    )


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
    from techne.core.state import ABANDONED_SESSION_IDLE_HOURS
    from techne.lifecycle import run_session_end

    # A replay, not only a live end (issue #986): a session silent past the sweep's
    # idle window gets its end derived from last activity and stamped 'derived',
    # instead of a clean exit at "now" that never happened.
    result = run_session_end(
        config, session_id, summary=summary, derive_if_idle_hours=ABANDONED_SESSION_IDLE_HOURS
    )
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
        click.echo(f"memory doctor --claims — {report['deed_count']} deed(s) considered")
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
        click.echo(
            json.dumps(
                {
                    "memory_present": True,
                    "deed_count": report["deed_count"],
                    "memory_files": report.get("memory_files", []),
                    "note": "no-model path; use MCP tools to complete",
                },
                indent=2,
            )
        )


@memory.command("doctor")
@click.option(
    "--since",
    "since_days",
    type=int,
    default=None,
    help="Only consider deeds from the last N days (default: all).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option(
    "--claims",
    "do_claims",
    is_flag=True,
    help="Claim-level reconciliation via the resident model (techne#107 PR3).",
)
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
    if _readable_cwd() is None:
        raise click.ClickException(f"{_UNREADABLE_CWD} Run this from your project directory.")

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
    if _readable_cwd() is None:
        raise click.ClickException(f"{_UNREADABLE_CWD} Run this from your project directory.")

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
    click.echo(f"memory sync — {stored} file(s) drained (stored/updated) of {found} found")


@main.group(invoke_without_command=True)
@click.pass_context
def agents(ctx):
    """Install and inspect techne's bundled subagent definitions (techne#754).

    ONE DEFINITION TODAY: `research-readonly`, the web-only sandbox that
    `techne guide investigating` tells an agent to research inside. That instruction
    is UNENFORCEABLE without this file — an operator who does not have it reaches for
    a general research agent, which has a shell, and an agent that can both read
    attacker-controlled pages and write to the pattern store is one injection away
    from writing whatever a page tells it to.

    SEPARATE FROM `techne skills` because the failure modes differ. A drifted skill
    gives worse research. A drifted sandbox gives an agent more tools than the
    procedure assumes, so `status` calls out a widened tool list specifically rather
    than reporting a bare "user-modified".
    """
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(agents_status_cmd)


@agents.command("status")
@click.pass_context
def agents_status_cmd(ctx):
    """Report each definition as unchanged / edited / widened / absent."""
    from techne.platforms.claude_code.agents import agents_dir, agents_status

    # Name the directory. On a non-Claude-Code install the bare "not installed, run
    # `techne agents install`" gives no hint that it means ~/.claude/agents, which is
    # the asymmetry with `skills status` Prudence flagged (#807 r1).
    click.echo(f"  target: {agents_dir()}")
    for r in agents_status(ctx.obj["config"]):
        line = f"  {r.name:22} {r.action}"
        click.echo(line)
        if r.detail:
            click.echo(f"      {r.detail}")


@agents.command("install")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite a definition you have edited. Off by default: techne reports a "
    "diverged copy rather than silently restoring its own.",
)
@click.pass_context
def agents_install(ctx, force):
    """Write the bundled subagent definitions into the platform's agents directory."""
    from techne.platforms.claude_code.agents import register_agents

    for r in register_agents(ctx.obj["config"], force=force):
        click.echo(f"  {r.name:22} {r.action}  {r.path}")
        if r.detail:
            click.echo(f"      {r.detail}")


@agents.command("remove")
@click.pass_context
def agents_remove(ctx):
    """Remove only the copies techne wrote and still owns."""
    from techne.platforms.claude_code.agents import unregister_agents

    results = unregister_agents(ctx.obj["config"])
    if not results:
        click.echo("nothing installed by techne to remove")
    for r in results:
        click.echo(f"  {r.name:22} {r.action}  {r.path}")
        if r.detail:
            click.echo(f"      {r.detail}")


@main.group(invoke_without_command=True)
@click.pass_context
def skills(ctx):
    """Install and inspect techne's bundled operational procedures (techne#641).

    `self-care` and `techne-promote` are version-coupled to techne — they drive its
    CLI and know this version's store layout — so they ship WITH the package rather
    than from a marketplace, where they would drift from the tool they operate.

    This group exists because `techne init` is not enough: an existing user upgrades
    with `pipx install --force`, never re-runs init, and would otherwise never
    receive them. That is exactly the 0.11/0.12 partner who has never run a hygiene
    pass and most needs one.
    """
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(skills_status_cmd)


@skills.command("status")
@click.pass_context
def skills_status_cmd(ctx):
    """Report each procedure and guide stub as current / outdated / user-modified / absent."""
    from techne.platforms.claude_code.skills import (
        guide_stubs_dir,
        guide_stubs_status,
        skills_dir,
        skills_status,
    )

    config = ctx.obj["config"]
    click.echo(f"Skills directory: {skills_dir()}")
    for r in skills_status(config):
        line = f"  {r.name}: {r.action}"
        if r.detail:
            line += f" — {r.detail}"
        click.echo(line)
    click.echo(f"Guide stubs directory: {guide_stubs_dir()}")
    for r in guide_stubs_status(config):
        line = f"  {r.name}: {r.action}"
        if r.detail:
            line += f" — {r.detail}"
        click.echo(line)


@skills.command("install")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite a procedure you have edited (default: report it and leave it).",
)
@click.pass_context
def skills_install(ctx, force):
    """Write the bundled procedures, and the research-guide stubs, for the platform."""
    from techne.platforms.claude_code.skills import register_guide_stubs, register_skills

    config = ctx.obj["config"]
    for r in [*register_skills(config, force=force), *register_guide_stubs(config, force=force)]:
        line = f"{r.name}: {r.action} -> {r.path}"
        if r.detail:
            line += f"\n  {r.detail}"
        click.echo(line)


@skills.command("remove")
@click.pass_context
def skills_remove(ctx):
    """Remove only the copies techne wrote and still owns."""
    from techne.platforms.claude_code.skills import unregister_guide_stubs, unregister_skills

    config = ctx.obj["config"]
    results = [*unregister_skills(config), *unregister_guide_stubs(config)]
    if not results:
        click.echo("nothing installed by techne")
    for r in results:
        line = f"{r.name}: {r.action}"
        if r.detail:
            line += f" — {r.detail}"
        click.echo(line)


@main.group(invoke_without_command=True)
@click.pass_context
def reflect(ctx):
    """Inspect and manage end-of-session reflection candidates.

    Reflection is opt-in (off by default; enable via reflect.enabled): an LLM
    scans each detected thread at session_end and proposes durable knowledge /
    identity entries. While config.reflect.write_to='candidates' the proposals
    land here for review and do NOT touch the live knowledge / identity tables.
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
    # The DEBT ledger, which is a different question from the CANDIDATE counts above:
    # candidates measure what reflection proposed, the queue measures what reflection
    # still OWES. They were never shown together, which is part of why an undrained
    # queue went unnoticed for weeks (tenishai/techne#644).
    from techne.core.reflect import reflection_debt_stats

    d = reflection_debt_stats(config)
    click.echo(
        f"Reflection debt: {d['pending']} thread(s) owed across "
        f"{d['sessions_pending']} session(s); {d['drained']} of {d['total']} paid"
    )
    if d["oldest_pending"]:
        click.echo(f"  oldest owed: {d['oldest_pending']}")
    if d["parked"]:
        click.echo(
            f"  parked: {d['parked']} below-threshold row(s), not counted as owed "
            "(`techne reflect reconcile` re-checks them)"
        )


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
        if r.get("revised_content"):
            # The proposal stays above; this is what was applied (techne#952).
            click.echo(f"  revised to: {r['revised_content']}")
            if r.get("decision_reason"):
                click.echo(f"  reason: {r['decision_reason']}")
        if r["tags"]:
            click.echo(f"  tags: {r['tags']}")
        click.echo(
            f"  src: session={r['source_session_id'][:16]}  "
            f"thread={r['source_thread_id']}  model={r['model_used']}"
        )
        click.echo(f"  created: {r['created_at']}")
        if r["status"] == "promoted" and r.get("promoted_to_id"):
            click.echo(f"  promoted_to: knowledge.id={r['promoted_to_id']}")
        # Faithfulness gate (techne#217): flag merge/supersede candidates where
        # the replacement was found to drop a load-bearing source claim.
        # faithfulness_ok: NULL=unchecked, 1=faithful, 0=unfaithful.
        faith_ok = r.get("faithfulness_ok")
        if faith_ok == 0:
            warning = r.get("faithfulness_warning") or "a source claim may have been dropped"
            click.echo(f"  ⚠ FAITHFULNESS WARNING: {warning}")
            click.echo("    This candidate may drop a load-bearing claim from its source entries.")
            click.echo(
                "    Review carefully before promoting. (Run: techne reflect promote --help)"
            )
        elif faith_ok == 1:
            click.echo("  ✓ faithfulness: ok")
        # faith_ok IS NULL: unchecked — silently omit so the noise level stays low.
        if show_raw and r.get("raw_response"):
            click.echo("  --- raw LLM response ---")
            click.echo("  " + (r["raw_response"][:1000].replace("\n", "\n  ")))
        click.echo()


@reflect.command("emit")
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Session to reflect on (default: the newest session with unreflected work, any age).",
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

    from techne.core.reflect import emit_reflection_pass

    # ONE emit implementation, shared with the MCP tool. This command used to build
    # its own batch and never advanced the `reflect:emitted:` cursor, so a session
    # reflected through the CLI stayed "pending" forever and was re-nominated on
    # every SessionStart (tenishai/techne#645).
    result = emit_reflection_pass(config, session_id, limit=limit)
    batch = result.get("batch", [])
    session_id = result.get("session_id") or session_id
    if not session_id:
        click.echo("No session to reflect on.", err=True)
        ctx.exit(1)

    # Placeholder the agent fills in-place with its JSON candidate array per thread,
    # then feeds the SAME file to `ingest` — no id reconstruction needed.
    #
    # THIS LITERAL IS ALSO THE DECLINE SIGNAL, and the collision is deliberate but sharp
    # (Prudence, #901 r2): an item still blank at ingest is read as "the agent declined",
    # which stops emit re-offering it. `techne reflect undecline` is the way back.
    for it in batch:
        it["raw_response"] = ""
    payload = json.dumps(batch, indent=2)
    if output:
        output.write_text(payload, encoding="utf-8")
        click.echo(
            f"Emitted {len(batch)} thread prompt(s) for session {session_id[:16]} -> {output}",
            err=True,
        )
        click.echo(
            "Fill each item's raw_response, then `techne reflect ingest`. An item left "
            "blank counts as DECLINED and is not offered again — retract with "
            f"`techne reflect undecline --session {session_id[:16]}` (a prefix is fine).",
            err=True,
        )
    else:
        click.echo(payload)
    for key in ("note", "unemittable_note"):
        if result.get(key):
            click.echo(result[key], err=True)


@reflect.command("ingest")
@click.argument("responses_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
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

    from techne.core.reflect import ingest_reflection_batch

    items = json.loads(responses_file.read_text(encoding="utf-8"))
    # ONE ingest implementation, shared with the MCP tool. This command used to
    # write candidates and stop — `drain_reflection_queue` had a single production
    # caller in mcp_server, so the documented /techne-promote path paid reflection
    # debt and never marked it paid (tenishai/techne#644).
    totals = ingest_reflection_batch(config, items)
    skipped = totals.pop("skipped", 0)
    drained = totals.pop("drained", 0)

    click.echo(json.dumps(totals, indent=2))
    if skipped:
        click.echo(f"skipped {skipped} thread(s) with empty raw_response")
    click.echo(f"reflection debt drained: {drained} thread(s)")
    if config.reflect.write_to == "live":
        click.echo("write_to=live — candidates promoted straight to knowledge/identity.")
    else:
        click.echo("write_to=candidates — review with: techne reflect review")


#: Recorded when `reflect promote`/`discard` is run without `--by` (techne#953). This
#: was "human", so an agent that omitted the flag had its decision audited as a
#: person's: one external store showed an agent's promotes as its own name and the
#: same agent's discards as "human". An audit field should say "unknown" rather
#: than name the wrong actor. Rows already written as "human" cannot be told apart.
UNSPECIFIED_DECIDER = "unspecified"


@reflect.command("promote")
@click.argument("candidate_id", type=int)
@click.option(
    "--by",
    "decided_by",
    default=UNSPECIFIED_DECIDER,
    show_default=True,
    help="Who/what decided (audit trail). Pass it: the default records that nobody said.",
)
@click.option("--reason", "decision_reason", default=None, help="Why (audit trail).")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Override the faithfulness gate for a merge/supersede candidate that was "
    "flagged unfaithful. Review the FAITHFULNESS WARNING first.",
)
@click.option(
    "--content",
    "revised_content",
    default=None,
    help="Promote this text instead of the proposed content (add candidates only; "
    "requires --reason). The proposal is kept on the candidate for audit.",
)
@click.option(
    "--content-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Like --content, read from a file.",
)
@click.pass_context
def reflect_promote(
    ctx, candidate_id, decided_by, decision_reason, force, revised_content, content_file
):
    """Promote a pending candidate to the live knowledge or identity store.

    Unfaithful merge/supersede candidates (those flagged by the faithfulness check)
    are refused by default — use --force after reviewing the warning to override.

    --content promotes an edited version (techne#952): for a claim worth keeping that
    arrived wrapped in counts, ids or versions that will go stale.
    """
    config = ctx.obj["config"]
    from techne.core.reflect import promote_candidate

    if revised_content is not None and content_file is not None:
        click.echo("Error: use --content or --content-file, not both.", err=True)
        ctx.exit(1)
    if content_file is not None:
        revised_content = content_file.read_text(encoding="utf-8").strip()

    try:
        new_id = promote_candidate(
            config.db_path,
            candidate_id,
            decided_by=decided_by,
            decision_reason=decision_reason,
            force=force,
            revised_content=revised_content,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    revised = " (revised)" if revised_content is not None else ""
    if new_id is not None:
        click.echo(f"Promoted candidate #{candidate_id}{revised} → knowledge.id={new_id}")
    else:
        click.echo(f"Promoted candidate #{candidate_id}{revised} → identity")


@reflect.command("discard")
@click.argument("candidate_id", type=int)
@click.option(
    "--by",
    "decided_by",
    default=UNSPECIFIED_DECIDER,
    show_default=True,
    help="Who/what decided (audit trail). Pass it: the default records that nobody said.",
)
@click.option("--reason", "decision_reason", default=None, help="Why (audit trail).")
@click.pass_context
def reflect_discard(ctx, candidate_id, decided_by, decision_reason):
    """Discard a pending candidate. The row stays for audit; status flips to 'discarded'."""
    config = ctx.obj["config"]
    from techne.core.reflect import discard_candidate

    try:
        discard_candidate(
            config.db_path,
            candidate_id,
            decided_by=decided_by,
            decision_reason=decision_reason,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        ctx.exit(1)
    click.echo(f"Discarded candidate #{candidate_id}")


@reflect.command("undecline")
@click.option("--session", "session_id", required=True, help="Session whose declines to retract.")
@click.option(
    "--thread",
    "thread_ids",
    multiple=True,
    help="Retract only these thread ids (default: all declines for the session).",
)
@click.pass_context
def reflect_undecline(ctx, session_id, thread_ids):
    """Offer declined threads again.

    Leaving an item's `raw_response` blank tells `ingest` the thread was declined, and
    nothing re-offers a declined thread — that is deliberate, or `emit` and the decline
    chase each other and the drain loop never ends (tenishai/techne#901).

    But `emit` PRE-FILLS that same blank as a placeholder, so an ordinary partial fill
    looks exactly like a refusal. This is the way back. The debt was never drained, so
    nothing is being recovered here except the offer.
    """
    from techne.core.database import connect
    from techne.core.reflect import clear_declines

    config = ctx.obj["config"]
    # SESSION_ID MAY BE A PREFIX, AND IT HAD BETTER BE (Prudence, #901 r3). `emit` prints
    # the retraction command with `session_id[:16]` and a trailing "...", and no surface
    # prints the full id — `reconcile` truncates, `techne history` truncates, and the batch
    # file is the thing that is LOST in the case this command exists for. Matching the
    # extension_state key exactly meant the printed command, pasted verbatim, exited 0 and
    # retracted nothing. Same prefix resolution as `threads redetect`.
    #: AN EMPTY PREFIX IS A WILDCARD, AND THE DELTA THAT ADDED RESOLUTION INTRODUCED IT
    #: (Prudence, #901 r3). `LIKE '' || '%'` is `LIKE '%'`, so `--session ""` resolved to
    #: the newest session and retracted all six of its declines -- measured. `required=True`
    #: requires the FLAG, not a value, and the ordinary `--session "$SID"` with SID unset
    #: is the trigger. `clear_declines` guards the empty id, but resolution now runs in
    #: FRONT of that guard. `--session "."` reduces to the same wildcard via the rstrip.
    prefix = session_id.rstrip(".")
    sid = None
    if prefix:
        conn = connect(config.db_path)
        try:
            sid = _resolve_session_prefix(conn, prefix)
        finally:
            conn.close()
    if sid is None:
        # DISTINCT FROM "no declines", because they call for opposite actions and used to
        # print identically — a mistyped id read as "nothing to do".
        click.echo(f"No session matching: {session_id}", err=True)
        ctx.exit(1)

    n = clear_declines(config, sid, list(thread_ids) or None)
    if not n:
        click.echo(f"No declines recorded for session {sid[:16]}...", err=True)
        return
    click.echo(f"Retracted {n} decline(s) for session {sid[:16]}...; `emit` will offer them again.")


@reflect.command("reconcile")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Remove the superseded rows and park the below-threshold ones (default: dry run).",
)
@click.pass_context
def reflect_reconcile(ctx, apply_):
    """Retire reflection-queue rows left behind by a replaced thread detection.

    Splits undrainable debt into the two populations #646 conflated: rows whose
    thread no longer exists in the current detection (superseded — safe to retire)
    and rows whose thread is current but has fallen below
    `min_observations_per_thread` (kept, that is the real signal). --apply parks
    the second kind so the SessionStart banner stops counting it as owed (#870).

    Dry run unless --apply.
    """
    config = ctx.obj["config"]
    from techne.core.reflect import reconcile_reflection_queue

    report = reconcile_reflection_queue(config, apply=apply_)
    for sid, rows in report["by_session"].items():
        click.echo(
            f"{sid[:16]}  superseded={len(rows['superseded'])}  "
            f"below_threshold={len(rows['below_threshold'])}  "
            f"orphaned={len(rows['orphaned'])}  "
            f"declined={len(rows['declined'])}"
        )
    click.echo(
        json.dumps(
            {k: v for k, v in report.items() if k != "by_session"},
            indent=2,
        )
    )
    if not apply_ and report["superseded"]:
        click.echo(
            f"Dry run — {report['superseded']} superseded row(s) would be removed. "
            "Re-run with --apply.",
        )
    if report["below_threshold"]:
        click.echo(
            f"{report['below_threshold']} row(s) left in place: their thread is current but "
            "now below reflect.min_observations_per_thread — that is tenishai/techne#646's "
            "real signal, not residue.",
        )
        # PARKED, NOT DRAINED (tenishai/techne#870): the rows stay as evidence and only
        # leave the SessionStart banner's actionable debt figure.
        if apply_:
            click.echo(
                f"Parked {report['parked']} newly (any parked earlier stay parked): still "
                "queued, no longer counted as owed by the SessionStart banner.",
            )
        else:
            click.echo(
                "Re-run with --apply to park them: still queued, no longer counted as owed "
                "by the SessionStart banner.",
            )
    if report["unparked"]:
        click.echo(
            f"Un-parked {report['unparked']} row(s) that render again (the floor moved or "
            "the thread regained observations); they are owed again.",
        )
    if report["orphaned"]:
        click.echo(
            f"{report['orphaned']} row(s) the emit cursor already counts as delivered "
            "but never had their answers ingested (tenishai/techne#900). Left in place "
            "and never removed: the next `reflect emit` for those sessions re-offers "
            "them.",
        )
    if report["declined"]:
        click.echo(
            f"{report['declined']} row(s) the agent was offered and left blank. Still "
            "owed, never re-offered, never removed: a decline is a decision, and "
            "re-asking would make `reflect emit` loop forever (tenishai/techne#901). "
            "`techne reflect undecline --session <id-or-prefix>` offers them again.",
        )


@reflect.command("stats")
@click.pass_context
def reflect_stats(ctx):
    """Counts of pending / promoted / discarded candidates."""
    config = ctx.obj["config"]
    from techne.core.reflect import candidate_stats, reflection_debt_stats

    s = candidate_stats(config.db_path)
    s["debt"] = reflection_debt_stats(config)
    click.echo(json.dumps(s, indent=2))


@main.command()
@click.option(
    "--runtime",
    type=click.Choice(["claude-code", "opencode", "pi"]),
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

    Note: opencode and pi have no native auto-memory, so `differential` there
    has no control and `off-test` already proves the only techne path (the
    plugin/extension push).
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)

    from techne.probe import (
        ClaudeCodeAdapter,
        OpencodeAdapter,
        PiAdapter,
        run_differential,
        run_probe,
    )

    if runtime == "opencode":
        adapter = OpencodeAdapter(timeout=timeout, **({"model": model} if model else {}))
    elif runtime == "pi":
        adapter = PiAdapter(timeout=timeout, **({"model": model} if model else {}))
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
def librarian(ctx):
    """Librarian document store — index + recall full documents (techne#91).

    Addressable as `techne librarian`, one of the four domain names (techne#744).
    `techne library` still works and is not listed, because two spellings in `--help`
    teaches the ambiguity rather than the vocabulary.
    """
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


@librarian.command("index-transcripts")
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
    click.echo("Search them: techne librarian search <query>")


@librarian.command("index-markdown")
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
    click.echo("Search them: techne librarian search <query>")


@librarian.command("search")
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
        click.echo(f"    ↳ full: techne librarian show {d['id']}")
        click.echo()


@librarian.command("migrate-store")
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


@librarian.command("doctor")
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


@librarian.command("show")
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


@librarian.command("card")
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


@librarian.group("cards")
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
    # Say "active" and name the excluded rows. Printing a bare `Patterns: N` that
    # silently drops supersessions is what sent a reader to `SELECT COUNT(*)`,
    # find a different number, and report the counter as broken.
    line = (
        f"Patterns: {s['patterns']} active  "
        f"Investigations: {s['investigations']}  Skips: {s['skips']}"
    )
    if s["patterns_superseded"]:
        line += f"  (+{s['patterns_superseded']} superseded)"
    click.echo(line)


@architect.command("ingest")
@click.argument("markdown_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--verdict",
    type=click.Choice(["skip", "skip-with-flag", "patterns-only", "full-eval"]),
    default=None,
    help="Your judgment for the URLs carrying patterns. Omit to record "
    "'patterns-only' (the modal outcome). Before techne#428 this was hardcoded to "
    "'full-eval' and could not be set at all.",
)
@click.pass_context
def architect_ingest(ctx, markdown_file, verdict):
    """Seed architect.db from an architectural-patterns.md corpus."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import ingest_patterns_md

    counts = ingest_patterns_md(
        architect_db(config),
        markdown_file.read_text(encoding="utf-8"),
        verdict=verdict,
        # Same config value `add` uses — otherwise the two feeders write different
        # halves of UNIQUE(url, surveyed_by, lens) and one URL becomes two rows.
        surveyed_by=config.architect.surveyed_by,
        lens=config.architect.lens,
    )
    # SAY WHAT WAS DROPPED, BEFORE the JSON (techne#754). A section missing its
    # `- **Source**:` line was skipped with a log line nobody sees, so this printed
    # `"patterns": 0` and exited 0 on a hand-written file — and the bold markers are
    # load-bearing, which is not guessable. stderr, so `| jq` over the JSON still works.
    for sk in counts["skipped"]:
        # `kind` renders as "section" for a pattern section, so this line is
        # byte-identical to what it printed before techne#787 for that case, and
        # names a bullet as a bullet for the new one.
        click.echo(f'skipped {sk["kind"]} "{sk["label"]}": {sk["reason"]}', err=True)
    # GATED ON "NOTHING LANDED", NOT ON "SOMETHING WAS SKIPPED" (Prudence, #758 round 2).
    # `skipped` only fills for sections that reached `_parse_pattern_body`, which requires
    # exactly `## `. So a file using `# My pattern` with perfect metadata bullets printed
    # `"patterns_added": 0`, exit 0, and NOTHING on stderr — this command's own CHANGELOG
    # entry verbatim, for a mistake at least as guessable as the bold markers. Worse than
    # the silence it replaced, in fact, because operators have now learned that ingest
    # speaks up when something is wrong.
    # KEYED ON WHAT WAS PARSED, NOT ON WHAT WAS WRITTEN. The question this message answers
    # is "did techne understand your file?", and writes dedup — so a re-ingest of a correct
    # corpus writes nothing and would otherwise be told its headings are wrong.
    if not counts["patterns_seen"] and not counts["no_lifts_seen"]:
        if not counts["skipped"]:
            click.echo(
                f"no pattern sections found in {markdown_file.name} — sections must start "
                "with `## ` (two hashes) at the top level.",
                err=True,
            )
        click.echo(
            "nothing was ingested. `techne architect add` takes the fields directly "
            "if you would rather not hand-write the markdown.",
            err=True,
        )
    click.echo(json.dumps(counts, indent=2))


@architect.command("add")
@click.option("--name", required=True, help="What the pattern is called. Becomes its heading.")
@click.option("--source", required=True, help="URL the pattern was lifted from.")
@click.option(
    "--surveyed",
    default=None,
    help="Date you looked at it, YYYY-MM-DD. Defaults to today.",
)
@click.option("--body", default=None, help="The pattern itself. Or --body-file, or stdin.")
@click.option(
    "--body-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the body from a file instead of --body.",
)
@click.option(
    "--verdict",
    type=click.Choice(["skip", "skip-with-flag", "patterns-only", "full-eval"]),
    default=None,
    help="Your judgment for this URL. Omit to record 'patterns-only' (the modal outcome).",
)
@click.option(
    "--surveyed-by",
    default=None,
    help="Who surveyed it. Defaults to config `architect.surveyed_by`"
    " ('operator' unless you set it). Part of a row's identity —"
    " UNIQUE(url, surveyed_by, lens) — so a store with history wants"
    " the value it already uses.",
)
@click.option(
    "--lens",
    default=None,
    help="Angle the survey was done from. Defaults to config `architect.lens` ('infra').",
)
@click.pass_context
def architect_add(ctx, name, source, surveyed, body, body_file, verdict, surveyed_by, lens):
    """Add ONE pattern to architect.db from its fields — the feeder that ships.

    THE ARCHITECT HAD NO WAY IN THAT CAME WITH techne (techne#754). `ingest` takes a
    markdown corpus, and `investigate-url` / `survey-field` — the skills that actually
    fill the store — live in tenishai/skills, a separate install we never mention. So an
    operator installs techne, reads the tutorial, learns to ask the architect, and the
    thing that fills it ships from somewhere else.

    A COMMAND RATHER THAN A BUNDLED SKILL, because `techne init --platform` advertises
    five platforms and `techne/platforms/` holds one renderer. A skill installs into
    Claude Code's `commands/` and nowhere else; a CLI command works on all five.

    AND NOT A MARKDOWN GENERATOR. `ingest`'s format is not guessable — `- Source:` and
    `- **Source**:` differ by two asterisks and the first is silently parsed as body
    text — so a feeder that rendered markdown and re-parsed it would still fail in that
    way, one layer down. This writes through the same store API `ingest` writes through
    after parsing, so there is no format to get wrong.
    """
    import sys
    from datetime import datetime, timezone

    config = ctx.obj["config"]
    # None means 'not given on the command line' — fall back to config rather
    # than to a persona name (techne#754). These two are part of a row's identity
    # via UNIQUE(url, surveyed_by, lens), so the fallback must be stable.
    surveyed_by = surveyed_by or config.architect.surveyed_by
    lens = lens or config.architect.lens
    from techne.core.architect import architect_db
    from techne.core.patterns import (
        DEFAULT_INGEST_VERDICT,
        add_pattern,
        backfill_graph,
        upsert_investigation,
    )

    sources = [s for s in (body, body_file) if s is not None]
    if len(sources) > 1:
        raise click.UsageError("pass --body or --body-file, not both")
    if body_file is not None:
        body = body_file.read_text(encoding="utf-8")
    elif body is None:
        # stdin only when it is a PIPE. On a tty this would hang looking like the command
        # is working, which is the failure this command exists to stop making.
        if sys.stdin.isatty():
            raise click.UsageError("no body: pass --body, --body-file, or pipe it on stdin")
        body = sys.stdin.read()
    if not body.strip():
        raise click.UsageError("the body is empty — a pattern with no body is not a pattern")

    # NORMALIZE THE WAY THE PARSER DOES, NOT MERELY ACCEPT WHAT IT ACCEPTS (Prudence, #758
    # Critical). `_URL_RE` and `_DATE_RE` are UNANCHORED EXTRACTORS — `_DATE_RE` carries a
    # capture group precisely so callers take `group(1)`, and `_parse_pattern_body` does.
    # This used them as booleans and then stored the RAW argument, so sharing the regex
    # bought acceptance parity while normalization — where the disagreement actually lives
    # — was never shared at all. Measured at the database:
    #
    #   `--source "https://github.com/x/y — src/foo.ts"`, which is the shape
    #   `export_patterns_md` itself teaches, stored whole. `investigations` is
    #   UNIQUE(url, surveyed_by, lens), so add and ingest MINT DIFFERENT ROWS FOR ONE URL:
    #   add → export → re-ingest turned one investigation into two, against the invariant
    #   `test_export_roundtrips_with_ingest` already names.
    #
    #   `--surveyed "surveyed 2026-01-01 roughly"` accepted and stored raw. patterns.py:50
    #   records that `surveyed_at` is a zero-padded ISO date BECAUSE import_md enforces it
    #   at parse time, and calls that scoping "the sweep's only safety property" — so the
    #   sweep's own predicate returns [] for such a row, since 's' > '2'.
    #
    # The date is checked against `_ISO_DATE_RE`, which is ANCHORED and already lives in
    # the module this command writes into — the right regex was one import away.
    from techne.core.patterns import _ISO_DATE_RE
    from techne.import_md import _URL_RE

    surveyed = surveyed or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not _ISO_DATE_RE.match(surveyed):
        raise click.UsageError(f"--surveyed must be exactly YYYY-MM-DD; got {surveyed!r}")
    url_match = _URL_RE.search(source)
    if url_match is None:
        raise click.UsageError(f"--source must be an http(s) URL; got {source!r}")
    # `.group(0).rstrip("),.;")` — character-for-character what import_md.py:250 stores.
    source = url_match.group(0).rstrip("),.;")

    db = architect_db(config)
    inv_id = upsert_investigation(
        db,
        url=source,
        surveyed_at=surveyed,
        verdict=DEFAULT_INGEST_VERDICT if verdict is None else verdict,
        surveyed_by=surveyed_by,
        lens=lens,
        # Same rules `ingest` applies: a stated verdict overwrites, the fallback is
        # insert-only so a `reverdict` judgment survives, and storing a pattern proves the
        # URL yielded one, so a stale skip must not outlive it.
        overwrite_verdict=verdict is not None,
        clears_skip=True,
    )
    pid, stored = add_pattern(
        db, investigation_id=inv_id, name=name, source_url=source, body=body.strip()
    )
    # AND THE GRAPH, WHICH `ingest` BUILDS AND THIS DID NOT (Prudence, #758 Important B).
    # `ingest_patterns_md` writes FOUR things: the investigation, the pattern, typed
    # relations declared in the body, and then `backfill_graph` to materialise wikilink
    # edges and re-resolve anything earlier ingests left dangling. This wrote two — so
    # `pattern_relations` stayed EMPTY for every pattern fed through `add`, and the
    # operator this command exists for got a silently graphless store while
    # `architect related` and `architect graph` returned nothing.
    backfill_graph(db)
    if stored:
        click.echo(f"added {pid}  {name}")
    else:
        # Ids are content hashes, so this is the same pattern, not a name collision.
        click.echo(f"already present {pid}  {name}")


@architect.command("reverdict")
@click.argument("url")
@click.option(
    "--verdict",
    type=click.Choice(["skip", "skip-with-flag", "patterns-only", "full-eval", "unknown"]),
    required=True,
    help="The corrected judgment for this URL.",
)
@click.option(
    "--surveyed-by",
    default=None,
    help="Who surveyed it. Defaults to config `architect.surveyed_by`"
    " ('operator' unless you set it). Part of a row's identity —"
    " UNIQUE(url, surveyed_by, lens) — so a store with history wants"
    " the value it already uses.",
)
@click.option(
    "--lens",
    default=None,
    help="Angle the survey was done from. Defaults to config `architect.lens` ('infra').",
)
@click.pass_context
def architect_reverdict(ctx, url, verdict, surveyed_by, lens):
    """Correct one investigation's verdict (techne#428).

    Does not touch surveyed_at — correcting a judgment is not a re-survey.
    """
    config = ctx.obj["config"]
    surveyed_by = surveyed_by or config.architect.surveyed_by
    lens = lens or config.architect.lens
    from techne.core.architect import architect_db
    from techne.core.patterns import set_verdict

    updated = set_verdict(
        architect_db(config), url=url, verdict=verdict, surveyed_by=surveyed_by, lens=lens
    )
    if updated:
        click.echo(f"{url} -> {verdict}")
    else:
        click.echo(f"No investigation found for {url} (surveyed_by={surveyed_by}, lens={lens}).")
        raise SystemExit(1)


@architect.command("sweep-legacy-verdicts")
@click.option(
    "--before",
    required=True,
    help="Only touch rows with surveyed_at earlier than this date (YYYY-MM-DD). "
    "Scopes the sweep to the pre-fix corpus so a real full-eval is never clobbered.",
)
@click.option(
    "--spare-with-eval-doc",
    "eval_doc_dirs",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Spare any row whose URL slug has a `<slug>-evaluation*.md` in this "
    "directory: an eval doc means the full-eval was a real judgment. Repeatable.",
)
@click.option(
    "--skip-url",
    "skip_urls",
    multiple=True,
    help="Spare this exact URL. Repeatable.",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Actually write. Without this it only reports what it would change. "
    "Refused unless --spare-with-eval-doc or --skip-url is given.",
)
@click.pass_context
def architect_sweep_legacy_verdicts(ctx, before, eval_doc_dirs, skip_urls, apply_):
    """Retire pre-techne#428 auto-assigned 'full-eval' verdicts to 'unknown'.

    Every full-eval written before techne#428 was assigned by the ingester purely
    because the markdown carried patterns — except the ones an operator judged,
    which no --before date separates from the rest (issue #549). Name them with
    --spare-with-eval-doc and/or --skip-url. This rewrites your corpus — it is
    dry-run unless you pass --apply, and --apply needs a spare mechanism.
    """
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import sweep_legacy_verdicts

    # REFUSE A DATE-ONLY APPLY (issue #549). Measured: every --before value that
    # reaches the legacy rows also reaches real operator full-evals, so date alone
    # always destroys judgments. The dry run stays open so the operator can look.
    if apply_ and not (eval_doc_dirs or skip_urls):
        raise click.UsageError(
            "--apply needs --spare-with-eval-doc DIR and/or --skip-url URL: --before "
            "alone cannot tell a legacy full-eval from a real one (issue #549)."
        )
    try:
        res = sweep_legacy_verdicts(
            architect_db(config),
            before=before,
            dry_run=not apply_,
            spare_urls=skip_urls,
            eval_doc_dirs=eval_doc_dirs,
        )
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--before") from e

    summary = {k: v for k, v in res.items() if k not in ("urls", "spared")}
    summary["spared"] = len(res["spared"])
    click.echo(json.dumps(summary, indent=2))

    # PRINT THE URLS, LABELLED. A bare `matched: 202` looks equally correct whether
    # the scope is right or has swallowed every post-fix judgment, so a count-only
    # preview is not protection (Prudence, PR #432). And an undifferentiated list
    # hid the 10 real judgments among 205 (issue #549), so kept rows are listed too.
    if res["urls"]:
        click.echo("\nWould set to 'unknown':" if not apply_ else "\nSet to 'unknown':")
        for u in res["urls"]:
            click.echo(f"  legacy  {u}")
    if res["spared"]:
        click.echo("\nSpared (kept as full-eval):")
        for r in res["spared"]:
            click.echo(f"  spare   {r['url']}  ({r['reason']})")
    if not apply_ and res["urls"] and not (eval_doc_dirs or skip_urls):
        click.echo(
            "\nNo spare mechanism given: every row above is listed as legacy, "
            "including any real judgment. --apply will need --spare-with-eval-doc "
            "or --skip-url."
        )
    if not apply_ and res["matched"]:
        click.echo(
            f"\nDry run — {res['matched']} row(s) above would be rewritten. "
            "Check the list, then re-run with --apply."
        )


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


@architect.command("dedup")
@click.argument("url")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def architect_dedup(ctx, url, as_json):
    """Has this URL been investigated before? Exit 0 if seen, 1 if new.

    THE CHECK `search` CANNOT DO (techne#754). `architect search` runs FTS over
    pattern NAME and BODY only — `source_url` is never indexed — so searching a URL
    reports "No patterns matched" for a repo that is demonstrably in the store. This
    queries the columns directly, and it reads BOTH tables: a `skip-with-flag`
    investigation records a verdict and a reason with no pattern row, which is
    exactly the population a patterns-only check misses and exactly the one worth
    catching, since "so we don't re-investigate this" is why the verdict was kept.
    """
    import json as _json

    from techne.core.architect import architect_db
    from techne.core.patterns import dedup_url

    config = ctx.obj["config"]
    res = dedup_url(architect_db(config), url)

    if as_json:
        click.echo(_json.dumps(res, indent=2))
    elif not res["seen"]:
        click.echo(f"new — no prior contact with {url}")
    else:
        for p in res["patterns"]:
            click.echo(f"  {p['id']}  {p['name']}")
            click.echo(f"    {p['source_url']}")
        for i in res["investigations"]:
            line = f"  investigated {i['surveyed_at']} verdict={i['verdict']}"
            if i["skip_reason"]:
                line += f" reason={i['skip_reason']}"
            click.echo(line)
        # The date is when the KEPT verdict was reached, not when anyone last
        # looked: since techne#711 surveyed_at and verdict move together, and an
        # ingest without --verdict moves neither, so a re-surveyed URL keeps its
        # old date (patterns._TAKES_NEW_SURVEY; last-looked is techne#741).
        click.echo("\nSeen before. Ask before re-investigating.")

    raise SystemExit(0 if res["seen"] else 1)


@architect.command("snapshot")
@click.pass_context
def architect_snapshot(ctx):
    """Content hash of the pattern corpus — the read-only-subagent backstop.

    Take it before spawning a research subagent and compare after. A difference
    means something wrote to the store that should not have, and the correct
    response is to treat the whole result as compromised: you cannot distinguish
    subagent-authored content from injection-authored content after the fact.

    WHAT THE DIGEST COVERS, stated because the useful reading is the converse — "no
    difference means nothing wrote" is the branch you proceed on. It covers EVERY column
    of `patterns`, `investigations` AND `pattern_outcomes`, including `status`, so a soft
    delete (status -> superseded), a new skip-with-flag row and a recorded outcome all
    move it. It does NOT cover the derived graph — `pattern_relations` and
    `pattern_applies` — because `backfill_graph` rebuilds both from content this does
    cover, so a change there alone means the graph was refreshed, not edited.

    (An earlier version of this text listed a third exclusion, `topics`. No such table
    exists; the real sibling missing from the list was `pattern_outcomes`, which is
    derived from nothing and is now covered.)
    """
    from techne.core.architect import architect_db
    from techne.core.patterns import store_snapshot

    click.echo(store_snapshot(architect_db(ctx.obj["config"])))


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


@architect.command("targets")
@click.pass_context
def architect_targets(ctx):
    """Show configured pattern targets against what the store actually holds.

    The configured list is validation: `_apply_targets` DROPS a target that is
    not on it. So a store with history under a name the config omits will
    silently stop tagging new patterns with it (techne#1008).
    """
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import known_targets, target_counts

    configured = known_targets()
    # ACTIVE PATTERNS ONLY decide "the store holds it" (issue #1016), the same
    # test `pattern_search` applies, so a target whose patterns were all retired
    # is not reported present here while MCP calls it unknown. Retired-only
    # targets are still listed, separately, so the history is not hidden.
    counts = target_counts(architect_db(config))
    rows = [r for r in counts if r["active"]]
    retired_only = [r for r in counts if not r["active"]]

    click.echo("configured (architect.pattern_targets):")
    for t in sorted(configured):
        click.echo(f"  {t}")

    if not rows and not retired_only:
        click.echo("\nstore holds no tagged patterns yet.")
        return

    unlisted = []
    if rows:
        click.echo("\nin the store (active patterns):")
    for r in rows:
        mark = " " if r["target"] in configured else "  <- NOT CONFIGURED"
        if r["target"] not in configured:
            unlisted.append(r["target"])
        click.echo(f"  {r['target']:<16} {r['active']:>5}{mark}")

    if retired_only:
        click.echo(
            "\nretired only (no active pattern; `pattern_search` serves nothing for these):"
        )
        for r in retired_only:
            click.echo(f"  {r['target']:<16} {r['retired']:>5} retired")

    if unlisted:
        click.echo(
            "\nAdd these to architect.pattern_targets or new patterns will stop "
            "being tagged with them:\n  pattern_targets: ["
            + ", ".join(f'"{t}"' for t in sorted(configured | set(unlisted)))
            + "]"
        )


@architect.command("applies")
@click.argument("target")
@click.option("--limit", default=100)
@click.pass_context
def architect_applies(ctx, target, limit):
    """List patterns tagged as applying to TARGET (see techne.core.patterns.known_targets())."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import patterns_for_target

    hits = patterns_for_target(architect_db(config), target, limit=limit)
    if not hits:
        click.echo(f"No patterns apply to {target!r}.")
        return
    for h in hits:
        click.echo(f"  {h['id']}  {h['name']}")


@architect.command("realized")
@click.argument("pattern_id")
@click.option(
    "--ref",
    required=True,
    help="What it became: 'tenishai/techne#385', a commit sha, a doc path.",
)
@click.option(
    "--kind",
    default="issue",
    type=click.Choice(["issue", "pr", "commit", "decision"]),
    help="What kind of artifact the ref points at.",
)
@click.option(
    "--state",
    default="filed",
    type=click.Choice(["filed", "shipped", "rejected"]),
    help="filed = work exists; shipped = it landed; rejected = considered and declined.",
)
@click.option("--note", default=None, help="Free-text context (e.g. what closed it, and when).")
@click.pass_context
def architect_realized(ctx, pattern_id, ref, kind, state, note):
    """Record what a pattern BECAME — the return path of provenance (techne#426).

    Upserts on (pattern, ref), so re-running promotes state: mark 'filed' when the
    issue is opened, 'shipped' when it closes as completed."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import record_outcome

    try:
        changed = record_outcome(
            architect_db(config),
            pattern_id=pattern_id,
            ref=ref,
            kind=kind,
            state=state,
            note=note,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{'recorded' if changed else 'unchanged'}  {pattern_id}  →  {ref} [{state}]")


@architect.command("outcomes")
@click.argument("pattern_id", required=False)
@click.pass_context
def architect_outcomes(ctx, pattern_id):
    """Show what patterns became. With PATTERN_ID, that pattern's outcomes; bare,
    the corpus-level conversion rate — did this investigation programme pay?"""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import outcome_stats, outcomes_for_pattern

    db = architect_db(config)
    if pattern_id:
        rows = outcomes_for_pattern(db, pattern_id)
        if not rows:
            click.echo(f"No recorded outcomes for {pattern_id}.")
            return
        for r in rows:
            note = f"  — {r['note']}" if r["note"] else ""
            click.echo(f"  [{r['state']:<8}] {r['kind']:<8} {r['ref']}  ({r['recorded_at']}){note}")
        return
    click.echo(json.dumps(outcome_stats(db), indent=2))


@architect.command("unrealized")
@click.option("--topic", default=None, help="Restrict to one topic slug.")
@click.option("--limit", default=40, show_default=True)
@click.pass_context
def architect_unrealized(ctx, topic, limit):
    """Active patterns with no recorded outcome — what we lifted and never acted on."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import unrealized_patterns

    hits = unrealized_patterns(architect_db(config), topic=topic, limit=limit)
    if not hits:
        click.echo("Every active pattern has a recorded outcome.")
        return
    for h in hits:
        click.echo(f"  {h['id']}  [{h['topic'] or '(untopiced)'}]  {h['name']}")


@architect.command("backfill")
@click.pass_context
def architect_backfill(ctx):
    """Re-derive the relation/applies graph from pattern bodies (idempotent)."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import backfill_graph

    counts = backfill_graph(architect_db(config))
    click.echo(json.dumps(counts, indent=2))


@architect.command("topic")
@click.argument("name", required=False)
@click.option(
    "--as-of",
    "as_of",
    default=None,
    help="Point-in-time view (techne#306): show what was current as of this time "
    "(YYYY-MM-DD[ HH:MM:SS]) via the validity window, instead of the live view.",
)
@click.pass_context
def architect_topic(ctx, name, as_of):
    """Topic index (techne#597). No NAME → list topics + counts (crowded first).
    With NAME → the topic's active patterns, each superseded predecessor folded
    under it with its salvage note. Use '(untopiced)' for the ungrouped bucket.
    --as-of <ts> switches to the point-in-time view (what was current then)."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import current_as_of, topic_list, topic_show

    db = architect_db(config)
    if as_of is not None:
        rows = current_as_of(db, as_of, topic=name)
        if not rows:
            scope = f" in topic {name!r}" if name else ""
            click.echo(f"No patterns current as of {as_of}{scope}.")
            return
        click.echo(f"# current as of {as_of}:")
        for r in rows:
            click.echo(f"  {r['id']}  {r['name']}  [{r['topic'] or '(untopiced)'}]")
        return
    if name is None:
        rows = topic_list(db)
        if not rows:
            click.echo("No patterns yet.")
            return
        for r in rows:
            click.echo(f"  {r['count']:>4}  {r['topic']}")
        return
    entries = topic_show(db, name)
    if not entries:
        click.echo(f"No active patterns in topic {name!r}.")
        return
    for e in entries:
        click.echo(f"  {e['id']}  {e['name']}")
        for s in e["superseded"]:
            note = f" — {s['absorbed']}" if s.get("absorbed") else ""
            click.echo(f"      ⤷ superseded: {s['id']}  {s['name']}{note}")


@architect.command("tag")
@click.argument("pattern_id")
@click.option(
    "--topic", default=None, help="Set the pattern's topic (problem-solved grouping key)."
)
# EXHAUSTIVE BY CONSTRUCTION, which is what makes `architect`'s counts honest.
# The help text always promised `active|superseded`; nothing enforced it, so
# `--status archived` was accepted silently and the summary then reported
# `Patterns: 0 active … (+1 superseded)` against 2 rows — itemised-looking and
# wrong, which is worse than the bare count it replaced (#878, Prudence on #879).
# This is the only caller of tag_pattern, so the Choice closes the in-product path.
@click.option(
    "--status",
    type=click.Choice(["active", "superseded"]),
    default=None,
    help="Set status directly. `superseded` also needs --superseded-by (techne#880).",
)
@click.option(
    "--superseded-by",
    default=None,
    help="Mark superseded by this pattern id — atomic: stamps status/superseded_by/absorbed "
    "and writes a resolved 'supersedes' edge.",
)
@click.option(
    "--absorbed",
    default=None,
    help="Salvage note: what this pattern contributed before being folded into its successor.",
)
@click.pass_context
def architect_tag(ctx, pattern_id, topic, status, superseded_by, absorbed):
    """Curate a pattern (techne#597): set its --topic, and/or supersede it via
    --superseded-by (the atomic supersession path). Any subset of options applies."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.patterns import tag_pattern

    try:
        changed = tag_pattern(
            architect_db(config),
            pattern_id,
            topic=topic,
            status=status,
            superseded_by=superseded_by,
            absorbed=absorbed,
        )
    except ValueError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(1)
    click.echo("updated" if changed else "no change")


@architect.command("graph")
@click.option(
    "--topic",
    default=None,
    help="Render only this topic's subgraph ('(untopiced)' for the ungrouped bucket). "
    "Default: the whole store.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["mermaid", "dot"]),
    default="mermaid",
    help="Output format (default: mermaid).",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the graph here (default: stdout).",
)
@click.pass_context
def architect_graph(ctx, topic, fmt, output):
    """Render the topic-index + relation graph as Mermaid or DOT (techne#305).

    Nodes cluster by topic; superseded patterns are dashed/de-emphasized; edges
    are labeled by relation kind (supersedes dashed). Scope a focused view with
    --topic — a whole-store render of the full corpus is dense."""
    config = ctx.obj["config"]
    from techne.core.architect import architect_db
    from techne.core.pattern_graph import render_dot, render_mermaid
    from techne.core.patterns import graph_data

    data = graph_data(architect_db(config), topic=topic)
    if not data["nodes"]:
        where = f" in topic {topic!r}" if topic else ""
        click.echo(f"No patterns{where}.", err=True)
        return
    text = (render_dot if fmt == "dot" else render_mermaid)(data)
    if output:
        output.write_text(text, encoding="utf-8")
        click.echo(
            f"Wrote {fmt} graph: {len(data['nodes'])} nodes, {len(data['edges'])} edges → {output}",
            err=True,
        )
    else:
        click.echo(text)
    if topic is None and len(data["nodes"]) > 60:
        click.echo(
            f"[{len(data['nodes'])} nodes — consider --topic <name> for a focused view; "
            "`architect topic` lists them]",
            err=True,
        )


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


@main.group()
@click.pass_context
def vectors(ctx):
    """Manage the personal-memory vector search channel (techne#284).

    Vector search (search_vec/search()) degrades silently to FTS-only when
    the embedding backend or sqlite-vec is unavailable, OR when the vec0
    tables are stale after a backend swap (techne#601/#602 — e.g. upgrading
    to Python 3.14 flips fastembed/MiniLM-384 to model2vec/potion-256). Use
    `techne vectors status` to check, `techne vectors backfill` to fix.
    """
    pass


@vectors.command("backfill")
@click.option(
    "--tags",
    "do_tags",
    is_flag=True,
    help="Also backfill observation tags/kind via embedding inference — fills rows "
    "stored before the embedding backend was available (tags=NULL).",
)
@click.option(
    "--all-tags",
    "all_tags",
    is_flag=True,
    help="With --tags, re-infer EVERY observation, not just untagged rows "
    "(e.g. after a prototype change).",
)
@click.option(
    "--no-download",
    "no_download",
    is_flag=True,
    help="Never fetch an uncached embedding model from the Hugging Face Hub — "
    "degrade to 'swept, embedded nothing' instead of blocking on the network. "
    "Set by the SessionStart hook; omit it for the documented cold-cache recovery.",
)
@click.pass_context
def vectors_backfill(ctx, do_tags, all_tags, no_download):
    """Embed un-embedded rows; re-embed everything if the backend changed.

    Thin CLI wrapper around search.backfill_vectors() (techne#283 stack) —
    the actual embedding-backend-identity migration (drop+recreate+re-embed
    threads_vec/observations_vec/knowledge_vec on a backend/model swap) is
    NOT reimplemented here, only invoked. See search.py's
    _ensure_vec_backend_identity for the migration itself.

    With --tags, also re-runs tag/kind inference over stored observations
    (inference.backfill_tags) — the same embedding backend, so it needs no
    extra deps beyond [vectors]. Rows stored while the backend was missing
    carry tags=NULL; this fills them.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core import embeddings as embeddings_mod

    # Graceful, non-error exit when there's nothing to embed with — matches
    # search_vec/backfill_vectors' own degrade-to-FTS-only behavior rather
    # than surfacing an ImportError to a user who simply didn't install the
    # optional [vectors] extra.
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        click.echo(
            "vector search unavailable: no embedding backend installed — install techne[vectors]"
        )
        ctx.exit(0)
    # NO backend_available() GATE HERE (#408). This command is the string
    # `vectors:coverage` hands the operator as its remedy, and an early exit made
    # that remedy do nothing on exactly the install the orphan sweep exists for:
    # sqlite-vec present, no model. Threads keep rotating rowids and stranding
    # embeddings whether or not a backend exists, so the doctor kept reporting
    # dangling rows with no way to clear them. Hoisting the sweep inside
    # backfill_vectors was not enough — a duplicate gate one frame up defeated it.
    #
    # backfill_vectors owns the decision now: it sweeps first (pure SQL, needs the
    # extension not a model) and then returns {} if it cannot embed. The CLI keeps
    # only the sqlite_vec import guard above, which is the one thing it genuinely
    # has to check to print a useful message.
    from techne.core.search import backfill_vectors

    counts = backfill_vectors(config.db_path, allow_download=not no_download)

    if not embeddings_mod.backend_available():
        click.echo(
            "vector search unavailable: no embedding backend installed — install techne[vectors]"
        )
        # REPORT WHAT ACTUALLY HAPPENED. backfill_vectors returns {} for three
        # different outcomes and this caller cannot tell them apart, so the old
        # unconditional line asserted the good one: with numpy absent (it is only
        # in the [vectors] extra) or an interpreter that cannot load extensions,
        # the sweep never ran and the message claimed it had. Worse than #496's
        # remedy-that-does-nothing — an operator told it succeeded stops looking.
        if "orphans_swept" not in counts:
            # DO NOT NAME sqlite-vec HERE. On the numpy path it imported fine —
            # the CLI's own guard just passed — so blaming it sends the operator
            # to re-check the thing they have already ruled out. numpy is only in
            # the [vectors] extra, so this is the reachable case.
            click.echo(
                "index hygiene did NOT run: the vector toolchain is incomplete "
                "(needs numpy and a loadable sqlite-vec) — install techne[vectors]."
            )
        elif counts.get("sweep_errors"):
            click.echo(
                f"index hygiene INCOMPLETE: {counts['sweep_errors']} index(es) unreadable, "
                f"{counts['orphans_swept']} orphaned embedding(s) swept."
            )
        else:
            click.echo(f"index hygiene: {counts['orphans_swept']} orphaned embedding(s) swept.")
        ctx.exit(0)

    click.echo(json.dumps(counts, indent=2))

    if do_tags:
        from techne.core.inference import backfill_tags

        tag_counts = backfill_tags(config.db_path, config, only_missing=not all_tags)
        click.echo(json.dumps({"tags": tag_counts}, indent=2))


@vectors.command("status")
@click.pass_context
def vectors_status(ctx):
    """Report the vector channel's health without mutating anything.

    Shows the active embedding backend, the stored backend-identity stamp
    (if any), per-table vec0 row counts with how many rows are not embedded,
    whether a re-embed is needed (a backend/model swap happened since the vec
    tables were last built), and whether a backfill is needed (rows missing
    vectors, e.g. knowledge just promoted — techne#948).
    Never calls _ensure_vec_backend_identity — that function DROPs the vec
    tables on a mismatch, which is exactly the mutation a read-only status
    check must not risk triggering as a side effect of merely looking.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core.vector_status import vector_channel_status

    status = vector_channel_status(config.db_path)

    if status["active_backend"] is None:
        click.echo("active embedding backend: none available")
    else:
        click.echo(
            f"active embedding backend: {status['active_backend']['name']}"
            f" (dim={status['active_backend']['dim']})"
        )

    if status["stamp"] is None:
        click.echo("stored identity stamp: none")
    else:
        click.echo(
            f"stored identity stamp: {status['stamp']['name']} (dim={status['stamp']['dim']})"
        )

    click.echo("vec table rows:")
    for table_name in ("threads_vec", "observations_vec", "knowledge_vec"):
        info = status["tables"][table_name]
        gap = status["unembedded"][table_name]
        suffix = f", {gap} not embedded" if gap else ""
        if not info["exists"]:
            click.echo(f"  {table_name}: absent{suffix}")
        else:
            click.echo(f"  {table_name}: {info['rows']} row(s) (dim={info['dim']}){suffix}")

    # Two questions, two lines (techne#948). `re-embed needed` is only "did the
    # embedding backend change?"; an operator read its False as "everything is
    # embedded" while 25 promoted knowledge rows had no vectors.
    click.echo(f"re-embed needed: {status['reembed_needed']}")
    click.echo(f"backfill needed: {status['backfill_needed']}")
    if status["reembed_needed"]:
        click.echo(
            "vector channel is stale after a backend change — "
            "run `techne vectors backfill` to restore it."
        )
    elif status["backfill_needed"]:
        click.echo("rows are missing vectors — run `techne vectors backfill` to embed them.")


def _active_codex_db(config, repo, *, require_indexed: bool = True):
    """Resolve the per-repo codex db for a codex query command (techne#209).

    ``repo`` given → that repo root; else walk up from the cwd for ``.git``.
    Raises a clean ClickException when there's no repo to target, or (when
    ``require_indexed``) when that repo has no codex index yet. Returns
    ``(repo_root, db_path)``.
    """
    from techne.core.codex import ensure_legacy_migrated, repo_db_path
    from techne.core.repo import find_repo_root

    ensure_legacy_migrated(config)
    if repo is not None:
        root = Path(repo).expanduser().resolve()
    else:
        root = find_repo_root(Path.cwd())
        if root is None:
            raise click.ClickException(
                "not inside a git repository — pass --repo PATH to select a codex repo."
            )
    db_path = repo_db_path(config, root)
    if require_indexed and not db_path.exists():
        raise click.ClickException(f"no codex index for {root}\n  run: techne codex index {root}")
    return root, db_path


@main.group(invoke_without_command=True)
@click.pass_context
def codex(ctx):
    """Code-graph store — per-repo symbols + edges, graph queries (techne#89, #209).

    EXPERIMENTAL: indexing is manual — the graph can go stale as code changes;
    re-run `codex index` to refresh. Each repo gets its own graph (db-per-repo,
    techne#209), so queries are scoped to the repo you're in.
    """
    if ctx.invoked_subcommand is not None:
        return
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return
    from techne.core.codegraph import stats
    from techne.core.codex import list_codex_repos

    repos = list_codex_repos(config)
    if not repos:
        click.echo("no repos indexed. Run: techne codex index <repo>")
        return
    click.echo(f"Indexed repos: {len(repos)}")
    for root, db_path in repos:
        s = stats(db_path)
        click.echo(f"  {root}  —  {s['symbols']} symbols  {s['edges']} edges  {s['files']} files")


def _import_resolve_pass(db_path: Path) -> dict:
    """Run the dependency-free import-binding resolver and report it (techne#679).

    UNCONDITIONAL, and deliberately not behind a flag the way `--lsp` is. It
    needs no language server, it never overwrites an `lsp` row, and it costs
    ~0.3s on a 24k-edge index. A resolver that has to be REMEMBERED is a
    resolver that does not run: `--lsp` existed for months and the live corpus
    still measured 33,081 call edges with 1 resolved target, that one in a demo
    shard. Making this one opt-in would reproduce that outcome exactly.

    ORDERING IS LOAD-BEARING. This runs BEFORE the optional `--lsp` step, not
    after. `import_resolve` only touches rows whose `resolution_status` is in
    its `_OVERWRITABLE` set, so it cannot clobber LSP; `resolve_edges_lsp`
    updates by (from_symbol, to_name) with no `to_symbol IS NULL` guard, so it
    still upgrades a row this pass resolved. Reversing the two would leave the
    cheap pass unable to fill anything the expensive one missed.

    SAME-FILE CALLS RIDE THE SAME PASS (techne#940). `resolve_edges_by_scope` runs
    second and only on rows the import pass left `unresolved` with no import
    binding, so the two cannot disagree about a row. Its count is reported as
    `scope_resolved`, apart from `resolved`, so the import figure keeps its meaning.
    """
    from techne.codex.import_resolve import resolve_edges_by_import, resolve_edges_by_scope
    from techne.core.stores import plain_connect

    conn = plain_connect(db_path)
    try:
        report = resolve_edges_by_import(conn)
        report["scope_resolved"] = resolve_edges_by_scope(conn)["resolved"]
        return report
    finally:
        conn.close()


@codex.command("index")
@click.argument("repo", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--language",
    "languages",
    default="python,typescript",
    show_default=True,
    help="Comma-separated list of languages to index (python,typescript).",
)
@click.option(
    "--lsp/--no-lsp",
    default=False,
    help="After indexing, resolve Python call edges via a language server (pyright) for "
    "ground-truth `callers` — semantic, not name-matched (techne#321). Slower (per-symbol), "
    "so opt-in for now.",
)
@click.option(
    "--rebuild",
    is_flag=True,
    default=False,
    help="Re-parse every file, ignoring the unchanged-file skip. Needed when what "
    "is EXTRACTED from a file changes while the file does not — a new parse-derived "
    "column, or a grammar/query fix (techne#864). A schema migration records this "
    "automatically; the flag is for the cases nothing recorded.",
)
@click.pass_context
def codex_index(ctx, repo, languages, lsp, rebuild):
    """Walk REPO and index Python + TypeScript symbols into its per-repo db.

    Uses tree-sitter for parsing; requires `pip install techne[codex]`.
    Incremental: files whose SHA-256 hash is unchanged are skipped — pass
    --rebuild to re-parse regardless.
    With --lsp, a language server then resolves call edges to exact symbols.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init", err=True)
        ctx.exit(1)
    from techne.codex.source import RepoSource
    from techne.core.codegraph import stats
    from techne.core.codex import codex_db_for_repo

    lang_list = [lang.strip() for lang in languages.split(",") if lang.strip()]
    db_path = codex_db_for_repo(config, repo)
    src = RepoSource(db_path, repo, languages=lang_list)
    count = src.initial_walk(force=rebuild)

    s = stats(db_path)
    if src.last_forced_by:
        # SAY WHY THE "UNCHANGED" REPO JUST RE-PARSED IN FULL (techne#864).
        # Without this the operator sees a long run on a repo they did not touch
        # and has no way to tell a migration reparse from a bug.
        click.echo(
            f"Reparse: forced by the schema v{src.last_forced_by} migration — its new "
            f"column is parse-derived, so unchanged files still had to be re-read"
        )
    elif rebuild:
        click.echo("Reparse: forced by --rebuild (unchanged-file skip bypassed)")
    click.echo(f"Indexed: {count} symbols from {repo}")
    if src.last_swept:
        click.echo(
            f"Swept:   {src.last_swept} symbols for paths the skip list excludes "
            f"(#613 — stored before the current skip list)"
        )
    click.echo(f"Totals:  {s['symbols']} symbols  {s['edges']} edges  {s['files']} files")

    rep = _import_resolve_pass(db_path)
    click.echo(
        f"Resolved: {rep['resolved']} call edges by import binding, "
        f"{rep['scope_resolved']} by same-file scope, "
        f"{rep['external']} ruled external (no toolchain, techne#679)"
    )
    if src.last_failed:
        # Not a warning tucked into a log file: a run that could not index N files
        # exits NON-ZERO. Those files kept their previous rows, so the graph is
        # stale exactly where this output otherwise reads as success (#610).
        raise click.ClickException(
            f"{src.last_failed} file(s) failed to index and kept their PREVIOUS rows — "
            "the graph is stale for those paths. See the codex_index_failed tracebacks "
            "in the log (tenishai/techne#610)."
        )

    if lsp:
        from techne.codex.lsp_resolve import resolve_edges_lsp

        rep = resolve_edges_lsp(db_path, repo, settle=(8, 1.5))
        if rep.get("skipped"):
            click.echo(f"LSP resolution skipped: {rep['skipped']}", err=True)
        else:
            click.echo(f"LSP-resolved: {rep['resolved']} call edges (of {rep['symbols']} symbols)")


@codex.command("index-all")
@click.option(
    "--language",
    "languages",
    default="python,typescript",
    show_default=True,
    help="Comma-separated list of languages to index (python,typescript).",
)
@click.option(
    "--rebuild",
    is_flag=True,
    default=False,
    help="Re-parse every file in every repo, ignoring the unchanged-file skip (techne#864).",
)
@click.pass_context
def codex_index_all(ctx, languages, rebuild):
    """Re-index every repo that already has a codex db (techne#209).

    Iterates the known per-repo dbs and re-walks each repo. Repos whose root no
    longer exists on disk are reported and skipped.

    A repo carrying a migration reparse debt is re-parsed in full whether or not
    --rebuild is passed, and says so (techne#864).
    """
    config = ctx.obj["config"]
    from techne.codex.source import RepoSource
    from techne.core.codex import codex_db_for_repo, list_codex_repos

    repos = list_codex_repos(config)
    if not repos:
        click.echo("no repos indexed. Run: techne codex index <repo>")
        return
    lang_list = [lang.strip() for lang in languages.split(",") if lang.strip()]
    total_failed = 0
    for root_str, _db in repos:
        root = Path(root_str)
        if not root.exists():
            click.echo(f"skip (missing): {root}")
            continue
        db_path = codex_db_for_repo(config, root)
        src = RepoSource(db_path, root, languages=lang_list)
        count = src.initial_walk(force=rebuild)
        forced = ""
        if src.last_forced_by:
            forced = f", REPARSED for schema v{src.last_forced_by}"
        elif rebuild:
            forced = ", reparsed (--rebuild)"
        swept = f", swept {src.last_swept} excluded" if src.last_swept else ""
        # Reported per-repo rather than raised: this loop is a sweep over many repos
        # and one bad repo must not hide the rest. The summary below is what makes
        # the run non-zero.
        failed = f", {src.last_failed} FAILED (stale, #610)" if src.last_failed else ""
        total_failed += src.last_failed
        rep = _import_resolve_pass(db_path)
        click.echo(
            f"reindexed {root}: {count} symbols{forced}{swept}{failed}"
            f"; resolved {rep['resolved']} calls, {rep['scope_resolved']} same-file, "
            f"{rep['external']} external"
        )

    if total_failed:
        raise click.ClickException(
            f"{total_failed} file(s) across all repos failed to index and kept their "
            "PREVIOUS rows — the graph is stale for those paths (tenishai/techne#610)."
        )


@codex.command("stats")
@click.option(
    "--repo", type=click.Path(path_type=Path), default=None, help="Repo to report (default: cwd)."
)
@click.option("--all", "all_repos", is_flag=True, help="Report every indexed repo.")
@click.pass_context
def codex_stats(ctx, repo, all_repos):
    """Show symbol/edge/file counts — for the current repo, or all with --all."""
    config = ctx.obj["config"]
    from techne.core.codegraph import stats
    from techne.core.codex import list_codex_repos

    if all_repos:
        out = {root: stats(db) for root, db in list_codex_repos(config)}
        click.echo(json.dumps(out, indent=2))
        return
    _root, db_path = _active_codex_db(config, repo)
    click.echo(json.dumps(stats(db_path), indent=2))


@codex.command("search")
@click.argument("query")
@click.option(
    "--repo", type=click.Path(path_type=Path), default=None, help="Repo to search (default: cwd)."
)
@click.option("--limit", default=20)
@click.pass_context
def codex_search(ctx, query, repo, limit):
    """FTS over symbol names + signatures, scoped to the active repo."""
    config = ctx.obj["config"]
    from techne.core.codegraph import annotate_freshness, search_symbols

    _root, db_path = _active_codex_db(config, repo)
    results = annotate_freshness(db_path, search_symbols(db_path, query, limit=limit))
    for s in results:
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        click.echo(f"  {s['id']}  [{s['kind']}] {s['name']}  {s.get('signature') or ''}{marker}")
        click.echo(f"    {s['file']}")


def _resolve_codex_symbol_or_exit(db_path: Path, arg: str) -> str:
    """Resolve a symbol argument (full id or bare name) to a concrete id,
    printing a helpful error and exiting non-zero on failure — so callers /
    blast-radius never silently return an empty result for an unresolvable
    argument (techne#300)."""
    from techne.core.codegraph import SymbolResolutionError, resolve_symbol_id

    try:
        return resolve_symbol_id(db_path, arg)
    except SymbolResolutionError as exc:
        click.echo(str(exc), err=True)
        for c in exc.candidates[:10]:
            click.echo(f"  {c['id']}  ({c['file']})", err=True)
        if len(exc.candidates) > 10:
            click.echo(f"  ... and {len(exc.candidates) - 10} more", err=True)
        raise SystemExit(1) from exc


@codex.command("callers")
@click.argument("symbol_id")
@click.option(
    "--repo", type=click.Path(path_type=Path), default=None, help="Repo to query (default: cwd)."
)
@click.option(
    "--include-fixtures",
    is_flag=True,
    default=False,
    help="Include tests/fixtures/** nodes (excluded by default; techne#325).",
)
@click.pass_context
def codex_callers(ctx, symbol_id, repo, include_fixtures):
    """Who calls this symbol (direct), scoped to the active repo.

    SYMBOL_ID is a full id (path::name::kind::line, from `codex search`) or a
    bare name that resolves to exactly one symbol.
    """
    config = ctx.obj["config"]
    from techne.core.codegraph import annotate_freshness, callers

    _root, db_path = _active_codex_db(config, repo)
    symbol_id = _resolve_codex_symbol_or_exit(db_path, symbol_id)
    rows = annotate_freshness(
        db_path, callers(db_path, symbol_id, exclude_fixtures=not include_fixtures)
    )
    for s in rows:
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        # techne#606: '?' marks a caller matched only on the bare call name, which
        # may not call this symbol at all. Shown, but not asserted.
        guess = " ?" if s.get("resolution") == "name-inferred" else ""
        click.echo(f"  {s['id']}  {s['name']}  ({s['file']}){marker}{guess}")
    inferred = sum(1 for s in rows if s.get("resolution") == "name-inferred")
    if inferred:
        click.echo(
            f"  ({inferred} of {len(rows)} marked ? — matched by call name only, "
            "not a resolved symbol; may not be real callers)"
        )


@codex.command("blast-radius")
@click.argument("symbol_id")
@click.option(
    "--repo", type=click.Path(path_type=Path), default=None, help="Repo to query (default: cwd)."
)
@click.option(
    "--direction",
    type=click.Choice(["in", "out"]),
    default="in",
    show_default=True,
    help="in=dependents (what breaks if you change this); out=dependencies (what this reaches).",
)
@click.option("--depth", default=5, show_default=True)
@click.option(
    "--include-fixtures",
    is_flag=True,
    default=False,
    help="Include tests/fixtures/** nodes (excluded by default; techne#325).",
)
@click.option(
    "--only-proven",
    is_flag=True,
    default=False,
    help="Only nodes reached through resolved edges, never a bare-name match (techne#606).",
)
@click.pass_context
def codex_blast(ctx, symbol_id, repo, direction, depth, include_fixtures, only_proven):
    """Transitive reachability via recursive CTE — in=dependents (blast radius), out=dependencies.

    The default `in` answers "what breaks if I change this symbol". SYMBOL_ID is
    a full id (path::name::kind::line, from `codex search`) or a bare name that
    resolves to exactly one symbol.
    """
    config = ctx.obj["config"]
    from techne.core.codegraph import annotate_freshness, reachable

    _root, db_path = _active_codex_db(config, repo)
    symbol_id = _resolve_codex_symbol_or_exit(db_path, symbol_id)
    raw = reachable(
        db_path,
        symbol_id,
        direction=direction,
        max_depth=depth,
        exclude_fixtures=not include_fixtures,
        include_inferred=not only_proven,
    )
    # techne#606: walk again over resolved edges only, so each node can say whether
    # it is a proven dependent or one reached by a bare-name guess.
    proven_ids = {
        r["id"]
        for r in reachable(
            db_path,
            symbol_id,
            direction=direction,
            max_depth=depth,
            exclude_fixtures=not include_fixtures,
            include_inferred=False,
        )
    }
    rows = annotate_freshness(db_path, raw)
    # AN EMPTY PROVEN RADIUS IS NOT "NOTHING DEPENDS ON THIS" (techne#940). Under
    # --only-proven the name-matched dependents were filtered out, and a bare
    # "(nothing reachable)" read as permission to change the symbol while the
    # permissive walk still found callers. Count what was hidden and say so.
    hidden = 0
    if only_proven:
        hidden = sum(
            1
            for r in reachable(
                db_path,
                symbol_id,
                direction=direction,
                max_depth=depth,
                exclude_fixtures=not include_fixtures,
            )
            if r["id"] not in proven_ids
        )
    if not rows:
        if hidden:
            click.echo(
                f"(no resolved dependents; {hidden} reached only by a bare-name match "
                "hidden — re-run without --only-proven to see them)"
            )
        else:
            click.echo("(nothing reachable)")
        return
    for s in rows:
        freshness = s.get("freshness", "unknown")
        marker = f" [{freshness}]" if freshness not in ("fresh", "unknown") else ""
        guess = "" if s["id"] in proven_ids else " ?"
        click.echo(f"  d{s['depth']}  {s['name']}  [{s['kind']}]  ({s['file']}){marker}{guess}")
    inferred = sum(1 for s in rows if s["id"] not in proven_ids)
    if hidden:
        click.echo(f"  ({hidden} more reached only by a bare-name match hidden by --only-proven)")
    if inferred:
        click.echo(
            f"  ({inferred} of {len(rows)} marked ? — reached only by matching a bare call "
            "name; re-run with --only-proven for the resolved-only radius)"
        )


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
    """Watch REPO for file changes and re-index its per-repo db (Ctrl-C to stop).

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
    from techne.core.codex import codex_db_for_repo

    lang_list = [lang.strip() for lang in languages.split(",") if lang.strip()]
    db_path = codex_db_for_repo(config, repo)
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
        config.db_path,
        session_id,
        since_iso=since,
        bash_captured=config.hooks.capture_bash,
    )

    click.echo(f"Session: {session_id}")
    if since:
        click.echo(f"Since:   {since}")
    click.echo(f"Observations: {ev.observation_count}")
    click.echo(f"Any tool call: {ev.any_tool_call}")
    click.echo(f"Active (deed) tool call: {ev.active_tool_call}")
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
        # techne#684: a structural fault with no named path is how a check gets
        # trained into background noise. Print the remedy; never run it.
        if not c.ok and c.remedy:
            click.echo(f"         fix: {c.remedy}")
    if report.ok:
        click.echo("fsck: all cross-DB checks passed.")
    else:
        click.echo("fsck: inconsistencies found.", err=True)
        raise SystemExit(1)


@main.group()
def cards():
    """Document cards — the techne.db -> librarian.db pointer layer."""


@cards.command("repair")
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    default=False,
    help="Perform the retype. Without this, prints what would change and exits.",
)
@click.pass_context
def cards_repair(ctx, do_apply):
    """Retype document_cards that never had a doc pointer (techne#684).

    A document_card IS a pointer at a librarian document. A row with no pointer
    was never a card — #633's producer bug chose the type when it had nothing to
    point at — so the content is real knowledge wearing the wrong type, and the
    repair is to retype it to `fact`.

    A card whose pointer EXISTS but does not resolve is a different fault
    (librarian.db out of sync) and is REPORTED, never touched: restoring the
    document and superseding the card are both valid and only a human knows which.
    """
    from techne.core.cards import repair_pointerless_cards

    config = ctx.obj["config"]
    result = repair_pointerless_cards(config, apply=do_apply)

    if not result["retyped"] and not result["needs_human"]:
        click.echo("No dangling cards. Nothing to repair.")
        return

    if result["retyped"]:
        verb = "Retyped" if result["applied"] else "Would retype"
        click.echo(f"{verb} {len(result['retyped'])} card(s) with no doc pointer -> fact:")
        for cid in result["retyped"]:
            click.echo(f"  knowledge#{cid}")
        if not result["applied"]:
            click.echo("\nDry run. Re-run with --apply to perform it.")

    if result["needs_human"]:
        click.echo(
            f"\n{len(result['needs_human'])} card(s) need a human — pointer set but unresolvable:"
        )
        for d in result["needs_human"]:
            click.echo(f"  knowledge#{d['card_id']}  doc:{d.get('doc_id')}  ({d.get('reason')})")
        click.echo("  Restore librarian.db, or supersede the card. This command will not guess.")


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
@click.option(
    "--out",
    default=None,
    help="Backups ROOT to write a timestamped set into (default: <data_dir>/backups/).",
)
@click.option(
    "--keep",
    # IntRange, not int: `--keep -1` used to run the whole backup and THEN traceback out of
    # prune_backup_sets' guard. A usage error belongs before any work happens.
    type=click.IntRange(min=0),
    default=None,
    help="Delete all but the newest N backup sets in the destination, after this one succeeds.",
)
@click.pass_context
def backup_cmd(ctx, out, keep):
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

    # PRUNE AFTER THE NEW SET LANDS, NEVER BEFORE (techne#742). Pruning first would, on a
    # keep=1 schedule, delete the only good backup and then discover the disk is full — the
    # window where the operator has nothing is exactly the window a backup exists to remove.
    # `protect` is belt-and-braces on top of that ordering.
    retention_incomplete = False
    if keep is not None:
        from techne.core.maintenance import prune_backup_sets

        # `apply_keep=False` WHEN THE RUN CAPTURED NOTHING (Prudence, #755 round 7). The
        # keep window means "keep the newest N backups", and a run that backed up nothing
        # did not produce one — so weighing real archives against it deletes them on the
        # strength of a failure. Empty sets are still reclaimed, since they hold no
        # database and there is nothing in them to lose. The exit-4 check below used to
        # come AFTER this call: the alarm sounded after the irreversible step.
        removed, failed = prune_backup_sets(
            res.dest.parent, keep, protect=res.dest, apply_keep=bool(res.backed_up)
        )
        if removed:
            # DO NOT CLAIM A CEILING THAT WAS NOT APPLIED (Prudence, #755 round 8, Nit 2).
            # With `apply_keep=False` only empty sets are reclaimed, so `removed` is
            # non-empty while real sets sit above `keep` untouched — printing "keeping 1"
            # over three surviving sets is an assertion the run did not make.
            if res.backed_up:
                click.echo(f"pruned {len(removed)} older set(s), keeping {keep}")
            else:
                click.echo(
                    f"reclaimed {len(removed)} empty set(s) — keep window NOT applied, "
                    "this run captured nothing"
                )
        # SAY SO WHEN RETENTION DID NOT HAPPEN (Prudence, #755 Important 2). On a nightly
        # timer a silently-failing prune fills the disk exactly as no `--keep` would.
        for path, err in failed:
            click.echo(f"could not prune {path.name}: {err}", err=True)
        if failed:
            click.echo(f"retention INCOMPLETE: {len(failed)} set(s) could not be removed", err=True)
            retention_incomplete = True
    if res.skipped_absent:
        click.echo(f"skipped (absent): {', '.join(res.skipped_absent)}")
    if not res.backed_up:
        # A BACKUP OF ZERO DATABASES IS NOT A BACKUP (techne#742). Said loudly AND in the
        # exit status, because on a timer the status is the only half anyone sees — and a
        # green nightly job that captures nothing is worse than a red one, being a false
        # belief that you are covered until the day you need to restore. Takes precedence
        # over `retention_incomplete`: both can be true, and this is the more serious.
        from techne.exit_codes import EX_NOTHING_BACKED_UP

        click.echo(
            "NO DATABASES WERE BACKED UP — the set is empty. Check that this is the right "
            f"store: {config.data_dir}",
            err=True,
        )
        ctx.exit(EX_NOTHING_BACKED_UP)
    if retention_incomplete:
        # EXIT 3, NOT 0 (Prudence, #755 Important 2, second half). Saying it on stderr was
        # not enough: both units this PR ships route stderr somewhere nobody reads until
        # something is already wrong (the plist to `backup.log`, the systemd unit to the
        # journal), and the ONLY thing `systemctl --user status` or `launchctl list`
        # surfaces is EXIT STATUS. At 0 that reads green forever while retention is
        # permanently broken — the disk-fills-silently outcome this PR exists to prevent,
        # one level up.
        #
        # THE CODE IS NAMED IN `techne/exit_codes.py`, not spelled here. That module's
        # stated rule is that a private convention scattered across call sites is not a
        # contract, and a bare `3` at this line would have been exactly that — for a
        # supervised entrypoint, which is the class the module exists for. Its docstring
        # carries why the backup succeeding rules out every sysexits failure code.
        # Exiting LAST so a run that did back up still prints its full report first.
        from techne.exit_codes import EX_RETENTION_INCOMPLETE

        ctx.exit(EX_RETENTION_INCOMPLETE)


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


@main.command()
@click.option(
    "--session",
    "session_id",
    default=None,
    help="Session to profile thread detection on (default: the largest).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the raw report as JSON.")
@click.pass_context
def profile(ctx, session_id, as_json):
    """Profile the hot paths: import cost, thread detection, query plans.

    Read-only. A repeatable latency-at-scale check the test suite can't provide
    (techne#359) — run it against a real-size DB, or gate thresholds in CI, so
    the #350/#351 class surfaces before it hits the hook budget.
    """
    config = ctx.obj["config"]
    if not config.db_path.exists():
        click.echo("techne not initialized. Run: techne init")
        return

    from techne.core.profiling import format_report, run_profile

    report = run_profile(config.db_path, session_id)
    click.echo(json.dumps(report, indent=2) if as_json else format_report(report))


if __name__ == "__main__":
    main()
