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

THE 2,823 LEGACY NULLS ARE LEFT ALONE, ON PURPose. Backfilling them is option 1,
which the issue describes as "a retrieval-behaviour change disguised as a schema
fix" that "should be measured against the eval gate before shipping". It changes
what recall returns on every existing install and belongs in its own change,
with that measurement. `test_legacy_nulls_are_not_backfilled` pins the decision
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

    Built by dropping and re-adding the column the way the v5 migration does,
    which is the only shape SQLite lets that migration produce.
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


def test_legacy_nulls_are_not_backfilled(migrated):
    """PINS THE DECISION. Backfilling is option 1 and changes ranking everywhere.

    If someone later adds the backfill, this fails and makes them say so on
    purpose -- with the eval-gate measurement the issue asks for.
    """
    conn = sqlite3.connect(migrated)
    conn.execute(
        "INSERT INTO knowledge (type, content, source, tags, confidence) "
        "VALUES ('fact', 'a legacy row', 'test', 't', 'high')"
    )
    conn.commit()
    legacy_id = conn.execute("SELECT id FROM knowledge WHERE content='a legacy row'").fetchone()[0]
    conn.close()

    # A later write through the fixed path must not touch the legacy row.
    store(migrated, "an unrelated new fact", type="fact", tags="t")

    assert _lca(migrated, legacy_id) is None, (
        "legacy NULLs must survive untouched; backfilling them is a retrieval "
        "change that needs its own measured decision"
    )


def test_decay_semantics_for_an_absent_timestamp_are_unchanged(fresh):
    """The absent case still means 'undecayed', which is why legacy rows are safe.

    Changing this would have the same blast radius as the backfill: it would
    re-rank every legacy row on every existing install.
    """
    from techne.core.knowledge import decay_score

    assert decay_score(0.9, None, half_life_days=30) == pytest.approx(0.9)
