"""Document cards — the Librarian Option D recall layer (techne#91).

A *card* is a compact knowledge row (type=document_card) that points at a row in
the `documents` table. Recall returns the card's GIST; full content is fetched ON
DEMAND via the pointer — "remember a thing exists + roughly what it says, fetch the
detail when needed." Cards are pull-only (recall-able, never injected).

Mirror-backed for now: the pointer is a local `documents.id` and `source_version`
is the document's content hash. (Pointer-backed sources — Jira/Confluence, live
fetch through limenal — come later, cabal#15.)

The gist is summarized agent-side (techne can't call a model — no API key). A card
is created `pending` with the document title as a stand-in gist, then filled later
via the emit/ingest summarization pass — the same shape as reflect.

State encoded in the knowledge row (no schema change):
  source = the document's source_url (the stable pointer)
  tags   = document_card, backing:<mode>, doc:<id>, ver:<source_version> [, pending]
"""

from __future__ import annotations

from pathlib import Path

from techne.config import Config
from techne.core.database import connect
from techne.core.knowledge import CONFIDENCE_NUMERIC
from techne.core.redaction import redact_credentials

CARD_TYPE = "document_card"


def _tag_value(tags: str | None, prefix: str) -> str | None:
    for t in (tags or "").split(","):
        t = t.strip()
        if t.startswith(prefix):
            return t[len(prefix) :]
    return None


def card_doc_id(tags: str | None) -> int | None:
    v = _tag_value(tags, "doc:")
    return int(v) if v and v.isdigit() else None


def _without_pending(tags: str | None) -> str:
    """Drop the `pending` marker, preserving every other tag and their order.

    ONE implementation, deliberately (techne#840 r1). `fill_card_summary` had this
    inline and the techne#819 repair became a SECOND path that fills a card, so two
    copies would have been two chances to drift on a tag that decides whether a
    card stays on the agent-assisted work-list.
    """
    return ",".join(t for t in (tags or "").split(",") if t.strip() != "pending")


def is_pending(tags: str | None) -> bool:
    return "pending" in [t.strip() for t in (tags or "").split(",")]


def _card_tags(doc_id: int, source_version: str, backing_mode: str, pending: bool) -> str:
    parts = [CARD_TYPE, f"backing:{backing_mode}", f"doc:{doc_id}", f"ver:{source_version}"]
    if pending:
        parts.append("pending")
    return ",".join(parts)


def _insert_card(conn, content: str, source_url: str, tags: str, confidence: str) -> int:
    # Defense in depth: redact at the INSERT boundary too. Today's callers
    # (upsert_document_card via a document title/summary, fill_card_summary via
    # an agent gist) already redact upstream, but redacting here means a future
    # second caller can't reopen the sink by forgetting to.
    cur = conn.execute(
        # last_confirmed_at is set HERE, not left to a schema default (techne#938).
        # The CREATE TABLE no longer carries one -- it could not, because ALTER
        # refuses a NON-CONSTANT default and the two shapes have to agree -- so
        # every INSERT owns the value. A pending card was NULL until
        # fill_card_summary stamped it, which put it on decay_score's no-decay
        # branch in the meantime -- ON A MIGRATED STORE. On a fresh one the schema
        # default filled it at insert, which is exactly the two-shape divergence
        # techne#938 exists to remove.
        """INSERT INTO knowledge
             (type, content, source, tags, confidence, confidence_numeric,
              last_confirmed_at)
           VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (
            CARD_TYPE,
            redact_credentials(content),
            source_url,
            tags,
            confidence,
            CONFIDENCE_NUMERIC[confidence],
        ),
    )
    return cur.lastrowid


def upsert_document_card(
    db_path: Path,
    *,
    source_url: str,
    doc_id: int,
    source_version: str,
    title: str,
    backing_mode: str = "mirror",
    summary: str | None = None,
    confidence: str = "medium",
) -> tuple[int, bool]:
    """Create/refresh the card for a document. One active card per source_url.

    - No active card → insert (pending unless a summary is given).
    - Active card, same source_version → no-op.
    - Active card, version changed → supersede it, insert a fresh (pending) card.

    Returns (card_id, changed).
    """
    pending = summary is None
    content = summary or title or f"document {doc_id}"
    tags = _card_tags(doc_id, source_version, backing_mode, pending)

    conn = connect(db_path)
    try:
        # BEGIN IMMEDIATE serializes writers so the SELECT→INSERT is atomic — else two
        # concurrent callers both see no active card and both insert, leaving two
        # active cards per (type, source_url). A partial unique index can't be used:
        # the insert-first supersede path is transiently two-active. (techne#170)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT id, tags, content FROM knowledge
               WHERE type = ? AND source = ? AND superseded_by IS NULL""",
            (CARD_TYPE, source_url),
        ).fetchone()

        if existing is not None and _tag_value(existing["tags"], "ver:") == str(source_version):
            # PENDING-CARD TITLE REPAIR (techne#819). A pending card's content IS
            # the document title — the stand-in gist it carries until the
            # agent-assisted pass fills a real one. The `ver:` tag is a CONTENT
            # fingerprint, so a corrected title leaves it unchanged and lands here,
            # and the junk title survives as the card's gist. That is the half of
            # #819 the issue's own scope note misses: repairing `documents.title`
            # alone still leaves recall returning the wrapper string, because recall
            # reads the card, not the document.
            #
            # Only PENDING cards. A filled card's gist is real prose that no longer
            # derives from the title, so rewriting it from a title would destroy
            # work. Updated in place without superseding, on `fill_card_summary`'s
            # precedent: the document version has not moved, only the stand-in.
            # `is_pending(existing["tags"])`, NOT the `pending` computed from this
            # call's arguments. Those are different questions: `pending` above means
            # "this CALLER supplied no summary", which every indexer pass does, while
            # what matters here is whether the STORED card has already been filled.
            # Reading the caller's flag let an indexer clobber an agent-written gist
            # — caught by test_a_filled_cards_gist_is_never_overwritten_by_a_title.
            repaired = redact_credentials(content)
            if is_pending(existing["tags"]) and repaired != existing["content"]:
                # vec_stale=1 (techne#382) — CRITICAL, and it was missing. This is an
                # in-place `content` edit on a STABLE rowid, so the row keeps its old
                # embedding: `backfill_vectors` re-embeds `(NOT IN vec OR vec_stale=1)`
                # and a repaired row is in NEITHER set, so the junk title would go on
                # being what semantic recall matches, forever. FTS heals itself via the
                # `knowledge_au` trigger, which is what makes the failure invisible —
                # and doctor's vector-freshness check counts `vec_stale=1`, so it would
                # have reported such a store 100% fresh. `fill_card_summary` sets this
                # flag forty lines below and its comment says ANY in-place content edit
                # must; this one did not.
                #
                # DROP `pending` WHEN A REAL SUMMARY ARRIVED, for two reasons. The card
                # leaves the emit work-list, which it otherwise never would — `content`
                # is `summary or title`, so on the ordinary two-pass sequence
                # (`build_transcript_gist` returns None before thread detection and a
                # string after) this branch writes a real gist and left the tag on,
                # offering a filled card up for filling forever. And it keeps
                # `is_pending(stored tags)` a SOUND PROXY for "never filled": that proxy
                # holds only while every path that fills a card drops the tag, and this
                # branch is now one of those paths. Without it the clobber this guard
                # closes re-opens one pass later, when a card sitting at
                # `content=<real gist>, tags=pending` meets a pass whose summary is None.
                new_tags = (
                    existing["tags"] if summary is None else _without_pending(existing["tags"])
                )
                conn.execute(
                    """UPDATE knowledge
                       SET content = ?, tags = ?, updated_at = CURRENT_TIMESTAMP,
                           vec_stale = 1
                       WHERE id = ?""",
                    (repaired, new_tags, existing["id"]),
                )
                conn.commit()
                return existing["id"], False
            conn.commit()  # release the write lock; nothing changed
            return existing["id"], False

        new_id = _insert_card(conn, content, source_url, tags, confidence)
        if existing is not None:
            conn.execute(
                """UPDATE knowledge SET superseded_by = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (new_id, existing["id"]),
            )
        conn.commit()
        return new_id, True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fill_card_summary(db_path: Path, card_id: int, summary: str) -> bool:
    """Fill a card's gist in place and drop `pending`. No supersede — the document
    version is unchanged, only the summary got better. Returns True if updated."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT tags FROM knowledge WHERE id = ? AND type = ?", (card_id, CARD_TYPE)
        ).fetchone()
        if row is None:
            return False
        tags = _without_pending(row["tags"])
        conn.execute(
            # vec_stale=1 (techne#382): the gist `content` changed, so this row's
            # existing embedding is now stale. This plain connection can't touch
            # the vec0 table (techne#168); flagging here lets backfill_vectors
            # re-embed it. ANY in-place `content` edit must set this flag.
            """UPDATE knowledge
               SET content = ?, tags = ?, updated_at = CURRENT_TIMESTAMP,
                   last_confirmed_at = CURRENT_TIMESTAMP, vec_stale = 1
               WHERE id = ?""",
            (redact_credentials(summary), tags, card_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def pending_cards(db_path: Path, limit: int = 100, source_prefix: str | None = None) -> list[dict]:
    """Active cards still awaiting an agent-written gist (the emit work-list).
    `source_prefix` filters by pointer prefix (e.g. 'docs:' or 'claude-code-')."""
    sql = """SELECT id, content, source, tags FROM knowledge
             WHERE type = ? AND superseded_by IS NULL AND tags LIKE '%pending%'"""
    params: list = [CARD_TYPE]
    if source_prefix:
        sql += " AND source LIKE ?"
        params.append(f"{source_prefix}%")
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def resolve_card(config: Config, card_id: int) -> dict | None:
    """Fetch the full document a card points at — the on-demand half of recall."""
    conn = connect(config.db_path)
    try:
        row = conn.execute(
            "SELECT tags FROM knowledge WHERE id = ? AND type = ?", (card_id, CARD_TYPE)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    doc_id = card_doc_id(row["tags"])
    if doc_id is None:
        return None
    # Cross-db: the card lives in techne.db, the document in librarian.db. Fetch it
    # through the Librarian StoreAdapter's generic get() (techne#102) — this is the
    # live recall path proving the Protocol surface works, so a non-sqlite store
    # (techne#87) swaps in without rewriting this caller.
    from techne.core.librarian import librarian_store

    return librarian_store(config).get("documents", doc_id)


def find_dangling_cards(config: Config) -> dict:
    """Cross-db health check (techne#104): the card→document pointer is a tag-string
    with no FK (can't have one cross-db), so a librarian.db rebuilt/restored out of
    sync with techne.db leaves cards that `resolve_card` silently returns None for.

    Reports active document_cards that are `dangling` (pointer doesn't resolve in
    librarian.db) or `stale` (the live document's content no longer hashes to the
    card's `ver:`). Returns {total, dangling: [...], stale: [...]}."""
    from techne.core.knowledge import content_fingerprint
    from techne.core.librarian import librarian_store

    store = librarian_store(config)
    conn = connect(config.db_path)
    try:
        cards = conn.execute(
            "SELECT id, source, tags FROM knowledge WHERE type = ? AND superseded_by IS NULL",
            (CARD_TYPE,),
        ).fetchall()
    finally:
        conn.close()

    dangling: list[dict] = []
    stale: list[dict] = []
    for c in cards:
        doc_id = card_doc_id(c["tags"])
        if doc_id is None:
            dangling.append({"card_id": c["id"], "source": c["source"], "reason": "no doc pointer"})
            continue
        doc = store.get("documents", doc_id)
        if doc is None:
            dangling.append(
                {"card_id": c["id"], "source": c["source"], "doc_id": doc_id, "reason": "missing"}
            )
            continue
        ver = _tag_value(c["tags"], "ver:")
        if ver and ver != content_fingerprint(doc.get("content") or ""):
            stale.append({"card_id": c["id"], "source": c["source"], "doc_id": doc_id})
    return {"total": len(cards), "dangling": dangling, "stale": stale}


def repair_pointerless_cards(config: Config, *, apply: bool = False) -> dict:
    """Retype `document_card` rows that never had a doc pointer (techne#684).

    THE TWO DANGLING REASONS ARE NOT THE SAME FAULT, and conflating them would
    destroy information:

    * ``no doc pointer`` — the row was NEVER a card. A document_card is a pointer
      at a librarian document and this one has none, so the type is simply wrong;
      #633's producer bug picked it when there was no document to point at. The
      content is real, durable knowledge. Retyping to `fact` preserves it and
      drops the false type. Mechanical and safe.
    * ``missing`` — the pointer EXISTS and does not resolve. That is librarian.db
      rebuilt or restored out of sync with techne.db. Retyping would be wrong: the
      remedy is to restore the document or supersede the card, and only a human
      knows which. THIS FUNCTION NEVER TOUCHES THOSE — it reports them.

    Dry-run by default. `apply=True` performs the retype. Returns
    {retyped, needs_human, applied}.
    """
    from techne.core.database import connect

    report = find_dangling_cards(config)
    pointerless = [d for d in report["dangling"] if d.get("reason") == "no doc pointer"]
    needs_human = [d for d in report["dangling"] if d.get("reason") != "no doc pointer"]

    if apply and pointerless:
        conn = connect(config.db_path)
        try:
            conn.executemany(
                "UPDATE knowledge SET type = 'fact' WHERE id = ? AND type = ?",
                [(d["card_id"], CARD_TYPE) for d in pointerless],
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "retyped": [d["card_id"] for d in pointerless],
        "needs_human": needs_human,
        "applied": bool(apply and pointerless),
    }


# --- agent-assisted gist fill (emit/ingest, same shape as reflect) -------------

_SUMMARY_PROMPT = (
    "Summarize this document in 2-3 sentences — what it covers and any durable "
    "decisions or outcomes worth recalling later. Write the gist only, no preamble.\n\n"
    "# {title}\n\n{content}"
)


def emit_card_summary_batch(
    config: Config,
    *,
    limit: int = 50,
    content_chars: int = 8000,
    source_prefix: str | None = None,
) -> list[dict]:
    """Build per-pending-card summarization prompts (no LLM). The agent writes a
    gist into each `summary` field; ingest_card_summaries feeds them back. The
    no-API-key path for the Option D gist, mirroring reflect emit/ingest.
    `source_prefix` scopes to one source (e.g. 'docs:')."""
    batch: list[dict] = []
    for card in pending_cards(config.db_path, limit=limit, source_prefix=source_prefix):
        doc = resolve_card(config, card["id"])
        if doc is None:
            continue
        prompt = _SUMMARY_PROMPT.format(
            title=doc.get("title") or card["source"],
            content=(doc.get("content") or "")[:content_chars],
        )
        batch.append(
            {"card_id": card["id"], "source": card["source"], "prompt": prompt, "summary": ""}
        )
    return batch


def ingest_card_summaries(db_path: Path, items: list[dict]) -> dict:
    """Fill cards from agent-written gists. Items: {card_id, summary}. Blank
    summaries are skipped (an unanswered card stays pending)."""
    counts = {"filled": 0, "skipped": 0, "missing": 0}
    for it in items:
        summary = (it.get("summary") or "").strip()
        if not summary:
            counts["skipped"] += 1
            continue
        if fill_card_summary(db_path, it["card_id"], summary):
            counts["filled"] += 1
        else:
            counts["missing"] += 1
    return counts
