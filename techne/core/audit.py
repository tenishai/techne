"""Audit utilities for the techne observation ledger (techne#270).

Provides verification helpers that let a holder of an original (pre-redaction)
payload prove it was present without re-exposing the plaintext. The core
invariant: when redaction fires, a SHA-256 hash of the RAW content is stored
alongside the redacted text. This module lets callers verify a candidate
plaintext against that stored hash.

Invariant scope (techne#270):
  - add_observation() (techne/core/state.py): FULL invariant — content_hash IS
    NOT NULL iff redaction fired.
  - transcript_import.py and jsonl_transcript.py importer paths: FULL invariant
    — backfilled on techne#270 to compute and store content_hash alongside every
    redacted INSERT, matching add_observation's behaviour.

Placement rationale: verify_observation_hash is a read-only, audit-path
function with no write-side coupling to the hot observation ingest path.
Keeping it here (rather than in state.py) avoids cluttering the ingest path
with audit logic and gives a clean home for future audit tooling (e.g.,
bulk verification, audit log exports).
"""

import hashlib
from pathlib import Path

from techne.core.database import connect


def verify_observation_hash(db_path: Path, obs_id: int, candidate_plaintext: str) -> bool:
    """Return True iff candidate_plaintext hashes to the stored content_hash.

    The stored hash is set only when redaction fired (content_hash IS NOT NULL).
    Returns False for observations where content_hash is NULL — either no
    redaction occurred (nothing to prove) or the row predates techne#270.

    Args:
        db_path: Path to the techne SQLite database.
        obs_id: Primary key of the observation row to check.
        candidate_plaintext: The raw (pre-redaction) text to verify.

    Returns:
        True  — candidate hashes to the stored content_hash (proven match).
        False — no hash stored for this observation, OR hash does not match.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT content_hash FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None or row["content_hash"] is None:
        # Either the row doesn't exist, or no redaction occurred (nothing to prove).
        return False

    # Guard: a non-str candidate (e.g. None) must return False, not raise.
    # The contract is "verifier always returns bool" — callers should not need
    # to handle TypeError from an unexpected argument type. (techne#270)
    if not isinstance(candidate_plaintext, str):
        return False

    candidate_hash = hashlib.sha256(candidate_plaintext.encode()).hexdigest()
    return candidate_hash == row["content_hash"]
