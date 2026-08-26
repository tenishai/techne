"""The agent-trigger convention: indexed so it fires, tested so it does not rot (#756/#757).

techne's failures are overwhelmingly silent, which is the class a human never thinks to ask
about — so parts of the docs are written as TRIGGERS: conditions under which an agent should
speak up unprompted. Two things were missing when the convention shipped in #755, both found
by Prudence reviewing it:

  #756  A trigger that lives only in a document fires only if that document was already
        open — the failure the documents were meant to fix, one level up. It needs an index
        on an always-loaded surface. `AGENTS.md` is that surface, and this file enforces the
        index BOTH DIRECTIONS, the same rule `check_arch_cards.py` applies to cards.

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

    WHAT THIS DOES NOT COVER, said rather than implied: the `INDEX IS EMPTY (0 files)`
    branch needs a db that RESOLVES while holding nothing, and I could not construct that
    state from the envelope in two attempts — creating the index db alone still answers
    `absent`. So this pins the reachable state and the doc claims only the qualification
    both states share. The empty-index branch is covered by codegraph's own tests, not by
    this one."""
    from techne.mcp_server import _codex_search

    repo = tmp_path / "unindexed-repo"
    repo.mkdir()

    envelope = _codex_search({"query": "zqxjkv_no_such_symbol", "repo": str(repo)},
                             config_free_tmp_home)

    assert envelope["results"] == []
    assert envelope.get("notice"), (
        "the codex returned a bare empty result — the trigger names it as the one domain "
        "that qualifies its nothing, and four-functions.md tells operators so"
    )
    assert "codex index" in envelope["notice"], (
        f"the notice no longer names the remedy: {envelope['notice']!r}"
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
