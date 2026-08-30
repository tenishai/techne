"""The agent-trigger convention: indexed so it fires, tested so it does not rot (#756/#757).

techne's failures are overwhelmingly silent, which is the class a human never thinks to ask
about — so parts of the docs are written as TRIGGERS: conditions under which an agent should
speak up unprompted. Two things were missing when the convention shipped in #755, both found
by Prudence reviewing it:

  #756  A trigger that lives only in a document fires only if that document was already
        open — the failure the documents were meant to fix, one level up. It needs an index
        on an always-loaded surface. `AGENTS.md` is that surface, and this file enforces the
        index BOTH DIRECTIONS, the same rule `check_arch_cards.py` applies to cards.

  #804  THIS FILE ALSO COVERS THE GUIDE'S OTHER AGENT-FACING BLOCK, and that is not
        implied by the two issues above. `docs/reflect-guide.md`'s "copy this into your
        agent's context" block sits BELOW the trigger block, so `_trigger_blocks()` does
        not see it and `TRIGGER_WITNESSES` cannot key on it — it is outside the
        both-directions gate entirely. Prudence reverted that block wholesale to base and
        all 39 tests here passed. `test_the_copy_paste_block_teaches_both_populations` is
        its only witness, and nothing names IT as a witness, so deleting it would leave
        the suite green and the accounting looking complete — verbatim the failure this
        file already records below. Said here because a docstring is the only place that
        can say it.

  #757  Architecture cards have `anchor:` shas; trigger blocks had nothing. A bullet saying
        "`techne doctor` does not check this" keeps firing, confidently, after that stops
        being true. What is mechanically checkable is checked here; what is not is MARKED,
        so the set does not read as uniformly verified.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def config_free_tmp_home(tmp_path):
    """A Config on a throwaway store, for the two tests that actually run techne."""
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    init_db(cfg.db_path).close()
    return cfg


AGENTS = REPO / "AGENTS.md"
TRIGGER_MARKER = "Agent: raise this unprompted"

#: A bullet inside a blockquoted trigger block. `> - ` ONLY was too narrow: a `*` bullet
#: and a nested (indented) one both landed unaccounted (Prudence, #764 round 2). The
#: failure mode that has bitten this file twice is a pattern narrower than the thing it
#: claims to match, not one that is too broad.
_BULLET_RE = re.compile(r"^>\s*[-*+]\s")


def _headings(path: Path) -> list[str]:
    """Real `#` headings — NOT lines that merely start with `#`.

    A shell comment inside a fenced block starts with `#` too, and `docs/deployment.md`
    has nine of them (`# edit ExecStart ...`). Counting those let an index anchor
    "resolve" to a code comment and pass, which is a hole in the gate rather than a
    cosmetic one. Found by rendering the headings through GitHub's own markdown API and
    getting 34 anchors for 35 candidate lines. `import_md._split_into_sections` already
    tracks fences for the same reason.
    """
    out, in_fence = [], False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if line.startswith("#") and not in_fence:
            out.append(line)
    return out


def _slug(heading: str) -> str:
    """GitHub's heading-to-anchor slug, which is what the index links against.

    THIS FUNCTION IS BOTH SIDES OF THE TEST BELOW, so that test cannot tell you the links
    RENDER — only that they are self-consistent with this implementation. If `_slug`
    disagreed with GitHub, every link in `AGENTS.md` would be broken and the suite would
    stay green. Saying so rather than letting the check read as stronger than it is.

    Cross-validated once, out of band, against GitHub's own renderer (`POST /markdown`,
    reading the `class="anchor" href="#…"` it emits) on 2026-08-25: 26 headings, 26
    anchors, zero disagreements, and all four anchors used by the index resolve. Redo it
    that way if you change this function — a local test cannot check it by construction.
    """
    text = heading.strip().lstrip("#").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)  # drop backticks, punctuation
    return re.sub(r"\s", "-", text)


def _index_rows() -> list[tuple[str, str]]:
    """(doc path, anchor) for every row in the AGENTS.md trigger table."""
    section = AGENTS.read_text().split("## Agent triggers")[1].split("## Architecture cards")[0]
    return re.findall(r"\]\((docs/[^)#]+)#([^)]+)\)", section)


def _trigger_blocks(path: Path) -> list[list[str]]:
    """Every trigger block in a document, as lists of bullet lines.

    `split(marker)[1]` read only the FIRST (Prudence, #764). A second block in the same
    document was invisible: its bullets were unaccounted, and because the index compares
    at document granularity, the doc still counted as indexed. Two silent holes from one
    assumption that a document carries at most one block.
    """
    blocks, current, in_block = [], [], False
    for line in path.read_text().splitlines():
        if TRIGGER_MARKER in line:
            in_block, current = True, []
            continue
        if in_block:
            if _BULLET_RE.match(line):
                current.append(line)
            elif not line.startswith(">"):
                blocks.append(current)
                in_block = False
    if in_block:
        blocks.append(current)
    return blocks


def _docs_with_trigger_blocks() -> set[str]:
    return {
        str(p.relative_to(REPO))
        for p in (REPO / "docs").rglob("*.md")
        if TRIGGER_MARKER in p.read_text()
    }


# --------------------------------------------------------------- #756: the index


def test_headings_exclude_fenced_code_comments():
    """A shell comment inside a fenced block starts with `#` too — `docs/deployment.md` has
    nine (`# edit ExecStart ...`). Counting them as headings let an index anchor "resolve"
    to a code comment, which is a hole in the gate rather than a cosmetic one. Found by
    rendering through GitHub's markdown API and getting 34 anchors for 35 candidate lines."""
    deployment = REPO / "docs" / "deployment.md"
    naive = [ln for ln in deployment.read_text().splitlines() if ln.startswith("#")]

    real = _headings(deployment)

    assert len(naive) > len(real), "fixture no longer contains fenced `#` comments"
    assert not any("edit ExecStart" in h for h in real), "a code comment is being read as a heading"


def test_the_index_is_not_empty():
    """A guard that silently matches nothing is worse than no guard — every assertion below
    would pass vacuously if the table parse broke."""
    assert len(_index_rows()) >= 8, f"parsed {len(_index_rows())} trigger rows from AGENTS.md"


@pytest.mark.parametrize("doc,anchor", _index_rows())
def test_every_indexed_trigger_points_at_a_real_section(doc, anchor):
    """A row whose link does not resolve sends the agent nowhere, which is indistinguishable
    from having no row at all — except that it looks covered."""
    path = REPO / doc
    assert path.is_file(), f"{doc} does not exist"
    slugs = {_slug(line) for line in _headings(path)}
    assert anchor in slugs, f"{doc}#{anchor} resolves to no heading"


def test_every_document_with_a_trigger_block_is_indexed():
    """THE DIRECTION THAT MATTERS MOST (#756). An unindexed trigger is one that never fires,
    exactly as an unindexed arch card is one that is never protected — and both fail the same
    way: silently, looking finished."""
    indexed = {doc for doc, _ in _index_rows()}
    missing = _docs_with_trigger_blocks() - indexed

    assert not missing, f"trigger blocks with no row in AGENTS.md: {sorted(missing)}"


# --------------------------------------------------- #757: the claims that can rot

def test_the_command_triggers_tell_agents_to_offer_still_exists():
    """"offer `techne backup` instead" is a claim about this codebase, and it is the
    remedy every WAL trigger points at."""
    from click.testing import CliRunner

    from techne.cli import main

    result = CliRunner().invoke(main, ["backup", "--help"])

    assert result.exit_code == 0, "the triggers offer a command that no longer exists"
    assert "--keep" in result.output, "the scheduling trigger's `--keep` advice is stale"
    # THE CLAIM RESTS ON THE DEFAULT, NOT ON THE FLAG EXISTING (Prudence, #764 round 2).
    # Both doc claims — "`--keep` is not optional on a timer" and "a nightly job without it
    # fills the disk" — are only true while the default is "no ceiling". Set `default=14`
    # and both become wrong advice while `--keep` is still present and this test still
    # green. Asserting the flag's presence tested the wrong half of the sentence.
    import click

    keep = next(
        p
        for p in main.commands["backup"].params
        if isinstance(p, click.Option) and p.name == "keep"
    )

    assert keep.default is None, (
        f"`--keep` now defaults to {keep.default!r} — deployment.md and the shipped units "
        "both tell operators a job without `--keep` grows without bound. It no longer does."
    )


#: The check names `techne doctor` runs today. Pinned, not keyword-matched.
#:
#: The keyword version asked whether any check NAME contained "backup"/"schedul" — and
#: Prudence walked straight through it with `check_timer_liveness`, which is exactly what
#: someone would call this feature (#764). AN ABSENCE CANNOT BE PROVED BY GREP: you can
#: only ask whether the surface changed. So the surface is pinned, and any new check trips
#: this and forces a human to re-read the trigger that claims doctor does not do this.
#: Re-bless by adding the name here, the same way an arch card is re-blessed.
DOCTOR_CHECKS_AT_LAST_REVIEW = {
    # techne#824. RE-READ DONE: deployment.md:78 claims `techne doctor` does
    # not check whether anything SCHEDULES A BACKUP; a card-backlog check does
    # not touch scheduling, so that trigger's claim stands as written.
    "check_pending_cards",
    "check_config_trust",
    "check_hooks",
    "check_instance_identity",
    "check_maintenance_rails",
    "check_mcp_registration",
    "check_model_bound_rails",
    "check_outbox_targets",
    "check_runtime_loaded",
    "check_runtime_revision",
    "check_runtime_torn",
    "check_schema_versions",
    "check_vector_coverage",
}


def test_the_absence_the_backup_trigger_asserts_is_still_absent():
    """"`techne doctor` does not check this" is the sharpest claim to let rot: if doctor
    GAINS a scheduling check, the trigger keeps telling operators it has not got one, and
    the agent talks them out of trusting a signal that now works."""
    from techne.core.checks import _REGISTRY

    now = {fn.__name__ for fn in _REGISTRY}
    added = now - DOCTOR_CHECKS_AT_LAST_REVIEW

    assert not added, (
        f"`techne doctor` gained checks since this was last reviewed: {sorted(added)}. "
        "Read deployment.md's backup trigger — it tells operators doctor does NOT check "
        "whether anything schedules a backup. If one of these does, the trigger is wrong. "
        "If none does, add them here."
    )


#: THE WHOLE COMMAND TREE. Not filtered by "archiv", which is how the second version still
#: missed things: `knowledge rotate-transcripts` archives transcripts and contains neither
#: string (Prudence, #764 round 2). WHICH COMMANDS COULD ARCHIVE TRANSCRIPTS CANNOT BE
#: DETERMINED FROM THEIR NAMES, so the only sound mechanical question is whether the
#: surface changed at all.
#:
#: Yes, this trips on every CLI addition. That is the cost, and it is the right side to err
#: on: the failure mode that has bitten this file three times is a check NARROWER than the
#: claim it is filed under, never one that was too noisy. Re-bless by adding the entry,
#: the same way an arch card is re-blessed when its seam moves.
COMMAND_TREE_AT_LAST_REVIEW = {
    "architect",
    # RE-BLESSED 2026-08-26 after #758 landed `architect add`. The question this pin asks
    # is not "did the CLI change" but "does the new command falsify the trigger", and it
    # was answered rather than waved through: `architect add` writes ONE pattern to
    # architect.db from --name/--source/--body, and has no transcript path of any kind.
    # deployment.md's migration trigger — "nothing archives session transcripts" — still
    # holds. This is the pin's first real firing, and it did the job it was built for:
    # it made a CLI change stop and answer a documentation question.
    "architect add",
    # techne#754. Both read architect.db and neither writes anything outside it —
    # `dedup` queries two tables, `snapshot` hashes pattern content. Re-verified
    # against deployment.md's migration trigger: "nothing archives transcripts"
    # is unaffected by either.
    # techne#754 — the research-sandbox installer. `install`/`remove` write and
    # delete ONE definition file in the platform config dir; `status` only reads.
    # Re-verified against deployment.md's migration trigger: none of the three
    # touches session transcripts, so "nothing archives transcripts" still holds.
    "agents",
    "agents install",
    "agents remove",
    "agents status",
    "architect dedup",
    "architect snapshot",
    "architect applies",
    "architect backfill",
    "architect export",
    "architect graph",
    "architect ingest",
    "architect outcomes",
    "architect realized",
    "architect related",
    "architect retire-legacy",
    "architect reverdict",
    "architect search",
    "architect show",
    "architect sweep-legacy-verdicts",
    "architect tag",
    "architect topic",
    "architect unrealized",
    "archivist",
    "archivist query",
    "archivist schema",
    "audit",
    "backup",
    "bindings",
    "capabilities",
    "cards",
    "cards repair",
    "codex",
    "codex blast-radius",
    "codex callers",
    "codex index",
    "codex index-all",
    "codex search",
    "codex stats",
    "codex watch",
    "compact",
    "context",
    "custodian",
    "custodian run",
    "custodian status",
    "deposit",
    "doctor",
    # techne#754 — prints bundled markdown from techne/docs/. Re-read
    # deployment.md's migration trigger when adding it: `guide` reads files
    # OUT of the package and writes nothing, so "nothing archives transcripts"
    # is unaffected. Claim re-verified, not merely re-pinned.
    "guide",
    "evidence",
    "fsck",
    "history",
    "identity",
    "identity show",
    "import",
    "init",
    "knowledge",
    "knowledge add",
    "knowledge archive-scan",
    "knowledge export",
    "knowledge list",
    "knowledge unarchive",
    "librarian",
    "librarian card",
    "librarian cards",
    "librarian doctor",
    "librarian index-markdown",
    "librarian index-transcripts",
    "librarian migrate-store",
    "librarian search",
    "librarian show",
    "memory",
    "memory doctor",
    "memory sync",
    "migrate-summaries",
    "observe",
    "outbox",
    "outbox dead",
    "outbox flush",
    "outbox flush-sinks",
    "outbox prune-orphans",
    "outbox purge",
    "outbox status",
    "outbox sweep",
    "probe",
    "profile",
    "prompts",
    "prompts export",
    "prompts path",
    "reconcile",
    "reflect",
    "reflect discard",
    "reflect emit",
    "reflect ingest",
    "reflect promote",
    # RE-BLESSED 2026-08-27 after #646 landed `reflect reconcile`. Answered, not waved
    # through: it reads `reflection_queue` and deletes undrained rows whose thread is
    # absent from the session's current detection. It touches no transcript, no session
    # JSONL and no export path — deployment.md's "nothing archives session transcripts"
    # still holds. Second real firing of this pin, and again it made a CLI change stop
    # and answer a documentation question.
    "reflect reconcile",
    "reflect review",
    "reflect stats",
    "restore",
    "search",
    "serve",
    "session-end",
    "sinks",
    "sinks describe",
    "sinks list",
    "skills",
    "skills install",
    "skills remove",
    "skills status",
    "status",
    "threads",
    "threads redetect",
    "threads show",
    "vectors",
    "vectors backfill",
    "vectors status",
    "watch",
}


def _command_tree() -> set[str]:
    import click

    from techne.cli import main

    names = set()
    for name, cmd in main.commands.items():
        names.add(name)
        if isinstance(cmd, click.Group):
            names.update(f"{name} {sub}" for sub in cmd.commands)
    return names


def test_the_absence_the_migration_trigger_asserts_is_still_absent():
    """"nothing archives transcripts" — an absence, which nothing about reading the code
    reminds you to re-check, and which cannot be established by matching command names."""
    now = _command_tree()
    added = now - COMMAND_TREE_AT_LAST_REVIEW

    assert not added, (
        f"the CLI gained commands since this was last reviewed: {sorted(added)}. Read "
        "deployment.md's migration trigger — it tells operators nothing archives session "
        "transcripts. If one of these does, the trigger is wrong. If none does, add them."
    )


#: Every option `techne restore` exposes, and every parameter `restore_all_dbs` takes.
#:
#: TWO SURFACES, BECAUSE THE CLAIM LIVES IN THE SECOND ONE. "all-or-nothing across domains"
#: is a property of `restore_all_dbs`, whose signature has no domain parameter at all — the
#: CLI options are downstream of it. Pinning only the options would have caught a `--only`
#: flag and missed a `domains=` kwarg added to the function, which another caller could
#: reach without any CLI change. Asked myself whether the surface I pinned actually carries
#: the claim, and the first answer was no.
RESTORE_OPTIONS_AT_LAST_REVIEW = {"backup_dir", "no_pre_backup", "yes"}
RESTORE_ALL_DBS_PARAMS_AT_LAST_REVIEW = {"config", "src_dir", "pre_restore_backup"}


def test_the_restore_trigger_is_still_all_or_nothing():
    """"a single-db restore desyncs cross-db pointers" — the trigger's advice depends on
    `restore` having no per-domain escape hatch.

    TWO REPAIRS, both from Prudence attacking this directly (#764 round 2). It had no
    `exit_code` guard, so renaming the command away entirely gave exit 2 and "No such
    command" and the test stayed GREEN — a test that passes when the thing it tests does
    not exist. And it grepped for the single string `--domain` while its siblings pin a
    surface, so a real per-domain hatch named `--only` sailed through. Same narrowness."""
    import click

    from techne.cli import main

    assert "restore" in main.commands, "restore no longer exists — the trigger is stale"
    # `click.Parameter`, NOT `click.Option` — filtering to Options made a positional
    # `domains` ARGUMENT invisible to the pin (Prudence, #764 round 3). A narrower filter
    # inside the fix for a narrow filter, which is now the seventh instance of that in
    # this PR. `backup_dir` is the existing positional and is pinned with the rest.
    options = {
        p.name for p in main.commands["restore"].params if isinstance(p, click.Parameter)
    }

    assert options == RESTORE_OPTIONS_AT_LAST_REVIEW, (
        f"`restore`'s options changed: {sorted(options)}. Read db-per-domain.md's restore "
        "trigger — it tells operators restore is all-or-nothing across domains. If one of "
        "these selects a subset, the trigger is wrong. If none does, re-bless this pin."
    )

    # THE SURFACE THE CLAIM ACTUALLY LIVES ON. A domain-selecting parameter added here
    # would make the trigger wrong without touching a single CLI option.
    import inspect

    from techne.core.maintenance import restore_all_dbs

    params = set(inspect.signature(restore_all_dbs).parameters)

    assert params == RESTORE_ALL_DBS_PARAMS_AT_LAST_REVIEW, (
        f"`restore_all_dbs`'s signature changed: {sorted(params)}. If one of these selects "
        "a subset of domains, restore is no longer all-or-nothing and the trigger is wrong."
    )


# ------------------------------------------------- #757: what is NOT checked, said aloud

#: Bullets in each document that NO test here can check, declared per document.
#:
#: Hardcoding one path and one count meant the index tests would extend to #753/#754 for
#: free and the staleness tests not at all — the half that gets expensive once three files
#: carry the convention (Prudence, #764). Keyed by document now, and a document carrying a
#: block with no entry is a failure, so a new one cannot land without this decision being
#: made.
#:
#: THE WAL BULLETS ARE NOT IN HERE ANY MORE, AND THAT IS THE POINT. I had exempted four of
#: them as "SQLite's own semantics, not claims about this codebase". They all rest on
#: `PRAGMA journal_mode=WAL` — ONE LINE of techne's own code (`core/database.py:773`).
#: Prudence flipped it to `DELETE` and 15 tests passed. Exempting by category is the same
#: mistake she caught on #755 round 2, and the category I chose was wrong on inspection.
#: WHICH TEST IS THE WITNESS FOR EACH BULLET, keyed by a distinctive phrase from the bullet.
#:
#: THE COUNT WAS DECORATIVE (Prudence, #764 round 4). The previous shape was
#: `{document: (total, unchecked)}` and the only assertion touching the second element was
#: `unchecked <= total` — `0 <= 4`, true of anything. She proved it twice: declaring EVERY
#: bullet unchecked (the opposite of what AGENTS.md claims) passed 22, and deleting
#: `test_the_wal_premise_…`, the sole witness behind all four `db-per-domain.md` bullets,
#: passed 21 while the accounting still declared full coverage.
#:
#: That is the absent-transformation shape from #758, not the narrow-check shape this file
#: kept producing: THE BULLET→TEST LINK EXISTED NOWHERE, so there was nothing to mutate and
#: no mutant could have exposed it. Naming the witness is what makes the link checkable.
TRIGGER_WITNESSES = {
    "have not mentioned `techne init`": "test_only_init_writes_the_backfill_segment",
    "recall is empty, that semantic search": (
        "test_the_hook_runs_backfill_no_download_and_the_cli_does_not"
    ),
    "`cp`, `rsync`, `scp`, `mv` or tar": "test_the_wal_premise_every_wal_trigger_rests_on",
    "file-level backup tool": "test_the_wal_premise_every_wal_trigger_rests_on",
    "propose deleting one": "test_the_wal_premise_every_wal_trigger_rests_on",
    "moving techne between machines": "test_the_wal_premise_every_wal_trigger_rests_on",
    "cron job, timer, launchd plist": "test_the_cli_really_does_not_prune_without_keep",
    "do I have backups?": "test_the_absence_the_backup_trigger_asserts_is_still_absent",
    "machine migration, a wipe": "test_the_absence_the_migration_trigger_asserts_is_still_absent",
    "They are restoring": "test_the_restore_trigger_is_still_all_or_nothing",
    "zero rows": "test_only_the_codex_distinguishes_an_empty_index_from_no_match",
    "rely on `librarian_search` for coverage": "test_nothing_automatically_indexes_transcripts",
    "why the architect is empty": "test_the_architect_feeder_ships_and_the_research_skills_do_not",
    "`reflection_queue`": "test_the_unemittable_reporter_still_refuses_to_drain",
    "reflect.enabled: false": "test_disabling_reflect_silences_the_nudge_but_not_the_debt",
    "whether reflection is running": "test_an_unbound_automatic_pass_is_the_default_posture",
    "live techne MCP server": "test_the_mcp_server_pins_config_before_it_serves",
    "drain a large session": "test_the_cli_emit_can_write_the_batch_to_a_file",
    "having drained a session": "test_emit_is_a_bounded_cursor_not_one_unbounded_pass",
    # techne#754 — the two procedures absorbed from the external skills repo.
    "pastes a repo URL": "test_the_dedup_step_the_url_trigger_names_exists",
    '"look at" or "check out"': "test_the_dedup_step_the_url_trigger_names_exists",
    "why the architect is empty, or how patterns get in":
        "test_the_dedup_step_the_url_trigger_names_exists",
    "reports `research-readonly` **absent or widened**":
        "test_the_sandbox_the_trigger_names_is_installable",
    "about to build something substantial": "test_the_survey_guide_teaches_the_same_sandbox",
    '"the landscape" or "who else does this"': "test_the_survey_guide_teaches_the_same_sandbox",
}


def test_every_bullet_names_a_witness():
    """A bullet with no named test is an unverified claim wearing the same clothes as a
    verified one, which is what this accounting exists to prevent."""
    unwitnessed = [
        line.strip()[:70]
        for doc in sorted(_docs_with_trigger_blocks())
        for block in _trigger_blocks(REPO / doc)
        for line in block
        if not any(key in line for key in TRIGGER_WITNESSES)
    ]

    assert not unwitnessed, (
        f"trigger bullets with no witness: {unwitnessed}. Name the test that checks each, "
        "or record it as unchecked — do not leave it looking covered."
    )


def test_every_named_witness_exists():
    """The other direction: a witness named here and absent from the module is a link that
    reads as real and is not. Deleting the WAL test used to leave the accounting intact."""
    import sys

    module = sys.modules[__name__]
    missing = sorted({t for t in TRIGGER_WITNESSES.values() if not hasattr(module, t)})

    assert not missing, f"named as witnesses but not defined in this module: {missing}"


def test_the_wal_premise_every_wal_trigger_rests_on(tmp_path):
    """FOUR BULLETS I HAD EXEMPTED AS "NOT ABOUT THIS CODEBASE" (Prudence, #764). "copying
    the `.db` loses recent writes", "`-wal` is committed data", "copy all three" — every one
    of them is true BECAUSE techne opens its databases in WAL mode, which is one line of our
    code. In `DELETE` mode there is no `-wal` beside a live database and all four bullets
    become confidently wrong advice. The premise is ours, so it is testable, so it is
    tested."""
    from techne.core.database import connect, init_db

    # `tmp_path`, NOT a fixed name in the tests directory. The first version wrote
    # `tests/_wal_probe.db` — a shared path under `-n 4`, and a write into the source tree
    # that outlives the run if the test dies between `init_db` and the cleanup. pytest
    # gives every test its own directory precisely so neither can happen.
    db = tmp_path / "wal_probe.db"
    init_db(db).close()
    conn = connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    assert mode.lower() == "wal", (
        f"journal_mode is {mode!r} — every `-wal` trigger in docs/db-per-domain.md is now "
        "wrong, including the advice to copy all three files"
    )


def test_the_cli_really_does_not_prune_without_keep(config_free_tmp_home, monkeypatch):
    """THE GATE, WHICH NEITHER OF MY OTHER TWO TESTS PINNED (Prudence, #764 round 3). I
    pinned the declared default (`keep.default is None`) and I pinned that `backup_all_dbs`
    does not prune. Neither touches `if keep is not None:` in `backup_cmd` — the gate that
    JOINS them. Adding `if keep is None: keep = 14` there left 160 tests green while
    `techne backup` on a timer became bounded and deployment.md became wrong advice.

    Two correct halves and no test on the seam between them. Driven through the real CLI,
    which is the only place the gate exists."""
    from click.testing import CliRunner

    import techne.core.maintenance as maint
    from techne.cli import main
    from techne.config import Config

    cfg = config_free_tmp_home
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
    called: list[int] = []
    real_prune = maint.prune_backup_sets
    monkeypatch.setattr(
        maint, "prune_backup_sets", lambda d, k, **kw: called.append(k) or real_prune(d, k, **kw)
    )

    result = CliRunner().invoke(main, ["backup"])

    assert result.exit_code == 0, result.output
    # ASSERT THE GATE, NOT ITS EFFECT. My first version ran backup three times and counted
    # sets — which survives her mutation, because a default ceiling of 14 prunes nothing at
    # three sets. Testing the downstream effect made the test's sensitivity depend on the
    # very number the mutation chooses. Whether the pruner is CALLED is the claim.
    assert called == [], (
        f"`techne backup` with no `--keep` invoked retention with keep={called} — "
        "deployment.md and both shipped units tell operators a job without `--keep` grows "
        "without bound. It no longer does."
    )


def test_backup_without_keep_really_does_grow_without_bound(config_free_tmp_home):
    """THE SECOND HALF OF THE `--keep` TRIGGER, which nothing was checking. That bullet
    makes two claims: `--keep` is the remedy (covered by the CLI test above) and WITHOUT IT
    GROWTH IS UNBOUNDED. I counted the bullet as checked on the strength of the first half
    only — which is the completeness claim I got wrong twice on #755, so it is asserted here
    rather than assumed. If `backup` ever gains a default ceiling, this trigger becomes
    advice about a problem that no longer exists and the bullet has to be rewritten."""
    from techne.core.maintenance import backup_all_dbs

    cfg = config_free_tmp_home
    for _ in range(3):
        backup_all_dbs(cfg)

    sets = list((Path(cfg.data_dir) / "backups").iterdir())

    assert len(sets) == 3, (
        f"{len(sets)} sets after 3 unbounded runs — `backup` may now prune by default, "
        "which would make deployment.md's `--keep` trigger wrong"
    )


def test_only_the_codex_distinguishes_an_empty_index_from_no_match(config_free_tmp_home, tmp_path):
    """ASSERTED AT THE ENVELOPE, WHICH IS THE SURFACE AN AGENT READS (Prudence, #771 round
    2 — Question A's second direction, still unfixed after round 1). My previous version
    called `zero_result_notice`, a core function; `_with_binding`'s own docstring records
    Prudence catching the identical miss in techne#725, where "the domain tests asserted
    the envelope of `domains.codex_search`, which no MCP caller reaches".

    BOTH STATES, because Prudence found the arrangement I could not (#771 round 3). The
    missing ingredient was `git init`, NOT the db: `resolve_repo_db_for_query` calls
    `primary_repo_root()` FIRST and returns None for a non-repo, short-circuiting before it
    looks at the db at all. She teeth-checked the diagnosis by removing only the `git init`
    and reproducing the `absent` I had been getting. Recording a gap was the honest fallback;
    closing it is better."""
    import subprocess

    from techne.core.codex import codex_db_for_repo
    from techne.mcp_server import _codex_search

    # STATE 1 — nothing resolves: a plain directory, not a repo
    unindexed = tmp_path / "not-a-repo"
    unindexed.mkdir()
    absent = _codex_search({"query": "zqxjkv_no_such_symbol", "repo": str(unindexed)},
                           config_free_tmp_home)

    assert absent["results"] == [] and absent.get("notice"), "no qualification on the absent state"
    assert absent["binding"] == "absent", absent["binding"]
    assert "codex index" in absent["notice"], absent["notice"]

    # STATE 2 — an index that RESOLVED and holds nothing
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True)
    codex_db_for_repo(config_free_tmp_home, empty)
    resolved = _codex_search({"query": "zqxjkv_no_such_symbol", "repo": str(empty)},
                             config_free_tmp_home)

    assert resolved["binding"] == "local", (
        f"expected a resolved binding for an initialised repo, got {resolved['binding']!r}"
    )
    assert "INDEX IS EMPTY" in (resolved.get("notice") or ""), (
        f"the resolved-but-empty state lost its qualification: {resolved.get('notice')!r}"
    )
    assert absent["notice"] != resolved["notice"], (
        "the two states now give the same notice — the doc says they are distinguishable"
    )


def test_the_other_three_domains_give_no_such_signal(config_free_tmp_home):
    """THE WORD `only` IN THE SIBLING TEST'S NAME WAS UNASSERTED (Prudence, #771 round 4).
    That test pins the codex exception well and never touches the archivist, librarian or
    architect — so "for those three, empty and unpopulated are indistinguishable", which is
    the premise the agent's ACTION rests on, had no witness at all. She probed it: all three
    return `count: 0`, `results: []`, and no `notice` key. True today, and now pinned.

    If any of the three gains a qualification, this fails and the trigger should be narrowed
    to exclude it — the same way the codex is excluded now."""
    from techne.core import domains

    unqualified = {}
    for name in ("archivist", "librarian", "architect"):
        fn = getattr(domains, f"{name}_search")
        result = fn(config_free_tmp_home, "zqxjkv_no_such_term")
        unqualified[name] = {
            "count": result.get("count"),
            "results": result.get("results"),
            "has_notice": bool(result.get("notice")),
        }

    for name, shape in unqualified.items():
        assert shape["results"] == [] and shape["count"] == 0, f"{name}: {shape}"
        assert not shape["has_notice"], (
            f"{name} now qualifies its empty answer ({shape}) — four-functions.md tells "
            "operators the codex is the ONLY domain that does. Narrow the trigger."
        )


def test_nothing_automatically_indexes_transcripts(config_free_tmp_home):
    """"nothing indexes transcripts automatically" — an absence, and the one the librarian
    trigger turns on. `self-care.md` states the consequence: the only index with no
    automatic feeder, and a 4%-coverage search looks exactly like a working one.

    PINNED AT THE IMPORT SURFACE, WHICH IS WHAT IT CAN SEE — and the doc is now phrased to
    match, saying "nothing IN THIS REPO runs it" rather than "nothing runs it". A
    deployment can wire `index-transcripts` into a `settings.json` hook as a shell string,
    which no import graph reaches; techne#704 forced that same correction on the identical
    reasoning about `vectors backfill`. A test that cannot see shell hooks must not back a
    sentence that claims there are none."""
    importers = {
        str(f.relative_to(REPO))
        for f in (REPO / "techne").rglob("*.py")
        if "transcript_library" in f.read_text() and f.name != "transcript_library.py"
    }

    assert importers == {"techne/cli.py"}, (
        f"transcript indexing gained a caller: {sorted(importers)}. four-functions.md tells "
        "operators nothing runs `index-transcripts` for them. If one of these does, that is "
        "now false."
    )


def test_the_architect_feeder_ships_and_the_research_skills_do_not(config_free_tmp_home):
    """The architect trigger makes two claims at once: `techne architect add` SHIPS, and the
    procedures that fill the store in bulk DO NOT. Both are checked, because half of it
    going stale would make the advice wrong in either direction."""
    import click

    from techne.cli import main

    assert "add" in main.commands["architect"].commands, (
        "`techne architect add` no longer ships — the trigger offers a command that is gone"
    )
    assert isinstance(main.commands["architect"], click.Group)

    bundled = {f.stem for f in (REPO / "techne" / "skills").glob("*.md")}

    assert not bundled & {"investigate-url", "survey-field"}, (
        f"a research skill now ships with techne ({sorted(bundled)}) — four-functions.md "
        "tells operators they live in tenishai/skills"
    )


def test_every_indexed_document_still_carries_a_trigger_block():
    """THE THIRD DIRECTION, which the gate was not enforcing. It checked that a document
    with a block has a row, and that a row's anchor resolves — but not that an indexed
    document still HAS a block. So deleting the last trigger from a document left its rows
    behind, pointing an agent at a section with nothing to raise.

    Found by mutation while fixing #753's block: removing the marker correctly left the
    other two checks silent, and nothing else spoke. The arch-card gate this convention
    models itself on enforces both directions, and `AGENTS.md` says this one does too."""
    indexed = {doc for doc, _ in _index_rows()}
    without_blocks = sorted(indexed - _docs_with_trigger_blocks())

    assert not without_blocks, (
        f"indexed in AGENTS.md but carrying no trigger block: {without_blocks}. Remove the "
        "row, or restore the block — a row pointing at nothing sends an agent to read a "
        "section with no trigger in it."
    )


# ------------------------------------------------- #791: the reflect-guide triggers


def _reflect_config(tmp_path, *, enabled: bool = True, min_obs: int = 3):
    """A throwaway store whose emittability threshold is small enough to straddle."""
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config()
    cfg.db_path = tmp_path / "reflect.db"
    cfg.data_dir = tmp_path
    cfg.reflect.enabled = enabled
    cfg.reflect.min_observations_per_thread = min_obs
    cfg.reflect.include_existing_knowledge = False  # skip FTS
    init_db(cfg.db_path).close()
    return cfg


def _seed_thread(cfg, *, n_obs: int):
    """One session, one persisted thread, `n_obs` observations against it."""
    from techne.core.state import start_session
    from techne.core.threads import Thread, save_threads

    session_id = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")
    conn = connect_db(cfg.db_path)
    try:
        for i in range(n_obs):
            conn.execute(
                "INSERT INTO observations (session_id, tool_name, content) VALUES (?,?,?)",
                (session_id, "Bash", f"obs {i}"),
            )
        conn.commit()
    finally:
        conn.close()

    thread = Thread(thread_num=1, label="t", summary="s", observation_indices=list(range(n_obs)))
    save_threads([thread], session_id, cfg.db_path, parent_links=[(f"{session_id}-1", None)])
    return session_id, thread


def connect_db(path):
    from techne.core.database import connect

    return connect(path)


def test_the_unemittable_reporter_still_refuses_to_drain(tmp_path):
    """`reflect-guide.md` tells the operator NOT to sweep these rows, and tells the agent to
    raise it unprompted — both rest on `unemittable_queue_rows` being a REPORTER. The day it
    starts draining (or stops seeing sub-threshold rows) that advice inverts: the guide would
    be telling people to preserve evidence the code is already destroying.

    Straddles the threshold deliberately — one observation under `min_observations_per_thread`
    — because that is the exact shape #646 produces, and asserts the row is BOTH reported and
    still undrained afterwards. Reporting without the second half would pass even if the
    function drained as a side effect."""
    from techne.core.reflect import enqueue_reflection_debt, unemittable_queue_rows

    cfg = _reflect_config(tmp_path, min_obs=3)
    session_id, _ = _seed_thread(cfg, n_obs=1)  # 1 < 3, so no prompt can ever render

    conn = connect_db(cfg.db_path)
    try:
        tid = conn.execute(
            "SELECT id FROM threads WHERE session_id = ?", (session_id,)
        ).fetchone()["id"]
    finally:
        conn.close()
    # Enqueue directly: this is the divergence #646 describes, where session_end queued a
    # row that emit will not produce a prompt for.
    enqueue_reflection_debt(cfg.db_path, session_id, thread_ids=[tid])

    stuck = unemittable_queue_rows(cfg, session_id)

    assert stuck == [tid], (
        f"unemittable_queue_rows returned {stuck}, expected the sub-threshold row {tid!r}. "
        "reflect-guide.md tells operators this surface is what splits payable debt from "
        "impossible debt, and tells agents to offer it instead of sweeping."
    )
    conn = connect_db(cfg.db_path)
    try:
        drained = conn.execute(
            "SELECT drained_at FROM reflection_queue WHERE thread_id = ?", (tid,)
        ).fetchone()["drained_at"]
    finally:
        conn.close()
    assert drained is None, (
        "unemittable_queue_rows DRAINED the row it reported. The guide's whole instruction — "
        "'evidence, not residue', do not sweep — assumes this function only looks."
    )


def test_disabling_reflect_silences_the_nudge_but_not_the_debt(tmp_path):
    """The trigger claims `reflect.enabled: false` is backwards: it silences the route that
    works while the debt keeps accruing. That is TWO claims about two different call sites,
    and the surprising half is the second one — `lifecycle.run_session_end` enqueues outside
    the `if config.reflect.enabled:` block that guards the automatic pass.

    Gate the enqueue on `enabled` and this test fails; without it the guide would be telling
    operators that disabling still accrues debt when it had quietly stopped."""
    from techne.lifecycle import run_session_end

    cfg = _reflect_config(tmp_path, enabled=False, min_obs=1)
    session_id, _ = _seed_thread(cfg, n_obs=4)

    run_session_end(cfg, session_id)

    conn = connect_db(cfg.db_path)
    try:
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM reflection_queue WHERE session_id = ?", (session_id,)
        ).fetchone()["n"]
    finally:
        conn.close()

    assert queued > 0, (
        "session_end enqueued NO reflection debt while reflect.enabled was false. "
        "reflect-guide.md tells operators that disabling does not stop the debt, only the "
        "nudge — if the enqueue is now gated, that sentence is wrong advice."
    )

    # THE OTHER HALF, WHICH NOTHING IN THIS REPO HELD (Prudence, #792 r1). The bullet
    # claims disabling silences the route that PRODUCES everything, and only the debt
    # clause was asserted. She flipped `if config.reflect.enabled:` at
    # hooks/session_start.py to `if True:` — the mutation that makes the first half false —
    # and the entire suite stayed green, 3651 passed. The accounting read as covered while
    # the claim underneath was unheld: the #764 shape again, one layer over.
    from techne.hooks.session_start import _run_phases_2_to_9

    parts: list[str] = []
    _run_phases_2_to_9(
        cfg,
        {"cwd": str(tmp_path), "session_id": session_id},
        run_ts="2026-01-01T00:00:00+00:00",
        db_path=cfg.db_path,
        output_parts=parts,
        failures=[],
    )
    disabled = "\n".join(parts)
    assert "pending reflection" not in disabled.lower(), (
        "the SessionStart nudge fired while reflect.enabled was false. The trigger tells "
        "agents that disabling silences the resident route — if the nudge survives it, the "
        "operator is being told to switch off something that is already off."
    )


def test_an_unbound_automatic_pass_is_the_default_posture(tmp_path, monkeypatch):
    """"Empty means the unattended pass is unbound by design and never ran; that is a normal
    posture, not a fault" — the agent is told to check this BEFORE calling reflection broken.

    Two halves: the shipped default really is empty, and an unbound pass really declines to
    call an LLM rather than falling back to `config.model`. The fallback is what #709 removed;
    restoring it would make the trigger's reassurance false and start unattended calls on
    stores that never opted in."""
    from techne.config import ReflectConfig
    from techne.core.reflect import reflect_on_session

    # THE DEFAULTS THE GUIDE DOES ARITHMETIC AGAINST (Prudence, #792 r3). Nothing in the
    # repo held these, and three places in the guide depend on them: the bullet claiming
    # emit_max_chars "defaults above what clients accept", the "inside the 100,000 budget"
    # reasoning, and "(25 threads or 100k chars)" in the agent block. The pointed part is
    # that the most likely future change to emit_max_chars is the one this guide's own
    # advice recommends — someone lowers the shipped default and the guide simultaneously
    # claims it defaults above what clients accept, reasons against 100,000, and advises
    # lowering it to a value it already holds, with the suite green.
    assert ReflectConfig().emit_max_chars == 100_000, (
        "reflect.emit_max_chars default moved — reflect-guide.md quotes 100,000 in three "
        "places and reasons about refusals sitting inside it."
    )
    assert ReflectConfig().emit_max_threads == 25, (
        "reflect.emit_max_threads default moved — reflect-guide.md quotes 25 as the slice "
        "size in both the prose and the copy-paste agent block."
    )
    assert ReflectConfig().min_confidence == "medium", (
        "reflect.min_confidence default moved — the copy-paste agent block tells agents "
        "anything marked 'low' is dropped on ingest. If the floor moved to 'low', that "
        "block now tells them to avoid a level that works."
    )

    assert ReflectConfig().model == "", (
        "reflect.model no longer defaults to empty — the guide's table and the trigger both "
        "tell operators that unbound is the shipped posture."
    )

    cfg = _reflect_config(tmp_path, enabled=True, min_obs=1)
    cfg.reflect.model = ""
    cfg.model = "some-model-the-operator-never-chose-for-this"
    session_id, thread = _seed_thread(cfg, n_obs=4)

    # NOT via `generate_fn=` — an injected callable IS a binding by this function's own
    # contract, so passing one bypasses the guard under test. Trip the BUILDER instead:
    # if the fallback to `config.model` came back, this is what it would reach for.
    def _explode(*a, **k):  # pragma: no cover - the point is that it is never reached
        raise AssertionError(
            "an unbound reflect pass built a generate_fn — it fell back to config.model, "
            "which is the #555 defect and makes reflect-guide.md's 'no LLM is called behind "
            "your back' false."
        )

    monkeypatch.setattr("techne.core.reflect._make_generate_fn", _explode)
    observations = [{"tool_name": "Bash", "content": f"obs {i}"} for i in range(4)]
    result = reflect_on_session(cfg, session_id, [thread], observations)

    assert result["candidates_written"] == 0
    assert not result.get("errors"), (
        f"an unbound pass reported errors {result.get('errors')} — the guide calls this a "
        "normal working posture, so it must be a clean no-op, not a failure."
    )


def test_the_mcp_server_pins_config_before_it_serves(tmp_path):
    """The `write_to` warning — "the tool call succeeds and writes to the wrong place, with
    no error" — is true only because config is read ONCE and never re-read while serving.

    PINS THE SENTENCE, NOT THE ARRANGEMENT (Prudence, #792 r1). An earlier version asserted
    only that `Config.load()` was lexically before the first top-level loop in `main` and
    not inside it. That leaves the doc falsifiable while the test stays green: a hot-reload
    added to `handle_request` — a far more natural home for one than the stdin loop — would
    make the tools stop using startup values without tripping a positional check. Counting
    every call site in the module closes that: the claim is that there is exactly ONE, at
    startup, so any second one anywhere fails here regardless of where it lives.

    Also resolves the serve loop by what it iterates rather than by `loops[0]`, so adding a
    warm-up loop ahead of it cannot quietly move the goalposts."""
    import ast

    src = (REPO / "techne" / "mcp_server.py").read_text()
    tree = ast.parse(src)

    def _is_config_load(n):
        return (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "load"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "Config"
        )

    everywhere = [n.lineno for n in ast.walk(tree) if _is_config_load(n)]
    assert len(everywhere) == 1, (
        f"Config.load() appears {len(everywhere)} time(s) in mcp_server.py, at {everywhere}. "
        "reflect-guide.md tells operators that editing config.yaml under a live MCP server "
        "has no effect until restart — that holds only while the server reads config exactly "
        "once. A second call site is a re-read, and the warning becomes wrong."
    )

    main_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    in_main = [n.lineno for n in ast.walk(main_fn) if _is_config_load(n)]
    assert in_main == everywhere, (
        f"the sole Config.load() is not inside main(): module {everywhere}, main {in_main}"
    )

    # The serve loop, identified by what it iterates — `for line in sys.stdin`.
    serve = [
        n
        for n in ast.walk(main_fn)
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.Attribute)
        and n.iter.attr == "stdin"
    ]
    assert serve, "mcp_server.main() no longer iterates sys.stdin — find the new serve loop"
    assert max(in_main) < serve[0].lineno, (
        f"Config.load() at {in_main} is not before the stdin serve loop at {serve[0].lineno}"
    )


def test_the_cli_emit_can_write_the_batch_to_a_file(tmp_path, monkeypatch):
    """The escape hatch the trigger offers when MCP emit would blow the tool-result cap.
    Driven through the real CLI, because the claim is about the shipped command surface —
    `--output/-o` existing AND actually producing a file the agent can read thread by thread.

    Pinning only the option's declared existence would survive a change that accepted the
    flag and still printed to stdout, which is precisely the failure the advice is meant to
    route around."""
    import json

    from click.testing import CliRunner

    from techne.cli import main
    from techne.config import Config

    cfg = _reflect_config(tmp_path, enabled=True, min_obs=1)
    session_id, _ = _seed_thread(cfg, n_obs=4)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))

    out = tmp_path / "batch.json"
    result = CliRunner().invoke(main, ["reflect", "emit", "--session", session_id, "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists(), (
        "`techne reflect emit -o <file>` wrote no file. reflect-guide.md tells agents to use "
        "this instead of the MCP tool for large sessions; without it there is no route that "
        "stays under the tool-result cap."
    )
    batch = json.loads(out.read_text())
    assert isinstance(batch, list) and batch, f"expected a non-empty batch array, got {batch!r}"
    assert "prompt" in batch[0] and "raw_response" in batch[0], (
        f"batch items are missing the fields the documented loop fills in: {sorted(batch[0])}"
    )


def test_emit_is_a_bounded_cursor_not_one_unbounded_pass(tmp_path):
    """THE THIRD NARROW WITNESS (Prudence, #792 r2). The "drain a large session" bullet had
    been rewritten to lead with "emit is a CURSOR: one bounded slice per call", while its
    only witness pinned that `-o` writes a file. She proved the gap by forcing both bounds
    in `emit_reflection_pass` to 10**9 — making emit one unbounded pass, exactly the
    #268/#269 defect the bullet warns about — and this file stayed fully green. The bullet
    is now split so each claim carries its own witness; this is the cursor half.

    NOT solved the way she suggested, and the reason is worth recording twice over:

    1. She proposed pointing a key at `test_emit_cursor_drains_large_session_across_passes`.
       It does hold the claim, but it lives in `tests/test_mcp_reflect.py`, and
       `test_every_named_witness_exists` resolves witnesses with `hasattr(module, ...)`
       against THIS module — naming it there would fail that check.
    2. She also proposed a SECOND key on the same bullet. That would have been decorative:
       `_trigger_blocks` collects only a bullet's FIRST line and `test_every_bullet_names_a_
       witness` is satisfied by `any(key in line ...)`, so a key matching the fifth line
       would never be consulted. Splitting the bullet is what actually makes the accounting
       hold, because this file's unit is one bullet, one claim, one witness.

    Observations are written as two clusters two hours apart, because emit RE-DETECTS
    threads from observations (`load_session_for_reflection`) rather than reading the
    persisted rows — hand-built Thread rows do not control what emit sees, which cost me a
    pass here."""
    from datetime import datetime, timedelta

    from techne.core.reflect import emit_reflection_pass
    from techne.core.state import start_session

    cfg = _reflect_config(tmp_path, min_obs=1)
    cfg.reflect.emit_max_threads = 1
    session_id = start_session(cfg.db_path, project_path="/tmp/p", agent_identity="t")

    base = datetime(2026, 1, 1, 12, 0)
    conn = connect_db(cfg.db_path)
    try:
        for cluster in range(2):
            for i in range(4):
                ts = base + timedelta(hours=2 * cluster, minutes=i)
                conn.execute(
                    "INSERT INTO observations (session_id, timestamp, tool_name, content) "
                    "VALUES (?,?,?,?)",
                    (session_id, ts.isoformat(), "Edit", f"/proj/c{cluster}.py step {i}"),
                )
        conn.commit()
    finally:
        conn.close()

    # Persist the detection, so batch items carry a real `thread_id`. Without this
    # `persisted_thread_id` returns None for every item and the "did the cursor advance"
    # comparison degenerates to {None} == {None} — a test that fails for the wrong reason.
    from techne.lifecycle import run_session_end

    run_session_end(cfg, session_id)

    first = emit_reflection_pass(cfg, session_id)
    assert len(first["batch"]) == 1, (
        f"emit returned {len(first['batch'])} items against emit_max_threads=1 — it is not "
        "bounding the slice, so the bullet's 'one bounded slice per call' is false and "
        "'looping until empty' is advice for a problem that does not exist."
    )
    assert first.get("truncated"), (
        "a bounded emit that left a thread behind did not report `truncated` — the operator "
        "has no signal to go again."
    )

    second = emit_reflection_pass(cfg, session_id)
    # ASSERT DELIVERY BEFORE ASSERTING NON-REPETITION (Prudence, #792 r3). `repeats` is
    # empty both when the cursor advanced correctly AND when the second pass returned
    # nothing at all, so on its own it certifies a cursor that drops the leftover thread —
    # under which "loop until the batch comes back empty" terminates immediately and looks
    # successful with the session half-drained. She demonstrated it: collapse `newly` to
    # every current id and derive `truncated` independently, and the old assertions passed
    # green with thread 2 permanently lost. They only held before because `truncated`
    # happens to be derived from the same `emitted_ids` set the cursor persists, which is
    # an implementation coupling rather than the claim.
    assert len(second["batch"]) == 1, (
        "the second pass delivered nothing — the leftover thread was lost, so 'loop until "
        "the batch comes back empty' terminates with the session half-drained."
    )
    repeats = {i["thread_id"] for i in first["batch"]} & {
        i["thread_id"] for i in second["batch"]
    }
    assert not repeats, (
        f"the second pass re-delivered {repeats} — the cursor did not advance, so the "
        "documented loop would never terminate."
    )


def test_the_copy_paste_block_teaches_both_populations():
    """THE BLOCK BELOW THE TRIGGERS IS UNPROTECTED, AND IT IS THE ONE THAT TRAVELS
    (Prudence, #804).

    `TRIGGER_WITNESSES` keys on phrases inside the `Agent: raise this unprompted` block, so
    the "For your Claude — copy this into your agent's context" block underneath it is
    covered by nothing. She reverted `docs/reflect-guide.md` wholesale to base — restoring
    the pre-#793 single-population rule, "never drain or delete them" — and all 39 tests in
    this file passed.

    That block is the one artifact in the document DESIGNED TO TRAVEL: it goes into an
    operator's CLAUDE.md and is then read in isolation, away from the trigger table, and the
    line after it calls it "everything an agent needs". An agent carrying only the reverted
    version would tell an operator that provably-superseded rows must stay — which is
    techne#646's confusion reproduced in the most portable component of the fix for it."""
    guide = (REPO / "docs" / "reflect-guide.md").read_text()
    marker = "For your Claude — copy this into your agent's context"
    assert marker in guide, "the copy-paste block heading moved; re-anchor this test"
    # BOUNDED AT THE NEXT SECTION (Prudence, #804 r2 §4b). Running to EOF swallowed
    # `## Where to go next`, so the pin would silently start passing on tokens from a
    # section that has nothing to do with the copy-paste block. Honest today only
    # because that section happens to mention neither word.
    start = guide.index(marker)
    rest = guide[start:]
    nxt = rest.find("\n## ", 1)
    block = rest[:nxt] if nxt != -1 else rest

    assert "reconcile" in block, (
        "the copy-paste block no longer mentions `techne reflect reconcile`. It is the "
        "sanctioned remedy for superseded rows, and without it an agent carrying this block "
        "tells the operator that undrainable rows must all stay."
    )
    assert "superseded" in block, (
        "the copy-paste block no longer distinguishes the superseded population from the "
        "below-threshold one — the distinction techne#793 established and #646's original "
        "single-population framing got wrong."
    )


def test_the_dedup_step_the_url_trigger_names_exists():
    """The URL triggers all route to a procedure whose FIRST step is `architect dedup`.
    If that command is gone or renamed, three triggers send an agent at nothing — and a
    trigger that points at a missing command is worse than none, because it reads as
    covered. Pins the CLI surface, not the prose."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.docs import bundled_guide

    res = CliRunner().invoke(main, ["architect", "dedup", "--help"])
    assert res.exit_code == 0, res.output

    guide = bundled_guide("investigating")
    assert "techne architect dedup" in guide
    assert "techne architect snapshot" in guide, "the sandbox backstop is unnamed"


def test_the_survey_guide_teaches_the_same_sandbox():
    """A survey fans out MORE untrusted input than an investigation, not less, so the
    weaker-because-there-are-more-agents mistake is the one to guard. The survey guide must
    keep pointing at the investigating guide's security model rather than restating a
    softened version of it."""
    from techne.docs import bundled_guide

    survey = bundled_guide("surveying")
    assert "web-only" in survey
    assert "snapshot" in survey
    assert "investigating guide" in survey, "the survey guide no longer defers on security"


def test_the_sandbox_the_trigger_names_is_installable():
    """The trigger tells an agent to check `techne agents status` before investigating.
    If that command or the definition behind it goes away, the trigger sends the agent at
    nothing — and a security trigger that points at a missing control is worse than none,
    because it reads as covered."""
    from click.testing import CliRunner

    from techne.cli import main
    from techne.platforms.claude_code.agents import REQUIRED_TOOLS_LINE, bundled_agent_text

    assert CliRunner().invoke(main, ["agents", "status", "--help"]).exit_code == 0
    assert REQUIRED_TOOLS_LINE in bundled_agent_text("research-readonly")


# ------------------------------------------------------- techne#814/#842: upgrading

def test_only_init_writes_the_backfill_segment(tmp_path, monkeypatch):
    """WITNESS for the upgrade trigger's first bullet.

    The claim is that the SessionStart chain an install runs is whatever `init`
    last wrote, so an upgrade alone leaves the old chain — which is why a stale
    install never gains the backfill segment on its own.

    DRIVES `register_hooks` AND READS THE EMITTED COMMAND (#843 r1 Important 1).
    The first version grepped `hooks.py` for the substring "vectors backfill",
    which also appears in `_backfill_command`'s own DOCSTRING — so it passed
    whenever the helper merely existed, regardless of whether the chain carried
    it. Review reproduced the historical v0.16.0 failure (the segment removed from
    the assembly, helper and docstring untouched) and the whole trigger suite
    stayed green: a witness that could not fail for the one bug this release
    exists to fix.
    """
    import json

    from techne.config import Config, HookConfig, SyncConfig
    from techne.platforms import claude_code
    from techne.platforms.claude_code import hooks as hooks_mod

    home = tmp_path / "home"
    home.mkdir()
    settings_path = home / ".claude" / "settings.json"
    monkeypatch.setattr(hooks_mod, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(hooks_mod, "MCP_CONFIG_PATH", home / ".claude.json")
    monkeypatch.setattr(claude_code, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(claude_code, "MCP_CONFIG_PATH", home / ".claude.json")

    cfg = Config()
    cfg.db_path = tmp_path / "techne.db"
    cfg.data_dir = tmp_path
    cfg.platform = "claude-code"
    cfg.hooks = HookConfig(post_tool_use=True)
    cfg.sync = SyncConfig(adapter="noop")

    claude_code.register_hooks(cfg)
    groups = json.loads(settings_path.read_text())["hooks"]["SessionStart"]
    command = groups[0]["hooks"][0]["command"]

    assert "vectors backfill" in command, (
        "`techne init` no longer writes the backfill segment into the SessionStart "
        "chain — an install cannot gain it by running init, and "
        "docs/deployment.md tells operators it can"
    )


def test_the_hook_runs_backfill_no_download_and_the_cli_does_not():
    """WITNESS for the upgrade trigger's second bullet.

    Two halves, and the trigger is only true if both hold: the HOOK passes
    `--no-download` (so a cold model cache degrades to "embedded nothing" instead of
    blocking session start), and the bare CLI does NOT (so one manual run is what
    warms the cache). If the CLI ever gains the flag by default, the trigger's advice
    becomes wrong — the manual run would no-op too, and there would be no way left to
    prime it.
    """
    import subprocess
    import sys

    src = (REPO / "techne/platforms/claude_code/hooks.py").read_text()
    assert "vectors backfill --no-download" in src, (
        "the SessionStart hook no longer passes --no-download; a cold cache can now "
        "block session start on a model fetch"
    )

    out = subprocess.run(
        [sys.executable, "-m", "techne.cli", "vectors", "backfill", "--help"],
        capture_output=True, text=True, cwd=REPO,
    ).stdout
    assert "--no-download" in out, "the CLI flag vanished; the hook's invocation is stale"
    assert "--no-download" not in out.split("Options:")[0], "sanity: parsed the wrong section"
