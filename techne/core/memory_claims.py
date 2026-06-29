"""Claim-level memory reconciliation — `techne memory doctor --claims` (techne#107 PR3).

Extracts discrete claims from the curated memory snapshot and judges each against the raw
deed-log using the RESIDENT-MODEL emit/ingest pattern (the agent answers with its own model;
no API key, no MCP sampling). Mirrors the consolidate pass shape from sleepdream.py.

KEY HONESTY CONSTRAINT: the deed-log content is truncated to ~500 chars
(post_tool_use summaries), so file PATHS are reliably present but claim text is
lossy.  Corroboration is file-path + visible-snippet grade.  "uncorroborated" means
"no deed evidence found", NOT "false".  The report header and the prompt both state
this caveat.

Output is EPHEMERAL — no new table, no persistence.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from techne.config import Config
from techne.core._resident import extract_json_array
from techne.core._resident import prompt_hash as _prompt_hash
from techne.core.curated import live_curated_memory
from techne.core.state import deed_log
from techne.platforms.claude_code.paths import memory_project_slug

log = structlog.get_logger(__name__)

MEMORY_BUDGET = 8000  # max chars of curated-memory content fed to the prompt
DEED_DIGEST_CAP = 200  # deeds pulled AND shown per project-path candidate (newest-first)
# Pull == show: capping the pull at the digest size means every deed the model receives is
# accounted for, and hitting the cap is surfaced as report["deed_log_capped"] rather than
# silently dropping older deeds (which would read as false `uncorroborated`). Narrow the
# window with --since to fully cover a busy project.


FIDELITY_CAVEAT = (
    "FIDELITY CAVEAT: The deed-log contains truncated ~500-char summaries. "
    "File PATHS are reliably present, but claim-level text is lossy. "
    "'uncorroborated' means no deed evidence was found — NOT that the claim is "
    "false. Be conservative."
)


CLAIMS_PROMPT = """\
You are reviewing a curated memory snapshot against a raw deed-log.

{fidelity_caveat}

# Curated Memory
{memory_content}

# Deed-Log Digest ({deed_count} deed(s) considered{capped_note})
{deed_digest}

# Your Task
Extract every discrete factual claim from the curated memory above. For each claim,
judge it against the deed evidence:
  corroborated  — a deed in the digest supports or is consistent with this claim
  uncorroborated — no deed evidence found (NOT necessarily false — deed-log is lossy)
  contradicted  — a deed directly conflicts with this claim

Return ONLY a JSON array. Each item must be:
  {{"claim": "<one sentence quoted or paraphrased from the memory>",
    "status": "corroborated" | "uncorroborated" | "contradicted",
    "evidence": "<which deed(s) support or contradict this, or 'none'>"}}

Output the JSON array and nothing else.
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deed_digest(deeds: list[dict], *, max_deeds: int = 200, snippet: int = 200) -> str:
    """Render the deed-log into a compact corroboration digest.

    One line per deed: ``- [tool_name] <first snippet chars, newlines collapsed>``.
    Capped at max_deeds (newest-first as deed_log returns them); if capped, appends
    a ``... (N more elided)`` line.
    """
    lines: list[str] = []
    for deed in deeds[:max_deeds]:
        tool = deed.get("tool_name") or "?"
        content = (deed.get("content") or "").replace("\n", " ")
        if len(content) > snippet:
            content = content[:snippet]
        lines.append(f"- [{tool}] {content}")
    elided = len(deeds) - max_deeds
    if elided > 0:
        lines.append(f"... ({elided} more elided)")
    return "\n".join(lines)


def _build_memory_block(memory_rows: list[dict]) -> str:
    """Concatenate curated-memory rows into a prompt block, capped at MEMORY_BUDGET."""
    parts: list[str] = []
    total = 0
    for row in memory_rows:
        header = f"## {row.get('title') or row['source_path']} ({row['source_path']})\n"
        body = (row.get("content") or "").strip()
        entry = header + body + "\n\n"
        if total + len(entry) > MEMORY_BUDGET:
            remaining = MEMORY_BUDGET - total - len(header) - 30
            if remaining > 50:
                entry = header + body[:remaining] + "\n...[truncated]\n\n"
                parts.append(entry)
            else:
                parts.append(f"\n...[additional memory files truncated at {MEMORY_BUDGET} chars]\n")
            break
        parts.append(entry)
        total += len(entry)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def claims_emit(
    config: Config,
    *,
    cwd: Path | None = None,
    since_days: int | None = None,
) -> dict:
    """Build the claim-reconciliation prompt for the resident model.

    Returns a dict with:
      memory_present  — False when no curated memory is found for this project
      prompt          — the full prompt (empty string when memory_present=False)
      prompt_hash     — sha256[:16] of the prompt (empty string when absent)
      deed_count      — number of deeds considered (pulled == shown)
      deed_log_capped — True when the pull cap was hit (older deeds omitted; narrow --since)
      memory_files    — list of source_path strings ([] when memory_present=False)
    """
    cwd = Path(cwd) if cwd else Path.cwd()
    slug = memory_project_slug(cwd)

    # Auto-drain CC's memory files into curated_memory FIRST, so the claim-gate reads a
    # fresh drain. Without this the drain only fills on a manual `techne memory sync`, and
    # --claims would falsely report "no curated memory" when files sit on disk where
    # `memory doctor` (which reads disk) just found them (staff-review C1/C2). Best-effort:
    # a drain failure falls back to whatever is already stored. Idempotent (content-hash).
    try:
        from techne.sync.cc_memory import CCMemorySource

        CCMemorySource(config, cwd=cwd).initial_walk()
    except Exception as exc:
        # warning (not debug): a recurring drain failure means --claims silently judges a
        # stale/empty drain — surface it without changing the degrade-to-read behavior.
        log.warning("claims_emit_drain_failed", slug=slug, error=str(exc))

    memory_rows = live_curated_memory(config.db_path, slug=slug)
    if not memory_rows:
        log.debug("claims_emit_no_memory", slug=slug)
        return {
            "memory_present": False,
            "prompt": "",
            "prompt_hash": "",
            "deed_count": 0,
            "deed_log_capped": False,
            "memory_files": [],
        }

    # Scope deeds to this project — union both resolved and raw cwd paths, mirroring
    # memory_doctor.reconcile's candidate scoping (the capture hook may store either form).
    # capped: a candidate hit the pull cap, so older deeds were not considered — surfaced
    # (not silently dropped) so an uncorroborated claim isn't mistaken for a false one.
    candidates = {str(cwd), str(cwd.resolve())}
    deeds_by_id: dict[int, dict] = {}
    capped = False
    for proj in candidates:
        batch = deed_log(
            config.db_path, project_path=proj, since_days=since_days, limit=DEED_DIGEST_CAP
        )
        if len(batch) >= DEED_DIGEST_CAP:
            capped = True
        for d in batch:
            deeds_by_id[d["id"]] = d
    deed_list = list(deeds_by_id.values())
    # Two path candidates each pulled up to DEED_DIGEST_CAP, so their union can be up
    # to 2× the cap when cwd != cwd.resolve() (macOS /tmp→/private/tmp) and deeds are
    # stored under both forms — breaking the "pull == show" budget the digest assumes.
    # Clamp to the cap, keeping the most recent (highest id) deeds. (techne#170)
    if len(deed_list) > DEED_DIGEST_CAP:
        capped = True
        deed_list.sort(key=lambda d: d["id"], reverse=True)
        deed_list = deed_list[:DEED_DIGEST_CAP]

    memory_content = _build_memory_block(memory_rows)
    # Show every pulled deed (pull is already bounded by DEED_DIGEST_CAP).
    digest = _deed_digest(deed_list, max_deeds=len(deed_list) or 1)
    capped_note = " — CAPPED, older deeds omitted; narrow with --since" if capped else ""

    prompt = CLAIMS_PROMPT.format(
        fidelity_caveat=FIDELITY_CAVEAT,
        memory_content=memory_content,
        deed_count=len(deed_list),
        capped_note=capped_note,
        deed_digest=digest if digest else "(no deeds found for this project)",
    )

    prompt_hash = _prompt_hash(prompt)

    log.debug(
        "claims_emit",
        slug=slug,
        memory_files=len(memory_rows),
        deed_count=len(deed_list),
        deed_log_capped=capped,
        prompt_hash=prompt_hash,
    )
    return {
        "memory_present": True,
        "prompt": prompt,
        "prompt_hash": prompt_hash,
        "deed_count": len(deed_list),
        "deed_log_capped": capped,
        "memory_files": [r["source_path"] for r in memory_rows],
    }


def parse_claims_response(raw: str) -> list[dict]:
    """Tolerant JSON-array parse of claim judgements.

    Mirrors reflect.parse_response: strips markdown fences, finds first ``[``
    … last ``]``, json.loads.  Validates each item: ``claim`` non-empty str,
    ``status`` in {corroborated, uncorroborated, contradicted} (else skip),
    ``evidence`` coerced to str.  Returns [] on any parse failure.
    """
    VALID_STATUSES = {"corroborated", "uncorroborated", "contradicted"}

    items = extract_json_array(raw)

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = item.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            continue
        status = item.get("status")
        if status not in VALID_STATUSES:
            continue
        evidence = str(item.get("evidence") or "")
        out.append({"claim": claim.strip(), "status": status, "evidence": evidence})
    return out


def claims_ingest(config: Config, prompt_hash: str, response: str) -> dict:
    """Parse the resident model's claim-judgement response and return an ephemeral report.

    Args:
        config:      techne Config (accepted for symmetry; not used for persistence).
        prompt_hash: sha256[:16] of the emitted prompt (advisory; not persisted).
        response:    Raw string response from the agent containing a JSON claim array.

    Returns a dict:
      claims  — list of validated claim dicts
      counts  — {corroborated, uncorroborated, contradicted}
      total   — len(claims)
    """
    claims = parse_claims_response(response)
    counts: dict[str, int] = {"corroborated": 0, "uncorroborated": 0, "contradicted": 0}
    for c in claims:
        counts[c["status"]] += 1

    log.debug("claims_ingest", prompt_hash=prompt_hash, total=len(claims), counts=counts)
    return {
        "claims": claims,
        "counts": counts,
        "total": len(claims),
        # Carry the honesty framing alongside the data so a --json consumer sees it too.
        "caveat": FIDELITY_CAVEAT,
    }
