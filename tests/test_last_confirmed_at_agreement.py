"""A store's provenance must not change its retrieval order (techne#938).

`knowledge.last_confirmed_at` was `NOT NULL DEFAULT CURRENT_TIMESTAMP` on a
fresh install and plain nullable `TIMESTAMP` on a migrated one, AT THE SAME
schema_version. SQLite cannot `ADD COLUMN ... NOT NULL` without a default, so
the v5 ALTER could never have matched the declaration as written -- a real
constraint, not an oversight in the migration alone.

THE CONSEQUENCE IS A RANKING DIVERGENCE, not a cosmetic one. None of the three
`INSERT INTO knowledge` paths named the column. On a fresh store the schema
default filled it; on a migrated store there was no default, so every new row
landed NULL -- 2,823 of 2,914 live rows on the operator's store, still growing
(MIN created_at 2026-06-20, MAX 2026-09-15). `decay_score` returns
`confidence_numeric` UNDECAYED when the timestamp is absent, so `_rerank`'s
decay tiebreak was a pure confidence term for 96.9% of the curated layer, while
the same rows DID decay on a fresh install. Identical code, identical
schema_version, different retrieval order.

THE FIX IS OPTION 3 THEN 2 FROM THE ISSUE, AND DELIBERATELY NOT OPTION 1.
A schema default cannot be made to agree across both paths, so the declaration
now matches what the ALTER can actually produce (nullable, no default) and the
value is guaranteed by the WRITE path instead -- the only place that knows a row
is being created rather than migrated.

THE 2,823 LEGACY NULLS ARE LEFT ALONE, ON PURPOSE. Backfilling them is option 1,
which the issue describes as "a retrieval-behaviour change disguised as a schema
fix" that "should be measured against the eval gate before shipping". It changes
what recall returns on every existing install and belongs in its own change,
with that measurement. `test_existing_nulls_are_not_backfilled_on_open` pins the decision
so a later backfill is a deliberate act rather than a drive-by.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.core.database import init_db
from techne.core.knowledge import merge_into, store, supersede


def _decl(db):
    conn = sqlite3.connect(db)
    try:
        row = [
            r for r in conn.execute("PRAGMA table_info(knowledge)") if r[1] == "last_confirmed_at"
        ]
        assert row, "column missing"
        return {"type": row[0][2], "notnull": row[0][3], "default": row[0][4]}
    finally:
        conn.close()


def _lca(db, kid):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT last_confirmed_at FROM knowledge WHERE id=?", (kid,)
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def fresh(tmp_path):
    db = tmp_path / "fresh.db"
    init_db(db)
    return db


@pytest.fixture
def migrated(tmp_path):
    """A store whose column arrived via ALTER -- nullable, no default.

    Built by dropping and re-adding the column the way the v5 migration does.
    It is not the only shape ALTER can produce -- a CONSTANT default would be
    accepted -- but it is the only one reconcilable with a CURRENT_TIMESTAMP
    default, which is what the CREATE TABLE carried.
    """
    db = tmp_path / "migrated.db"
    init_db(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE knowledge DROP COLUMN last_confirmed_at")
    conn.execute("ALTER TABLE knowledge ADD COLUMN last_confirmed_at TIMESTAMP")
    conn.commit()
    conn.close()
    return db


def test_the_two_store_shapes_declare_the_same_column(fresh, migrated):
    """THE REGRESSION. Before the fix these differed on notnull and default."""
    assert _decl(fresh) == _decl(migrated), (
        f"fresh={_decl(fresh)} migrated={_decl(migrated)} — "
        "a store's provenance must not change its schema"
    )
    assert _decl(fresh)["notnull"] == 0
    assert _decl(fresh)["default"] is None


@pytest.mark.parametrize("which", ["fresh", "migrated"])
def test_store_stamps_the_timestamp_on_both_shapes(request, which):
    """The write path is what guarantees the value now, not the schema default."""
    db = request.getfixturevalue(which)
    kid = store(db, "a durable fact", type="fact", tags="t")
    assert _lca(db, kid) is not None, f"{which}: a new row must carry last_confirmed_at"


@pytest.mark.parametrize("which", ["fresh", "migrated"])
def test_supersede_stamps_the_timestamp(request, which):
    """The second INSERT path. A superseding row is new and must decay from now."""
    db = request.getfixturevalue(which)
    old = store(db, "the old claim", type="fact", tags="t")
    new = supersede(db, old, "the corrected claim")
    assert _lca(db, new) is not None, f"{which}: supersede must stamp the new row"


@pytest.mark.parametrize("which", ["fresh", "migrated"])
def test_merge_stamps_the_timestamp(request, which):
    """The third INSERT path."""
    db = request.getfixturevalue(which)
    a = store(db, "claim a", type="fact", tags="t")
    b = store(db, "claim b", type="fact", tags="t")
    new = merge_into(db, [a, b], "the merged claim", type="fact", confidence="high")
    assert _lca(db, new) is not None, f"{which}: merge must stamp the new row"


def test_existing_nulls_are_not_backfilled_on_open(tmp_path):
    """PINS THE DECISION not to re-rank existing installs (techne#938).

    TWO WRONG PREMISES WERE BURNED GETTING HERE, AND BOTH ARE WORTH RECORDING.

    The FIRST version of this test inserted its NULL row after `init_db` had run
    and asserted it survived. That pinned nothing: a backfill written as a
    migration step executes inside `init_db` against an empty table and never
    sees a row added afterwards.

    The SECOND version built a v4 store with a NULL row and migrated it -- and
    failed, because the v5 step has ALWAYS carried
    `UPDATE knowledge SET last_confirmed_at = created_at WHERE ... IS NULL`.
    Backfilling a row that predates the column is existing, correct behaviour.

    So the 2,823 NULLs on the operator's store are NOT rows migration missed.
    They were written AFTER v5 ran, by INSERT paths that named no column while
    the migrated store had no default to fall back on. The risk worth pinning is
    therefore a backfill that fires on OPEN -- in `connect()`, `store()`, or a
    future migration step operating on an already-current store -- because that is
    what would silently re-rank 96.9% of the curated layer on every install.
    Issue #938 calls that option 1 and says it must be measured against the eval
    gate first.
    """
    import sqlite3

    from techne.core.database import connect, init_db
    from techne.core.knowledge import store

    db = tmp_path / "current.db"
    init_db(db)

    # A row of the shape the old code produced: current schema, no timestamp.
    raw = sqlite3.connect(db)
    raw.execute(
        "INSERT INTO knowledge (type, content, source, tags, confidence) "
        "VALUES ('fact', 'written by the old code', 'test', 't', 'high')"
    )
    raw.commit()
    raw.close()

    # Everything an ordinary run does to a store.
    connect(db).close()
    store(db, "an unrelated new fact", type="fact", tags="t")
    connect(db).close()

    raw = sqlite3.connect(db)
    value = raw.execute(
        "SELECT last_confirmed_at FROM knowledge WHERE content = 'written by the old code'"
    ).fetchone()[0]
    raw.close()

    assert value is None, (
        "an existing NULL was backfilled by opening or writing to the store. That "
        "is issue #938's option 1 -- it changes what recall returns on every "
        "existing install and must be measured against the eval gate, not "
        "smuggled in as a side effect of connect() or store()."
    )


def test_decay_semantics_for_an_absent_timestamp_are_unchanged(fresh):
    """The absent case still means 'undecayed', which is why legacy rows are safe.

    Changing this would have the same blast radius as the backfill: it would
    re-rank every legacy row on every existing install.
    """
    from techne.core.knowledge import decay_score

    assert decay_score(0.9, None, half_life_days=30) == pytest.approx(0.9)


def test_every_insert_into_knowledge_sets_the_timestamp():
    """THE PRE-PASS FINDING, AND THE ONLY SHAPE THAT COULD HAVE CAUGHT IT.

    The first cut of this change said "all three INSERT INTO knowledge paths"
    and patched three. There are FIVE: `cards.py::_insert_card` and
    `reflect.py::_apply_add` were missed, and _apply_add is promote_candidate's
    "add" dispatch -- the main way curated knowledge grows.

    That is worse than leaving them alone. Removing the CREATE TABLE default was
    safe only if every writer set the value; with two writers not setting it, a
    FRESH install started writing NULL where the default used to fill it. The
    change moved fresh stores TOWARD the migrated store's broken state for
    reflect-promoted and card rows -- the opposite of its purpose.

    An enumerated test cannot catch a path nobody enumerated, so this one derives
    the list from the source. A new writer is covered the day it is written.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "techne"
    pattern = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+knowledge\b", re.I)

    missing = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            # knowledge_candidates / knowledge_vec / knowledge_fts are other tables.
            after = text[match.end() : match.end() + 1]
            if after and (after.isalnum() or after == "_"):
                continue
            window = text[match.start() : match.start() + 700]
            statement = window.split('"""')[0] if '"""' in window else window[:400]
            if "last_confirmed_at" not in statement:
                line = text[: match.start()].count("\n") + 1
                missing.append(f"{path.relative_to(root.parent)}:{line}")

    assert not missing, (
        "INSERT INTO knowledge without last_confirmed_at: "
        + ", ".join(missing)
        + ". The CREATE TABLE carries no default (techne#938), so a path that "
        "omits the column writes NULL and the row never decays."
    )
