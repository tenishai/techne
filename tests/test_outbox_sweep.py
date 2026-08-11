"""Outbox retention (#518) + prune-orphans registration (#484).

THE INVARIANT THIS FILE EXISTS TO PROTECT: the delivered rows on a ledger lane
ARE the dedup pre-filter's state (``core/archivist.py`` reads them back), so a
retention policy that ages them out makes the next teardown re-enqueue the whole
knowledge base. That is not hypothetical — a hand-run sweep of 128,200 rows on
2026-08-07 finished at 20:13 and 761 distinct rows re-enqueued at 20:16:01-06.

``test_sweep_preserves_the_prefilter_set`` is the regression guard for exactly
that, and it fails against an age-only sweep. Teeth-checked, not assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from techne.core.archivist import (
    ARCHIVIST_KNOWLEDGE_TARGET,
    LEDGER_TARGETS,
    push_knowledge_to_archivist,
    register_base_archivist,
)
from techne.sync.outbox import enqueue, sweep_delivered, sweep_preview
from techne.sync.sinks import use_registry

NON_LEDGER = "sink:archivist"


@pytest.fixture()
def config(tmp_path, monkeypatch):
    """A non-standalone store with both archivist sinks registered.

    `enqueue` refuses an unregistered sink: target, so registration is a
    precondition of every test here rather than part of what they assert.
    """
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config
    from techne.core.database import init_db

    cfg = Config.load()
    cfg.standalone = False
    cfg.archivist_db_path = str(tmp_path / "shared-archivist.db")
    init_db(cfg.db_path)
    with use_registry({}):
        register_base_archivist(cfg)
        yield cfg


def _kp(label):
    """A valid KnowledgePayload — enqueue validates against the sink's schema."""
    return {"content": label, "type": "fact", "tags": "", "confidence": "high"}


def _tp(label):
    """A valid ThreadSummaryPayload for the non-ledger lane."""
    return {
        "session_id": "s-1",
        "thread_id": label,
        "thread_label": label,
        "summary": label,
        "resume_points": "",
        "agent_identity": "agentA",
    }


def _rows(config, target=None):
    from techne.core.stores import plain_connect

    conn = plain_connect(config.db_path)
    try:
        if target is None:
            return conn.execute("SELECT id, target, status, content_hash FROM outbox").fetchall()
        return conn.execute(
            "SELECT id, target, status, content_hash FROM outbox WHERE target = ?", (target,)
        ).fetchall()
    finally:
        conn.close()


def _set_all(config, **cols):
    """Force status/created_at on every row — the sweep keys off both."""
    from techne.core.stores import plain_connect

    assignments = ", ".join(f"{k} = ?" for k in cols)
    conn = plain_connect(config.db_path)
    try:
        conn.execute(f"UPDATE outbox SET {assignments}", tuple(cols.values()))
        conn.commit()
    finally:
        conn.close()


# ---- the regression guard ---------------------------------------------------


def test_sweep_preserves_the_prefilter_set(config):
    """After a sweep, a re-push must still enqueue NOTHING.

    This is the whole point of #518's design. An age-based sweep of the ledger
    lane passes every other test in this file and fails this one, because the
    pre-filter's set is gone and every fact looks new again.
    """
    from techne.core.knowledge import store

    for i in range(5):
        store(config.db_path, f"durable fact number {i}", type="fact", confidence="high")

    assert push_knowledge_to_archivist(config) == 5

    # Delivered and old — the state an age-based sweep would delete outright.
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")
    sweep_delivered(config.db_path, retain_days=7)

    assert push_knowledge_to_archivist(config) == 0, (
        "the sweep destroyed the dedup ledger — a re-push re-enqueued the "
        "knowledge base, which is the bug #518 exists to prevent"
    )


def test_collapse_keeps_exactly_one_row_per_hash(config):
    for _ in range(4):
        enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("same"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("other"), content_hash="h2")
    _set_all(config, status="synced")

    result = sweep_delivered(config.db_path)

    assert result["collapsed"] == 3
    hashes = sorted(r[3] for r in _rows(config, ARCHIVIST_KNOWLEDGE_TARGET))
    assert hashes == ["h1", "h2"]


def test_collapse_prefers_a_non_dead_survivor(config):
    """The pre-filter's query excludes dead rows.

    Keeping the NEWEST row when it happens to be dead would drop that hash from
    the set the pre-filter can see — reintroducing the re-enqueue bug via the fix
    for it. Ordering must prefer a live row over a newer dead one.
    """
    from techne.core.stores import plain_connect

    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    conn = plain_connect(config.db_path)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM outbox ORDER BY id").fetchall()]
        conn.execute("UPDATE outbox SET status = 'synced' WHERE id = ?", (ids[0],))
        conn.execute("UPDATE outbox SET status = 'dead' WHERE id = ?", (ids[1],))
        conn.commit()
    finally:
        conn.close()

    sweep_delivered(config.db_path)

    survivors = _rows(config, ARCHIVIST_KNOWLEDGE_TARGET)
    assert len(survivors) == 1
    assert survivors[0][2] == "synced", (
        "collapse kept the dead row; the pre-filter excludes dead, so that hash would re-enqueue"
    )


# ---- what the sweep must never touch ---------------------------------------


def test_sweep_never_deletes_pending_rows(config):
    """Pending rows are undelivered work; sweeping one loses it silently.

    Note the DISTINCT hashes: `idx_outbox_dedup` is UNIQUE on content_hash
    WHERE status = 'pending', so duplicate pending rows cannot exist by
    construction. Only delivered rows accumulate — which is precisely why the
    collapse policy has anything to collapse.
    """
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a2"), content_hash="h2")
    enqueue(config.db_path, NON_LEDGER, _tp("b"), content_hash="h9")
    _set_all(config, status="pending", created_at="2020-01-01 00:00:00")

    result = sweep_delivered(config.db_path, retain_days=0)

    assert result == {"collapsed": 0, "aged_out": 0}
    assert len(_rows(config)) == 3, "a pending row was swept before it was ever delivered"


def test_collapse_never_deletes_a_pending_row_sharing_a_hash(config):
    """The collapse partitions by (target, content_hash) ACROSS statuses.

    A pending row that shares a hash with a NEWER delivered row loses the
    `id DESC` tiebreak, so without the `status != 'pending'` filter it would be
    collapsed away — silently discarding work that was never delivered. The
    partial unique index only bars two PENDING rows sharing a hash; it says
    nothing about pending-vs-synced.

    Written because mutation-testing found the guard uncovered: deleting the
    filter left every other test in this file green.
    """
    from techne.core.stores import plain_connect

    # Built directly rather than via enqueue(): with a sink registered, enqueue
    # attempts immediate delivery, so the row would already be 'synced' and the
    # state under test could never be reached.
    conn = plain_connect(config.db_path)
    try:
        # Older row is PENDING; a NEWER row carries the same hash, delivered.
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?,?,?,?)",
            (ARCHIVIST_KNOWLEDGE_TARGET, "{}", "h1", "pending"),
        )
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?,?,?,?)",
            (ARCHIVIST_KNOWLEDGE_TARGET, "{}", "h1", "synced"),
        )
        conn.commit()
        # Precondition: assert the state actually exists before exercising the
        # code, so a setup that silently did nothing fails loudly (techne#912).
        assert [
            r[0]
            for r in conn.execute("SELECT status FROM outbox WHERE content_hash = 'h1' ORDER BY id")
        ] == ["pending", "synced"]
    finally:
        conn.close()

    sweep_delivered(config.db_path)

    statuses = sorted(r[2] for r in _rows(config, ARCHIVIST_KNOWLEDGE_TARGET))
    assert "pending" in statuses, (
        "the collapse deleted an undelivered row that shared a hash with a newer "
        "delivered one — that work is now lost"
    )


def test_sweep_keeps_dead_rows_on_non_ledger_lanes(config):
    """`list_dead` reads these — they are diagnostic, not debris."""
    enqueue(config.db_path, NON_LEDGER, _tp("b"), content_hash="h9")
    _set_all(config, status="dead", created_at="2020-01-01 00:00:00")

    sweep_delivered(config.db_path, retain_days=0)

    assert len(_rows(config, NON_LEDGER)) == 1


# ---- the age policy on the other lane ---------------------------------------


def test_non_ledger_lane_ages_out(config):
    enqueue(config.db_path, NON_LEDGER, _tp("old"), content_hash="h1")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")
    enqueue(config.db_path, NON_LEDGER, _tp("new"), content_hash="h2")

    result = sweep_delivered(config.db_path, cutoff="2021-01-01 00:00:00")

    assert result["aged_out"] == 1
    assert len(_rows(config, NON_LEDGER)) == 1


def test_non_ledger_lane_is_not_collapsed_by_hash(config):
    """Unique events, not repeats of a bounded set.

    Collapsing here would silently drop distinct deliveries that merely share a
    payload, and reclaims ~21% against age's ~99% anyway.
    """
    for _ in range(3):
        enqueue(config.db_path, NON_LEDGER, _tp("same"), content_hash="dup")
    _set_all(config, status="synced")

    result = sweep_delivered(config.db_path, cutoff="2000-01-01 00:00:00")

    assert result["collapsed"] == 0
    assert len(_rows(config, NON_LEDGER)) == 3


# ---- the dry run has to describe the real thing -----------------------------


def test_preview_matches_apply_exactly(config):
    for _ in range(3):
        enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, NON_LEDGER, _tp("b"), content_hash="h9")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")

    preview = sweep_preview(config.db_path, retain_days=0)
    before = len(_rows(config))
    applied = sweep_delivered(config.db_path, retain_days=0)
    after = len(_rows(config))

    assert (preview["collapsed"], preview["aged_out"]) == (
        applied["collapsed"],
        applied["aged_out"],
    )
    assert preview["retained"] == after
    assert before - after == applied["collapsed"] + applied["aged_out"]


def test_preview_does_not_mutate(config):
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    enqueue(config.db_path, ARCHIVIST_KNOWLEDGE_TARGET, _kp("a"), content_hash="h1")
    _set_all(config, status="synced")

    sweep_preview(config.db_path, retain_days=0)

    assert len(_rows(config)) == 2


# ---- the drift guard --------------------------------------------------------


def test_reading_enqueue_history_requires_a_declared_lane():
    """THE INVARIANT, ENFORCED BY THE CALL RATHER THAN BY A REGEX.

    Reading enqueue history back makes those rows load-bearing state, so the
    sweep must not age them out. Two rounds tried to hold that with a lexical
    guard over the source tree and it could not:

      * BLIND TO INDIRECTION — a consumer reaching the table THROUGH
        sync/outbox.py was invisible, and since every other outbox query in this
        repo lives there, that is the idiomatic way to write one.
      * FALSE POSITIVES — `core/state.py` has a `COUNT(*) FROM outbox` stat with
        no target and no dedup semantics. It was spared only because a `;` fell
        between the tokens; the guard's precision rested on semicolon placement
        in unrelated files.

    `delivered_hashes` is now the one way to read that history, and it refuses an
    undeclared lane. A new pre-filter is either declared or it raises on its
    first call — no spelling evades that.
    """
    from techne.sync.outbox import delivered_hashes

    with pytest.raises(ValueError, match="LEDGER_TARGETS"):
        delivered_hashes(Path("/nonexistent.db"), "sink:undeclared_lane")


def test_no_undeclared_inlined_prefilter():
    """A DETECTOR WITH KNOWN GAPS (not exhaustive — see below).

    THE HEADING NO LONGER CARRIES A NUMBER, and that is the finding, not a
    formatting preference. It read "ONE NAMED GAP", then "THREE NAMED GAPS", and
    the count has now been proven wrong in three consecutive rounds: r5 found
    unnamed RECALL regressions under it, r6 found two unnamed PRECISION false
    positives under it, and r7 found that r6's own precision fix opened a fourth.
    The mechanism survived every round; only the count kept failing. A number in
    a heading is a completeness claim about a lexical net over a language that
    can always express the same query one more way — so the list grows and the
    heading stops promising it is finished (#528 r7, asked for in r5 and r6 too).

    `delivered_hashes` enforces the strong half (call it, declare your lane).
    This catches the INLINED form by looking for the content_hash / outbox /
    target triple INSIDE A SINGLE SQL STATEMENT — string literals walked from
    the AST, whitespace collapsed, then split on `;` so a statement is the unit
    rather than a whole literal.

    WHY PER-LITERAL, after three rounds of regex failed on this:
      * `[^;]*?` under re.S spans hundreds of unrelated lines, so a file-wide
        `target` conjunct made `core/state.py` — which has a queue-depth COUNT
        and a redaction count on `observations`, and reads no enqueue history at
        all — a FALSE POSITIVE the moment an unrelated observations stat was
        added. What spared it was `;` characters in unrelated comments. Precision
        resting on semicolon placement is the exact flaw r3's guard was retired
        for, restored by its replacement.
      * The same span made a prose semicolon between the two tokens a working
        evasion.
    Bounding the match to one statement removes both: no cross-statement span,
    so no semicolon luck in either direction.

    AND THE FIRST CUT OF THAT FIX NARROWED TWO THINGS AT ONCE, ONE OF THEM
    UNMEASURED (r5 review, finding 1 — the same shape this docstring exists to
    stop). Moving to per-literal ALSO turned `\\bFROM\\s+outbox\\b` into the
    substring `"from outbox"` and skipped a literal wholesale on `create table`.
    Both quietly lost coverage the regex had: `FROM  outbox` (aligned SQL) and a
    table name on the next line walked past, and a DDL+DML blob — `executescript`
    is used in ~15 places here — switched the detector off for every statement
    beside the schema. Measured against the retired regex, three probes regressed.
    Hence the whitespace collapse and the `;` split above: a change of altitude
    needs the same before-and-after as anything else.

    THE GAPS KNOWN TODAY, re-measured after the fix above rather than re-asserted.
    Ordered by how likely they are to be hit here, not by how clever they are:
      1. INDIRECTION — a consumer calling a helper in sync/outbox.py. This is why
         the accessor exists.
      2. SPLIT OR COMPOSED SQL — `_TABLE = "outbox"` with an f-string, or
         `cols = "content_hash"` assembled later. The tokens never share a
         literal. Not exotic: this module builds its own DELETEs with
         `_SWEEP_COLLAPSE.format(...)`. Lexically out of reach, and saying so is
         the honest option.
      3. OUTSIDE THE PACKAGE — the scan is rooted at the techne package, so
         `scripts/` is invisible. Nothing pre-filter-shaped lives there today.
      4. A DOCSTRING READ BACK AND EXECUTED — a function whose docstring IS the
         query (`SELECT content_hash FROM outbox WHERE target = ?`), run via
         `fn.__doc__`.
         The precision fix skips strings in docstring POSITION, and Python does
         not distinguish prose from a string a function later executes. Verified
         as a WORKING pre-filter, not a lexical curiosity: probe built a table,
         enqueued a row, and got the hash back (#528 r7).
         DELIBERATELY NOT CHASED. `.__doc__`-as-SQL has zero precedent in ~40
         modules here, where gap 2 is idiomatic — and more AST cleverness is the
         unwinnable game three earlier rounds already played on this detector.
         Named so it is a known limit rather than a surprise.
    """
    from pathlib import Path

    import techne

    root = Path(techne.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "outbox.py" and path.parent.name == "sync":
            continue  # the module that OWNS the table, not a consumer
        try:
            # ONE DEFINITION, shared with the precision test below — see
            # _inlines_prefilter. This loop owns WHICH FILES to scan; it does not
            # own what counts as an offence.
            if _inlines_prefilter(path.read_text()):
                offenders.append(str(path.relative_to(root)))
        except SyntaxError:  # pragma: no cover - not our file to fix
            continue

    assert offenders == [], (
        f"these modules inline a dedup-shaped outbox query instead of calling "
        f"delivered_hashes: {sorted(offenders)}. Route the read through the "
        f"accessor so the lane is declared. If the match is NOT reading enqueue "
        f"history back (a plain COUNT or a status display is not), narrow this "
        f"assertion — do NOT add the target to LEDGER_TARGETS, which would switch "
        f"a non-ledger lane from age-out to collapse-only, i.e. the unbounded "
        f"growth #518 exists to fix."
    )


def test_the_second_tombstone_keeps_the_NEWEST_row_when_synced_at_ties(config):
    """Two rows synced in the SAME SECOND must not protect the older one.

    #528 r7, and the bug only became reachable when #531 landed. The clause was
    `GROUP BY target HAVING synced_at = MAX(synced_at)`, which is ambiguous on a
    tie — SQLite returns an arbitrary row's id, and it returned the OLDER one, so
    the sweep protected the row it should delete and left the newest unprotected.

    IT PASSED FOR MONTHS ON PRECISION IT NEVER DECLARED. `synced_at` was written
    with `.isoformat()`, whose microseconds made ties practically impossible.
    #531 normalised every on-disk timestamp to second precision and the tie became
    routine — a drain stamps a whole batch with one `now`, so same-second pairs are
    the normal case, not a corner.

    Ties are constructed directly here rather than by racing two drains: the point
    is the SQL's behaviour on equal values, and a timing-dependent test of a
    timing-dependent bug pins nothing.
    """
    from techne.core.stores import plain_connect
    from techne.sync.outbox import sweep_delivered

    same = "2026-01-01 00:00:00"
    conn = plain_connect(config.db_path)
    for hid, created in (("h-older", "2020-01-01 00:00:00"), ("h-newer", "2020-01-02 00:00:00")):
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status, created_at, synced_at) "
            "VALUES (?, '{}', ?, 'synced', ?, ?)",
            (NON_LEDGER, hid, created, same),
        )
    conn.commit()
    conn.close()

    sweep_delivered(config.db_path, cutoff="2021-01-01 00:00:00")

    conn = plain_connect(config.db_path)
    survivors = {r[0] for r in conn.execute(
        "SELECT content_hash FROM outbox WHERE target = ?", (NON_LEDGER,)
    )}
    conn.close()
    assert survivors == {"h-newer"}, (
        "on a synced_at tie the sweep must keep the row with the greater id — the "
        "NEWEST. Keeping the older one both destroys the lane's freshest sync "
        f"evidence and leaves the stale row behind. Survivors: {survivors}"
    )


def test_the_first_tombstone_survives_an_out_of_band_write(config):
    """The `id NOT IN (SELECT MAX(id) ...)` clause has teeth — against the state
    that actually motivates it.

    #528 r6 showed the clause was untested (mutating it to `1=1` left the whole
    suite green) AND that its stated justification was unreachable: no code in
    this package can leave a `synced` row with a NULL synced_at, because every
    `status='synced'` assignment sets both together.

    That sharpened the finding rather than dissolving it. The clause is worth
    keeping for OUT-OF-BAND writes — a manual UPDATE, a future migration, a
    third-party tool against what is, after all, a plain SQLite file — so this
    test creates exactly that state directly instead of through the API, which is
    the only way the defended state can arise.

    Without the clause, the lane's last surviving evidence of a sync is deleted
    and check_outbox_targets reads MAX(synced_at) as NULL: a healthy lane reports
    never-synced.
    """
    from techne.core.database import connect
    from techne.sync.outbox import sweep_delivered

    conn = connect(config.db_path)
    # OUT-OF-BAND: status synced, synced_at NULL — unreachable through the API,
    # which is the point. Old enough that the age sweep wants it.
    conn.execute(
        "INSERT INTO outbox (target, data_json, content_hash, status, created_at, synced_at) "
        "VALUES ('sink:probe', '{}', 'h-old', 'synced', '2020-01-01 00:00:00', NULL)"
    )
    conn.commit()
    before = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE target = 'sink:probe'"
    ).fetchone()[0]
    conn.close()
    assert before == 1, "precondition: the lane has exactly one row"

    sweep_delivered(config.db_path, retain_days=7)

    conn = connect(config.db_path)
    after = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE target = 'sink:probe'"
    ).fetchone()[0]
    conn.close()
    assert after == 1, (
        "the age sweep deleted a lane's ONLY row. The first tombstone exists to "
        "keep one row per target so COUNT/MAX stay honest; without it this lane "
        "now reads as never-synced. Reachable only via an out-of-band write, "
        "which is exactly what the clause defends against."
    )


def _inlines_prefilter(source: str) -> bool:
    """THE ONE DEFINITION of "this file inlines a dedup-shaped outbox query".

    Module-level so the guard and its precision test share it. An earlier draft
    had the precision test re-implement these rules, with a docstring claiming it
    did not — a second definition is exactly what drifts, and this file's whole
    subject is a detector whose behaviour nobody could pin down (#528 r6).

    PRECISION, both axes measured against real false positives:
      - DOCSTRINGS ARE PROSE. A docstring quoting the pre-filter it describes
        executes nothing. Collected structurally (first statement of a module,
        class or function) rather than guessed at.
      - AN AGGREGATE IS A METRIC. `COUNT(DISTINCT content_hash)` answers "how
        many" and skips nothing; the guard is only about reading hashes BACK to
        decide what to re-send. checks.py carries a legitimate one already.
    """
    import ast
    import re

    tree = ast.parse(source)
    docstrings = set()
    for holder in ast.walk(tree):
        if isinstance(
            holder, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = holder.body[0] if holder.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        # COLLAPSE WHITESPACE FIRST (#518 r5 finding 1, preserved through the
        # #528 r6 rebase). `FROM  outbox` and a table name on its own line are
        # ordinary formatting, not evasions, and a substring test is
        # whitespace-exact where the regex it replaced was not.
        sql = " ".join(node.value.lower().split())
        # SPLIT ON `;` — a literal is not a statement. executescript is used in
        # ~15 places here, so DDL and DML sharing one blob is in-idiom; skipping
        # the WHOLE literal on `create table` switched the detector off for every
        # other statement in it.
        for stmt in sql.split(";"):
            if "create table" in stmt:
                continue  # a schema blob is DDL, not a pre-filter
            # AN AGGREGATE IS A METRIC, NOT A PRE-FILTER (#528 r6).
            aggregated = re.search(
                r"\b(count|sum|avg|min|max)\s*\(\s*(distinct\s+)?content_hash", stmt
            )
            reads_hashes_back = re.search(
                r"select\s+(distinct\s+)?content_hash\b", stmt
            ) or re.search(r"content_hash\s*(=|in\b)", stmt)
            if aggregated and not reads_hashes_back:
                continue
            if (
                "content_hash" in stmt
                and "from outbox" in stmt
                and "target" in stmt
                and "select" in stmt
            ):
                return True
    return False


def test_the_inlined_prefilter_guard_does_not_fire_on_prose_or_metrics(tmp_path):
    """The guard's PRECISION, pinned — it fired on innocent code for three rounds.

    Both cases below were demonstrated against the shipped guard by #528 r6, using
    throwaway modules dropped into the package. They are checked in here instead,
    because a false positive that only lives in a review comment comes back: the
    assertion's own message has told readers to narrow it in exactly these cases
    since it was written, and nobody did.

    Why it matters beyond tidiness: a guard that cries wolf on a docstring gets
    narrowed by whoever it blocks next, in a hurry, by someone who does not know
    which of its four clauses is load-bearing. Precision IS part of a guard
    working, not a nicety on top of it.
    """
    prose = tmp_path / "_probe_prose.py"
    prose.write_text(
        '"""Historical note: the pre-filter used to inline its own query:\n\n'
        "    SELECT content_hash FROM outbox WHERE target = ? AND status != 'dead'\n\n"
        'That is narration. Nothing here executes SQL at all.\n"""\n'
    )
    metric = tmp_path / "_probe_metric.py"
    metric.write_text(
        "def distinct_hash_count(conn, target):\n"
        "    return conn.execute(\n"
        '        "SELECT COUNT(DISTINCT content_hash) FROM outbox WHERE target = ?",\n'
        "        (target,),\n"
        "    ).fetchone()[0]\n"
    )

    def offends(path):
        return _inlines_prefilter(path.read_text())

    assert not offends(prose), (
        "a docstring QUOTING the pre-filter tripped the guard. Prose executes "
        "nothing; this repo's style is narrative docstrings that quote the code "
        "they describe, so this fires hardest on the best-documented files."
    )
    assert not offends(metric), (
        "COUNT(DISTINCT content_hash) tripped the guard. An aggregate answers "
        "'how many' and skips nothing — it is not reading enqueue history back to "
        "decide what to re-send, which is the only thing this guard is about."
    )

    # AND THE GUARD MUST STILL BITE — precision that costs recall is a worse bug
    # than the false positive it fixed. Both real spellings, forward and reverse.
    real_fwd = tmp_path / "_probe_real_fwd.py"
    real_fwd.write_text(
        "def prefilter(conn, target):\n"
        '    return conn.execute("SELECT content_hash FROM outbox WHERE target = ?", (target,))\n'
    )
    real_rev = tmp_path / "_probe_real_rev.py"
    real_rev.write_text(
        "def seen(conn, target, h):\n"
        '    return conn.execute(\n'
        '        "SELECT 1 FROM outbox WHERE target = ? AND content_hash = ?",\n'
        '        (target, h),\n    )\n'
    )
    assert offends(real_fwd), "the guard stopped catching the forward pre-filter spelling"
    assert offends(real_rev), "the guard stopped catching the reverse pre-filter spelling"



def test_the_prefilter_goes_through_the_accessor(config):
    """The accessor is only a mechanism if the real pre-filter uses it.

    An accessor nobody calls is the shape #504 names — present, correct, and not
    load-bearing. Asserted by observing the CALL, so it fails if the archivist
    ever re-inlines the SQL.
    """
    from techne.core.knowledge import store
    from techne.sync import outbox as outbox_mod

    # Patched on the SOURCE module: archivist imports the name inside the
    # function, so it is resolved at call time and is not an attribute of
    # archivist itself.
    calls = []
    real = outbox_mod.delivered_hashes

    def spy(db_path, target):
        calls.append(target)
        return real(db_path, target)

    outbox_mod.delivered_hashes = spy
    try:
        store(config.db_path, "a durable fact", type="fact", confidence="high")
        push_knowledge_to_archivist(config)
    finally:
        outbox_mod.delivered_hashes = real

    assert calls == [ARCHIVIST_KNOWLEDGE_TARGET], (
        f"push_knowledge_to_archivist did not read history through the accessor: {calls}"
    )


# ---- #484: prune-orphans must register sinks before judging orphanhood -------


def test_prune_orphans_leaves_a_registered_lane_alone(tmp_path, monkeypatch):
    """A registered lane must not present as unregistered in a fresh process.

    Sinks are PER-PROCESS state. The CLI builds a new process with an empty
    registry, so without `ensure_registered_for` every sink:archivist_knowledge
    row looks like a row for an unknown target and the whole pending backlog is
    marked dead. The suite had nothing covering the REGISTERED case, which is why
    this survived.

    Invoked through the CLI on purpose: the defect lives in the command's wiring,
    not in `prune_orphans`, so a unit test that pre-registers would pass against
    the broken code.
    """
    from click.testing import CliRunner

    from techne.cli import main
    from techne.core.database import init_db
    from techne.core.stores import plain_connect

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    cfg = Config.load()
    cfg.standalone = False
    cfg.archivist_db_path = str(tmp_path / "shared-archivist.db")
    cfg.save(tmp_path / "config.yaml")
    init_db(cfg.db_path)

    conn = plain_connect(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status) VALUES (?,?,?,?)",
            (ARCHIVIST_KNOWLEDGE_TARGET, "{}", "h1", "pending"),
        )
        conn.commit()
    finally:
        conn.close()

    result = CliRunner().invoke(main, ["outbox", "prune-orphans"])
    assert result.exit_code == 0, result.output

    conn = plain_connect(cfg.db_path)
    try:
        status = conn.execute("SELECT status FROM outbox WHERE content_hash = 'h1'").fetchone()[0]
    finally:
        conn.close()

    assert status == "pending", (
        "prune-orphans marked a REGISTERED lane's backlog dead — the sink registry "
        "was empty because the command never called ensure_registered_for (#484)"
    )


def test_ledger_targets_is_not_empty():
    """A silently empty set would make the sweep age out the ledger lane."""
    assert ARCHIVIST_KNOWLEDGE_TARGET in LEDGER_TARGETS


def test_age_out_always_leaves_one_row_per_target(config):
    """A swept lane must never present as a lane that never existed.

    `check_outbox_targets` reads outbox history per target and falls into its
    `_declared_outbox_targets - seen` branch when a target has no rows at all —
    reporting "declared lane has NEVER enqueued anything", which is false and
    which REPLACES the STALE diagnosis that check exists to produce. The two
    thresholds make it worse than incidental: `retain_days` defaults to 7 and
    `_PRODUCER_SILENT_AFTER` is also 7 days, so the evidence is destroyed in
    exactly the window where it becomes actionable.
    """
    for i in range(3):
        enqueue(config.db_path, NON_LEDGER, _tp(f"old{i}"), content_hash=f"h{i}")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")

    result = sweep_delivered(config.db_path, retain_days=0)

    rows = _rows(config, NON_LEDGER)
    assert result["aged_out"] == 2, "expected all but the newest row to go"
    assert len(rows) == 1, "the lane was emptied — check_outbox_targets now reads it as never used"


def test_a_swept_lane_is_still_visible_to_check_outbox_targets(config):
    """The property the tombstone exists for, asserted against the real query.

    Pinned against `check_outbox_targets`' own SQL rather than a paraphrase, so
    this fails if that check's grouping changes shape.
    """
    from techne.core.stores import plain_connect

    enqueue(config.db_path, NON_LEDGER, _tp("only"), content_hash="h1")
    _set_all(config, status="synced", created_at="2020-01-01 00:00:00")
    sweep_delivered(config.db_path, retain_days=0)

    conn = plain_connect(config.db_path)
    try:
        seen = {
            r[0]
            for r in conn.execute(
                "SELECT target, COUNT(*), MAX(synced_at), SUM(status='pending'), "
                "MAX(created_at) FROM outbox GROUP BY target"
            )
        }
    finally:
        conn.close()

    assert NON_LEDGER in seen, (
        "the lane vanished from the check's GROUP BY — it will report "
        "'declared lane has NEVER enqueued anything'"
    )


def test_a_backed_up_lane_keeps_its_sync_history(config):
    """The FIRST tombstone preserves presence; it does not preserve MAX(synced_at).

    When a lane has a backlog its newest row is PENDING, so that row takes the
    presence slot and every synced row older than the boundary is deleted —
    leaving MAX(synced_at) NULL. check_outbox_targets then falls to
    temporal_verdict(None), which is UNOBSERVABLE: a healthy, actively-draining
    lane reporting "never synced". UNOBSERVABLE is reserved for a rail whose
    telemetry does not exist, and spending it on a healthy lane is the
    teach-the-operator-to-ignore-the-report failure.

    Asserted against the aggregates the check actually selects, so it fails if
    retention stops preserving them however the sweep is spelled.
    """
    from techne.core.stores import plain_connect

    conn = plain_connect(config.db_path)
    try:
        # Synced history, all older than the boundary. DISTINCT `synced_at`, with
        # the NEWEST on the LOWER id — deliberately, and it is the whole point of
        # the fixture (r4 nit, r5 finding 2). With both rows sharing a timestamp
        # the assertion below cannot tell "kept the max-synced_at row" from "kept
        # an arbitrary synced row", and the plausible simplification
        #   AND id NOT IN (SELECT MAX(id) FROM outbox WHERE status='synced' GROUP BY target)
        # left the whole file green. Ordering max-synced_at against max-id is what
        # makes MAX(id) and MAX(synced_at) disagree, so the mutant now fails.
        # Understating the max by 6 days is not cosmetic: the check reads it
        # against a 14-day interval, so a large enough gap flips LIVE -> STALE.
        for i, synced_at in enumerate(("2026-07-31 00:00:00", "2026-07-25 00:00:00")):
            conn.execute(
                "INSERT INTO outbox (target, data_json, content_hash, status, created_at, "
                "synced_at) VALUES (?, '{}', ?, 'synced', '2026-07-30 00:00:00', ?)",
                (NON_LEDGER, f"old{i}", synced_at),
            )
        # A newer PENDING row — the backlog. This is what takes the presence slot.
        conn.execute(
            "INSERT INTO outbox (target, data_json, content_hash, status, created_at) "
            "VALUES (?, '{}', 'backlog', 'pending', '2026-08-08 00:00:00')",
            (NON_LEDGER,),
        )
        conn.commit()
    finally:
        conn.close()

    sweep_delivered(config.db_path, cutoff="2026-08-01 00:00:00")

    conn = plain_connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT target, COUNT(*), MAX(synced_at), SUM(status='pending'), MAX(created_at) "
            "FROM outbox WHERE target = ? GROUP BY target",
            (NON_LEDGER,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "the lane vanished entirely"
    assert row[2] == "2026-07-31 00:00:00", (
        f"MAX(synced_at) is {row[2]!r} — the check will read this lane as never "
        f"synced and report UNOBSERVABLE on a lane that is actively draining"
    )
    assert row[3] == 1, "the pending backlog row was swept"
