"""Tests for the Librarian Option D document-card layer (techne#91)."""

from __future__ import annotations

import pytest

from techne.config import Config
from techne.core.cards import (
    card_doc_id,
    fill_card_summary,
    find_dangling_cards,
    is_pending,
    pending_cards,
    resolve_card,
    upsert_document_card,
)
from techne.core.database import init_db
from techne.core.documents import store_document
from techne.core.egress import select_tier0_injection
from techne.core.knowledge import content_fingerprint, store


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "techne.db"
    init_db(p).close()
    return p


@pytest.fixture()
def config(db, tmp_path):
    # Domain stores are registry-resolved from the Config now (#102); cards live in
    # techne.db (db), documents in the registry's librarian.db.
    return Config(db_path=db, data_dir=tmp_path)


def _doc(config, url="claude-code-transcript:s1", content="full transcript body"):
    # Documents live in librarian.db (db-per-domain #99); cards in the techne db.
    from techne.core.librarian import librarian_db

    doc_id, _ = store_document(
        librarian_db(config),
        source_type="claude-code-transcript",
        source_url=url,
        content=content,
        title="t",
    )
    return doc_id


def test_upsert_creates_pending_card(db, config):
    doc_id = _doc(config)
    card_id, changed = upsert_document_card(
        db,
        source_url="claude-code-transcript:s1",
        doc_id=doc_id,
        source_version="v1",
        title="decide the backend",
    )
    assert changed
    rows = [r for r in pending_cards(db)]
    assert len(rows) == 1
    card = rows[0]
    assert card["id"] == card_id
    assert card["content"] == "decide the backend"  # gist = title placeholder
    assert is_pending(card["tags"])
    assert card_doc_id(card["tags"]) == doc_id
    assert "backing:mirror" in card["tags"]


def test_upsert_idempotent_same_version(db, config):
    doc_id = _doc(config)
    a, _ = upsert_document_card(db, source_url="u", doc_id=doc_id, source_version="v1", title="x")
    b, changed = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    assert b == a and changed is False


def test_upsert_supersedes_on_version_change(db, config):
    doc_id = _doc(config)
    a, _ = upsert_document_card(db, source_url="u", doc_id=doc_id, source_version="v1", title="x")
    b, changed = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v2", title="x"
    )
    assert changed and b != a
    # Only one active card per source_url.
    assert len(pending_cards(db)) == 1


def test_fill_card_summary_drops_pending(db, config):
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    assert fill_card_summary(db, card_id, "A crisp two-line gist of the session.")
    assert pending_cards(db) == []  # no longer pending
    resolved = resolve_card(config, card_id)
    assert resolved["id"] == doc_id  # pointer still resolves


def test_fill_card_summary_flags_vec_stale(db, config):
    """techne#382: editing a card's gist in place flags the row for re-embed
    (vec_stale=1), so backfill regenerates its vector instead of matching the
    OLD gist. A fresh card is unflagged (its vector, if any, matches its gist)."""
    from techne.core.database import connect

    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )

    def _vec_stale():
        conn = connect(db)
        try:
            return conn.execute(
                "SELECT vec_stale FROM knowledge WHERE id = ?", (card_id,)
            ).fetchone()[0]
        finally:
            conn.close()

    assert _vec_stale() == 0  # freshly created — not flagged
    assert fill_card_summary(db, card_id, "A crisp two-line gist of the session.")
    assert _vec_stale() == 1  # gist edited in place → flagged for re-embed


def test_resolve_card_fetches_document(db, config):
    doc_id = _doc(config, content="the full body to fetch on demand")
    card_id, _ = upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id, source_version="v1", title="x"
    )
    doc = resolve_card(config, card_id)
    assert doc["id"] == doc_id
    assert "fetch on demand" in doc["content"]


def test_document_card_is_pull_only_excluded_from_egress(db):
    # A high-confidence card must NOT be injected — it's recall-only reference.
    card = store(db, "card gist", type="document_card", confidence="high", tags="document_card")
    fact = store(db, "a real fact", type="fact", confidence="high", tags="memory-type:user")
    injected = [r["id"] for r in select_tier0_injection(db, char_budget=4000)]
    assert fact in injected
    assert card not in injected


def test_emit_card_summary_batch(db, config):
    doc_id = _doc(config, content="we decided to use SQLite for the store")
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="storage decision"
    )
    from techne.core.cards import emit_card_summary_batch

    batch = emit_card_summary_batch(config)
    assert len(batch) == 1
    item = batch[0]
    assert item["card_id"] == card_id
    assert item["summary"] == ""  # placeholder for the agent to fill
    assert "use SQLite" in item["prompt"]  # the doc content is in the prompt


def test_ingest_card_summaries_fills_and_skips(db, config):
    doc_id = _doc(config)
    c1, _ = upsert_document_card(db, source_url="u1", doc_id=doc_id, source_version="v1", title="a")
    c2, _ = upsert_document_card(db, source_url="u2", doc_id=doc_id, source_version="v1", title="b")
    from techne.core.cards import ingest_card_summaries

    counts = ingest_card_summaries(
        db,
        [
            {"card_id": c1, "summary": "A crisp gist."},
            {"card_id": c2, "summary": "   "},  # blank → skipped, stays pending
        ],
    )
    assert counts == {"filled": 1, "skipped": 1, "missing": 0}
    still_pending = {r["id"] for r in pending_cards(db)}
    assert c1 not in still_pending and c2 in still_pending


def test_pending_cards_source_prefix_filter(db, config):
    doc = _doc(config)
    upsert_document_card(db, source_url="docs:a.md", doc_id=doc, source_version="v1", title="a")
    upsert_document_card(
        db, source_url="cc-transcript:s1", doc_id=doc, source_version="v1", title="b"
    )
    all_pending = pending_cards(db)
    docs_only = pending_cards(db, source_prefix="docs:")
    assert len(all_pending) == 2
    assert len(docs_only) == 1 and docs_only[0]["source"] == "docs:a.md"


def test_find_dangling_cards_clean(db, config):
    content = "the full body to fetch on demand"
    doc_id = _doc(config, content=content)
    upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version=content_fingerprint(content), title="x"
    )
    report = find_dangling_cards(config)
    assert report == {"total": 1, "dangling": [], "stale": []}


def test_find_dangling_cards_reports_missing_pointer(db, config):
    """#104: a card whose doc id doesn't exist in librarian.db (e.g. librarian.db
    rebuilt out of sync) is reported, not silently None."""
    upsert_document_card(db, source_url="u", doc_id=9999, source_version="v1", title="x")
    report = find_dangling_cards(config)
    assert report["total"] == 1
    assert len(report["dangling"]) == 1 and report["dangling"][0]["doc_id"] == 9999
    assert report["stale"] == []


def test_find_dangling_cards_reports_stale_version(db, config):
    """A card whose ver: no longer matches the live document content is flagged."""
    doc_id = _doc(config, content="current body")
    upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="hash-of-an-older-body", title="x"
    )
    report = find_dangling_cards(config)
    assert report["dangling"] == []
    assert len(report["stale"]) == 1 and report["stale"][0]["doc_id"] == doc_id


# ---------------------------------------------------------------------------
# techne#684 — repairing cards that never had a pointer
# ---------------------------------------------------------------------------


def _pointerless_card(db, content="real durable knowledge"):
    """A document_card with NO doc: tag — the shape #633's producer bug minted."""
    from techne.core.cards import CARD_TYPE
    from techne.core.database import connect

    conn = connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO knowledge (type, content, confidence, source, tags) "
            "VALUES (?, ?, 'high', 'consolidate-merge:1', '')",
            (CARD_TYPE, content),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_repair_is_dry_run_by_default(db, config):
    """A repair that writes without being asked is not a repair, it is a surprise."""
    from techne.core.cards import CARD_TYPE, repair_pointerless_cards
    from techne.core.database import connect

    kid = _pointerless_card(db)
    result = repair_pointerless_cards(config)

    assert result["retyped"] == [kid]
    assert result["applied"] is False
    conn = connect(db)
    try:
        assert conn.execute("SELECT type FROM knowledge WHERE id=?", (kid,)).fetchone()[0] == CARD_TYPE
    finally:
        conn.close()


def test_repair_retypes_and_clears_fsck(db, config):
    """The whole point: fsck can go green. It had been red since June because the
    #633 producer fix cleared the cause and left the debris."""
    from techne.core.cards import find_dangling_cards, repair_pointerless_cards
    from techne.core.database import connect

    kid = _pointerless_card(db, content="db-per-domain: one store file per persona")
    assert len(find_dangling_cards(config)["dangling"]) == 1

    result = repair_pointerless_cards(config, apply=True)
    assert result["applied"] is True and result["retyped"] == [kid]

    conn = connect(db)
    try:
        row = conn.execute("SELECT type, content FROM knowledge WHERE id=?", (kid,)).fetchone()
    finally:
        conn.close()
    assert row[0] == "fact"
    # THE CONTENT SURVIVES. These rows are real knowledge wearing the wrong type;
    # deleting them would have been the easy way to a green check and a real loss.
    assert row[1] == "db-per-domain: one store file per persona"
    assert find_dangling_cards(config)["dangling"] == []


def test_repair_refuses_to_touch_a_card_with_an_unresolvable_pointer(db, config):
    """THE DISTINCTION THAT MATTERS. A pointer that EXISTS and does not resolve is
    librarian.db out of sync — retyping would destroy the pointer and the evidence
    of the fault. Restoring the doc and superseding the card are both valid; only a
    human knows which, so this reports and never guesses."""
    from techne.core.cards import CARD_TYPE, repair_pointerless_cards
    from techne.core.database import connect

    upsert_document_card(db, source_url="u", doc_id=9999, source_version="v1", title="x")
    result = repair_pointerless_cards(config, apply=True)

    assert result["retyped"] == []
    assert len(result["needs_human"]) == 1
    assert result["needs_human"][0]["doc_id"] == 9999

    conn = connect(db)
    try:
        types = [r[0] for r in conn.execute("SELECT type FROM knowledge")]
    finally:
        conn.close()
    assert CARD_TYPE in types, "the card must survive untouched"


def test_repair_separates_the_two_faults_in_one_pass(db, config):
    from techne.core.cards import repair_pointerless_cards

    kid = _pointerless_card(db)
    upsert_document_card(db, source_url="u", doc_id=9999, source_version="v1", title="x")

    result = repair_pointerless_cards(config, apply=True)
    assert result["retyped"] == [kid]
    assert [d["doc_id"] for d in result["needs_human"]] == [9999]


def test_fsck_names_a_remedy_for_a_repairable_fault(db, config):
    """techne#684's actual defect: fsck could name a structural fault and offer the
    operator nothing. A check that can never go green trains them to ignore it."""
    from techne.core.maintenance import run_fsck

    _doc(config, content="anything — this exists only to bring librarian.db into being,\n"
         "which _check_dangling_cards requires before it will run at all")
    _pointerless_card(db)
    report = run_fsck(config)
    check = next(c for c in report.checks if c.name == "dangling_cards")
    assert check.ok is False
    assert check.remedy and "cards repair" in check.remedy


def test_fsck_does_not_offer_the_repair_for_a_fault_it_cannot_fix(db, config):
    """Naming `cards repair` for an unresolvable pointer would promise a fix that
    command deliberately refuses to perform."""
    from techne.core.maintenance import run_fsck

    _doc(config, content="brings librarian.db into being")
    upsert_document_card(db, source_url="u", doc_id=9999, source_version="v1", title="x")
    check = next(c for c in run_fsck(config).checks if c.name == "dangling_cards")
    assert check.ok is False
    assert check.remedy and "cards repair" not in check.remedy


# ---------------------------------------------------------------------------
# #244 — write-site redaction: fill_card_summary
# ---------------------------------------------------------------------------


def test_fill_card_summary_redacts_credential(db, config):
    """#244: fill_card_summary must store the agent gist redacted (techne#244).

    GitHub PAT in the gist — uses a long-established pattern so this test does
    not depend on any single new #243 pattern.
    """
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="u", doc_id=doc_id, source_version="v1", title="x"
    )
    gist_with_secret = (
        "The repo was cloned using ghp_abcdefghijklmnopqrstuvwxyz1234 "
        "and the migration was applied."
    )
    result = fill_card_summary(db, card_id, gist_with_secret)
    assert result is True

    # Read the knowledge row back directly to verify the stored value
    from techne.core.database import connect

    conn = connect(db)
    row = conn.execute("SELECT content FROM knowledge WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    stored_content = row["content"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in stored_content
    assert "<redacted:github>" in stored_content


def test_insert_card_redacts_content_defense_in_depth(db, config):
    """#244 (review): _insert_card redacts at the INSERT boundary itself, so a
    credential reaching the card content by any path (here a doc title used as the
    stand-in gist) is redacted at rest — not only when the upstream caller remembers.
    """
    doc_id = _doc(config)
    # No summary → the title becomes the card's content (the placeholder gist),
    # exercising _insert_card's own redaction rather than an upstream wrapper.
    card_id, changed = upsert_document_card(
        db,
        source_url="u-secret",
        doc_id=doc_id,
        source_version="v1",
        title="issue mentions ghp_abcdefghijklmnopqrstuvwxyz1234 in the body",
    )
    assert changed

    from techne.core.database import connect

    conn = connect(db)
    row = conn.execute("SELECT content FROM knowledge WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in row["content"]
    assert "<redacted:github>" in row["content"]


def test_a_pending_cards_title_is_repaired_at_the_same_version(db, config):
    """techne#819, the half the issue's scope note misses.

    A PENDING card's content IS the document title — the stand-in gist it carries
    until the agent-assisted pass fills a real one. The `ver:` tag is a CONTENT
    fingerprint, so a corrected title leaves it unchanged, the upsert no-ops, and
    the junk title survives as the card's gist. Repairing `documents.title` alone
    would still leave recall returning the wrapper string, because recall reads the
    CARD, not the document.
    """
    doc_id = _doc(config)
    first, _ = upsert_document_card(
        db,
        source_url="claude-code-transcript:s1",
        doc_id=doc_id,
        source_version="v1",
        title="<local-command-caveat>Caveat: The messages below",
    )
    second, changed = upsert_document_card(
        db,
        source_url="claude-code-transcript:s1",
        doc_id=doc_id,
        source_version="v1",  # SAME version — content did not move, only the title
        title="why is recall empty",
    )
    assert second == first, "a title repair must not supersede the card"
    assert changed is False, "no new card row was written"
    card = next(r for r in pending_cards(db) if r["id"] == first)
    assert card["content"] == "why is recall empty"
    assert is_pending(card["tags"]), "the card is still awaiting a real gist"


def test_a_filled_cards_gist_is_never_overwritten_by_a_title(db, config):
    """The guard on the repair above. A filled card's gist is real prose that no
    longer derives from the title, so rewriting it from one would destroy the
    agent-assisted work `fill_card_summary` exists to produce.
    """
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db,
        source_url="claude-code-transcript:s1",
        doc_id=doc_id,
        source_version="v1",
        title="junk title",
    )
    fill_card_summary(db, card_id, "A real gist written by the agent-assisted pass.")

    upsert_document_card(
        db,
        source_url="claude-code-transcript:s1",
        doc_id=doc_id,
        source_version="v1",
        title="a different title entirely",
    )
    row = db_row_content(db, card_id)
    assert row == "A real gist written by the agent-assisted pass.", (
        "a filled gist was clobbered by a title repair"
    )


def db_row_content(db, card_id):
    from techne.core.database import connect

    conn = connect(db)
    try:
        return conn.execute("SELECT content FROM knowledge WHERE id = ?", (card_id,)).fetchone()[
            "content"
        ]
    finally:
        conn.close()


def _row(db, card_id):
    from techne.core.database import connect

    conn = connect(db)
    try:
        r = conn.execute(
            "SELECT content, tags, vec_stale FROM knowledge WHERE id = ?", (card_id,)
        ).fetchone()
        return dict(r)
    finally:
        conn.close()


def test_a_repaired_card_is_flagged_for_re_embedding(db, config):
    """techne#840 r1 CRITICAL 1. An in-place `content` edit on a stable rowid leaves
    the OLD embedding in `knowledge_vec`. `backfill_vectors` re-embeds
    `(NOT IN vec OR vec_stale = 1)`, so an unflagged repaired row is in NEITHER set
    and semantic recall goes on matching the junk title forever.

    FTS heals itself through the `knowledge_au` trigger, which is exactly what makes
    this invisible: half of recall looks fixed. And doctor's vector-freshness check
    counts `vec_stale = 1`, so it would report such a store 100% fresh — a false
    green about the thing that is broken.
    """
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id,
        source_version="v1", title="<local-command-caveat>Caveat: junk",
    )
    assert _row(db, card_id)["vec_stale"] == 0

    upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id,
        source_version="v1", title="why is recall empty",
    )
    row = _row(db, card_id)
    assert row["content"] == "why is recall empty"
    assert row["vec_stale"] == 1, "a repaired card must be queued for re-embedding"


def test_a_repair_that_carries_a_real_summary_drops_pending(db, config):
    """techne#840 r1 CRITICAL 2, and it is the guard's own premise.

    `content` is `summary or title`, and `build_transcript_gist` returns None before
    thread detection and a real string after — so the ordinary two-pass sequence
    reaches this branch WITH a summary. Leaving `pending` on would keep a filled
    card on the agent-assisted emit work-list forever, and would break
    `is_pending(stored tags)` as a proxy for "never filled": that proxy is only
    sound while EVERY path that fills a card drops the tag, and this branch is now
    one of those paths.
    """
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id,
        source_version="v1", title="junk title",
    )
    assert is_pending(_row(db, card_id)["tags"])

    upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id,
        source_version="v1", title="junk title",
        summary="Session transcript (2026-08-29, 3 threads): a; b; c",
    )
    row = _row(db, card_id)
    assert row["content"].startswith("Session transcript")
    assert not is_pending(row["tags"]), "a filled card must leave the emit work-list"
    assert card_id not in [c["id"] for c in pending_cards(db)]


def test_the_repair_redacts_like_the_insert_path_does(db, config):
    """techne#840 r1 Important 3. `_insert_card` redacts even though its callers do,
    because "today's callers redact" is not a property you can hold still. This
    UPDATE is the second write path into `knowledge.content` in this file.

    The comparison is the second-order half: `content != existing["content"]` puts a
    RAW incoming string against a stored one that went through redaction, so an
    unredacted caller would not merely leak — the two could never be equal and the
    row would be rewritten on every index pass, forever, carrying the credential.
    """
    doc_id = _doc(config)
    card_id, _ = upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id,
        source_version="v1", title="a harmless title",
    )
    upsert_document_card(
        db, source_url="claude-code-transcript:s1", doc_id=doc_id,
        source_version="v1", title="token ghp_0123456789abcdefghijklmnopqrstuvwxyzAB",
    )
    content = _row(db, card_id)["content"]
    assert "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB" not in content, content
