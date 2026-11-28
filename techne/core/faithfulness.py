"""Faithfulness gate for merge/supersede candidates (techne#217 / coherence-layer D4).

TRUSTMEM (arxiv:2606.25161) shows naive LLM writes to memory produce 40%+ omission,
79% corruption, 50% hallucination.  techne's candidate-staging (review before promote)
is already the right instinct; this module adds a light **faithfulness check** on
merge/supersede candidates: does the replacement content preserve the load-bearing
claims of its source entries?

Design principles (from coherence-layer.md §D4 + issue #217):
- FLAG, not block. Unfaithful candidates are ANNOTATED and still go to the operator.
  techne never autonomously mutates or silently discards operator-owned knowledge.
- Resident-model, no-API-key. The same emit/ingest pattern as consolidate/dream:
  techne builds the prompt, an in-session agent answers it with its own model, then
  feeds the result back via faithfulness_ingest. No network/sampling calls here.
- Scope is LOCAL: only merge/supersede candidates from the local knowledge store.
  Federation (distiller-foreign-intake, cross-store paths) is out of scope (#221/#227).
- Lightweight coverage/preservation check — not a full semantic verifier.

Entry points:
  faithfulness_emit(config) → [{candidate_id, prompt}]   (prompts for the agent)
  faithfulness_ingest(config, candidate_id, response) → dict  (annotates candidate)
  annotate_faithfulness(db_path, candidate_id, *, ok, warning)  (direct write, tests)

The columns carrying the result (added in migration step 13):
  knowledge_candidates.faithfulness_ok  INTEGER  NULL=unchecked, 1=faithful, 0=unfaithful
  knowledge_candidates.faithfulness_warning  TEXT  NULL when ok/unchecked; reason otherwise
"""

from __future__ import annotations

import json
from pathlib import Path

from techne.config import Config
from techne.core._resident import extract_json_object
from techne.core.database import connect
from techne.core.redaction import redact_credentials

# Actions that have source rows whose claims must be preserved.
CHECKED_ACTIONS = frozenset({"merge", "supersede"})

# The gate must see what the merge judge saw, and the whole replacement (techne#1244):
# consolidation renders entries up to sleepdream.MAX_ENTRY_CHARS whole, and a merge of
# two of them can be twice as long.
MAX_SOURCE_CHARS = 4000
MAX_REPLACEMENT_CHARS = 8000

# Default cap on the emit batch, mirroring the sibling passes' bounded batches
# (consolidate_emit's max_clusters=20). Prevents an unbounded prompt batch on a
# large candidate backlog. Overridable per-call (and via the MCP tool arg).
DEFAULT_MAX_CANDIDATES = 50

FAITHFULNESS_PROMPT = """You are checking whether a proposed replacement knowledge entry
preserves all load-bearing claims from the original source entries it replaces.

A **load-bearing claim** is a specific, non-obvious fact a reader would rely on —
not generic framing or introductory prose.

# Source entries being replaced (id, content)
{sources}

# Proposed replacement
{replacement}

# Your task
Does the proposed replacement preserve every load-bearing claim from the sources?

Return ONLY a JSON object:
  {{
    "verdict": "faithful" | "unfaithful",
    "dropped_claims": "<brief summary of any dropped claims, or empty string>",
    "reason": "<one line: why faithful or which claims were dropped>"
  }}

Rules:
- "faithful": the replacement covers all load-bearing claims (it may reword or
  consolidate them, but none are omitted or inverted).
- "unfaithful": at least one load-bearing claim from a source is missing or
  contradicted in the replacement.
- Be conservative: mark "faithful" when the replacement is a reasonable consolidation
  even if it is shorter, as long as no specific fact is lost.
Output the JSON object and nothing else.
"""


def _render_sources(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        content = (r.get("content") or "").replace("\n", " ")
        if len(content) > MAX_SOURCE_CHARS:
            content = content[:MAX_SOURCE_CHARS] + "…"
        lines.append(f"- (#{r['id']}) {content}")
    return "\n".join(lines) if lines else "(no source entries found)"


def _fetch_source_rows(conn, target_ids: list[int]) -> list[dict]:
    """Fetch knowledge rows by id. Missing ids are silently skipped."""
    if not target_ids:
        return []
    placeholders = ",".join("?" * len(target_ids))
    rows = conn.execute(
        f"SELECT id, content FROM knowledge WHERE id IN ({placeholders})",
        target_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def build_faithfulness_prompt(source_rows: list[dict], replacement_content: str) -> str:
    """Render the faithfulness check prompt. Exported for testing."""
    repl = (replacement_content or "").replace("\n", " ")
    if len(repl) > MAX_REPLACEMENT_CHARS:
        repl = repl[:MAX_REPLACEMENT_CHARS] + "…"
    return FAITHFULNESS_PROMPT.format(
        sources=_render_sources(source_rows),
        replacement=repl,
    )


def parse_faithfulness_response(raw: str) -> dict | None:
    """Parse the agent's JSON verdict.

    Returns {"verdict": "faithful"|"unfaithful", "reason": str, "dropped_claims": str}
    or None if the response is unparseable/missing required fields.
    A None result is treated as UNCHECKED (not as unfaithful) so a bad response
    never auto-blocks a candidate.
    """
    obj = extract_json_object(raw or "")
    if not isinstance(obj, dict):
        return None
    verdict = obj.get("verdict")
    if verdict not in ("faithful", "unfaithful"):
        return None
    reason = obj.get("reason") or ""
    dropped = obj.get("dropped_claims") or ""
    return {"verdict": verdict, "reason": str(reason), "dropped_claims": str(dropped)}


# ---------------------------------------------------------------------------
# Emit — build prompts for pending merge/supersede candidates that haven't
# been faithfulness-checked yet.
# ---------------------------------------------------------------------------


def faithfulness_emit(
    config: Config, *, max_candidates: int = DEFAULT_MAX_CANDIDATES
) -> list[dict]:
    """Find pending merge/supersede candidates without a faithfulness check and
    return a prompt batch for the agent to answer with its own model.

    Returns ``[{"candidate_id": int, "prompt": str}]``. The agent answers each
    prompt and feeds it back via ``faithfulness_ingest`` with the same
    ``candidate_id``. Only candidates with ``action IN ('merge', 'supersede')``
    and ``faithfulness_ok IS NULL`` are included (already-checked candidates are
    skipped — the check is idempotent, not repeated).

    ``max_candidates`` caps the batch (default DEFAULT_MAX_CANDIDATES), mirroring
    the sibling passes' bounded emits — an unbounded batch on a large backlog would
    blow the agent's context. The SQL LIMIT is applied BEFORE the source-existence
    filter, so a batch may return fewer than ``max_candidates`` items; the newest
    candidates are prioritised (ORDER BY created_at DESC) and the rest resurface on
    the next emit once these are checked.
    """
    conn = connect(config.db_path)
    try:
        # Filter is `action IN ('merge','supersede')` — deliberately NOT 'retire'.
        # A 'retire' candidate only LINKS an older row to an existing survivor
        # (superseded_by) and writes NO synthesized content into the live store on
        # promote — there is nothing lossy to check for claim preservation. If a
        # future change ever wires a retire candidate's display `content` into a
        # live write, this filter MUST be revisited to include it (techne#217).
        rows = conn.execute(
            """SELECT id, action, content, target_ids
                 FROM knowledge_candidates
                WHERE status = 'pending'
                  AND action IN ('merge', 'supersede')
                  AND faithfulness_ok IS NULL
                ORDER BY created_at DESC
                LIMIT ?""",
            (max_candidates,),
        ).fetchall()
        batch = []
        for row in rows:
            row = dict(row)
            target_ids = json.loads(row["target_ids"] or "[]")
            if not target_ids:
                # No source ids → nothing to check; skip silently.
                continue
            source_rows = _fetch_source_rows(conn, target_ids)
            if not source_rows:
                # Sources already retired/missing — skip; don't flag.
                continue
            prompt = build_faithfulness_prompt(source_rows, row["content"])
            batch.append({"candidate_id": row["id"], "prompt": prompt})
    finally:
        conn.close()
    return batch


# ---------------------------------------------------------------------------
# Ingest — annotate the candidate with the agent's verdict.
# ---------------------------------------------------------------------------


def faithfulness_ingest(config: Config, candidate_id: int, response: str) -> dict:
    """Process the agent's faithfulness verdict for one candidate.

    Annotates the candidate row with ``faithfulness_ok`` (1/0) and
    ``faithfulness_warning`` (human-readable reason when unfaithful).
    A parse failure is recorded as UNCHECKED (ok=NULL unchanged) — a bad
    response NEVER auto-flags a candidate as unfaithful.

    Returns ``{"candidate_id": int, "ok": bool|None, "warning": str|None}``.
    Sovereignty invariant: the candidate status is NEVER changed here; only
    the faithfulness annotation columns are updated.
    """
    result: dict = {"candidate_id": candidate_id, "ok": None, "warning": None}
    verdict = parse_faithfulness_response(response)
    if verdict is None:
        # Unparseable — leave unchecked, report honestly.
        result["warning"] = "parse-error"
        return result

    ok = verdict["verdict"] == "faithful"
    warning: str | None = None
    if not ok:
        parts = [verdict["reason"]]
        if verdict["dropped_claims"]:
            parts.append(f"dropped: {verdict['dropped_claims']}")
        warning = " | ".join(p for p in parts if p)

    annotate_faithfulness(config.db_path, candidate_id, ok=ok, warning=warning)
    result["ok"] = ok
    result["warning"] = warning
    return result


def annotate_faithfulness(
    db_path: Path,
    candidate_id: int,
    *,
    ok: bool | None,
    warning: str | None,
) -> None:
    """Write the faithfulness annotation to the candidate row.

    ``ok=True`` → faithful (1), ``ok=False`` → unfaithful (0),
    ``ok=None`` → leave NULL (unchecked / inconclusive). Idempotent — safe
    to call multiple times; later calls overwrite earlier ones.

    This is the ONLY function that writes faithfulness_ok / faithfulness_warning.
    It never changes candidate status — sovereignty invariant (flag, not block).

    The warning is a new stored free-text column, so it is run through
    ``redact_credentials`` before being written (the redaction.py invariant: every
    new free-text column that reaches disk must be redacted at rest).
    """
    ok_int: int | None = None if ok is None else (1 if ok else 0)
    safe_warning = redact_credentials(warning) if warning else warning
    conn = connect(db_path)
    try:
        conn.execute(
            """UPDATE knowledge_candidates
                  SET faithfulness_ok = ?, faithfulness_warning = ?
                WHERE id = ?""",
            (ok_int, safe_warning, candidate_id),
        )
        conn.commit()
    finally:
        conn.close()
