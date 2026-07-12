"""Content-addressed spawn contracts — drift detection for agent handoffs.

Before an orchestrator spawns a worker (subagent / persona / workflow stage), it
FREEZES a contract of exactly what the worker is authoritatively given — the inputs
and the *actual prompt text* — content-addressed by SHA-256. The frozen contract is
appended to a `template_id`-keyed ledger. When a template's prompt or input-shape
silently drifts between spawns, `record_spawn` flags it — turning invisible
orchestrator↔worker drift (the data shape changes in a release but the worker prompt
still describes the old one, and nothing errors) into a detectable event.

Lifted from okfy's "frozen worker-job artifact" (architect pattern dcefb4b4): trust
flows from a hash of what was *really* handed over, not the orchestrator's self-report.

Prototype: this is the primitive + a JSONL ledger. A caller (e.g. a cabal persona
handoff, or a techne subagent spawn) invokes `freeze()` at spawn time and
`record_spawn()` to log + drift-check. Not yet wired into a live spawn path — techne
has none today; cabal's persona handoffs are the intended first integration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: object) -> str:
    """Stable serialization so identical inputs always hash identically (sorted keys,
    tight separators, non-JSON values coerced via str)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class SpawnContract:
    """A frozen record of what a spawned worker is authoritatively given.

    `digest` is content-addressed over (template_id, prompt_hash, inputs_hash) — it is
    deterministic and timestamp-free, so identical handoffs collide by design and any
    change to the prompt text or the input shape changes the digest.
    """

    template_id: str  # stable name for the prompt-template / handoff kind
    prompt_hash: str  # sha256 of the actual prompt text handed to the worker
    inputs_hash: str  # sha256 of the canonical inputs
    digest: str  # sha256 over (template_id, prompt_hash, inputs_hash)
    label: str | None = None  # optional per-spawn label (e.g. the URL / task)
    agent_type: str | None = None  # optional worker kind (persona / subagent_type)


def freeze(
    template_id: str,
    prompt_text: str,
    inputs: object = None,
    *,
    label: str | None = None,
    agent_type: str | None = None,
) -> SpawnContract:
    """Freeze the spawn contract for one handoff. Pure and deterministic — no I/O, no
    clock; the same (template_id, prompt_text, inputs) always yields the same digest."""
    prompt_hash = _sha(prompt_text)
    inputs_hash = _sha(_canonical(inputs if inputs is not None else {}))
    digest = _sha("\x1f".join((template_id, prompt_hash, inputs_hash)))
    return SpawnContract(
        template_id=template_id,
        prompt_hash=prompt_hash,
        inputs_hash=inputs_hash,
        digest=digest,
        label=label,
        agent_type=agent_type,
    )


def _iter_ledger(ledger_path: Path):
    if not ledger_path.exists():
        return
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn final line rather than crash the reader


def _last_for_template(ledger_path: Path, template_id: str) -> dict | None:
    last = None
    for row in _iter_ledger(ledger_path):
        if row.get("template_id") == template_id:
            last = row
    return last


def _drift(prior: dict | None, contract: SpawnContract) -> dict:
    """Compare this contract against the most recent prior one for the same template."""
    if prior is None:
        return {"first_seen": True, "prompt_changed": False, "inputs_changed": False, "changed": False}
    prompt_changed = prior.get("prompt_hash") != contract.prompt_hash
    inputs_changed = prior.get("inputs_hash") != contract.inputs_hash
    return {
        "first_seen": False,
        "prompt_changed": prompt_changed,
        "inputs_changed": inputs_changed,
        "changed": prompt_changed or inputs_changed,
    }


def record_spawn(
    ledger_path: Path, contract: SpawnContract, *, recorded_at: str | None = None
) -> dict:
    """Append *contract* to the append-only JSONL ledger and drift-check it against the
    most recent prior contract for the same `template_id`. Returns a drift report:
    `{digest, prior_digest, drift: {first_seen, prompt_changed, inputs_changed, changed}}`.

    A `changed: True` means the template's prompt or inputs shifted since the last spawn
    of this template_id — the signal that an orchestrator↔worker contract drifted.
    `recorded_at` is optional ledger metadata only; it is NOT part of the digest.
    """
    prior = _last_for_template(ledger_path, contract.template_id)
    drift = _drift(prior, contract)
    row = {**asdict(contract), "recorded_at": recorded_at}
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(_canonical(row) + "\n")
    return {
        "digest": contract.digest,
        "prior_digest": prior.get("digest") if prior else None,
        "drift": drift,
    }


def spawn_history(ledger_path: Path, template_id: str | None = None) -> list[dict]:
    """All recorded contracts (optionally scoped to one template_id), oldest first."""
    return [
        row
        for row in _iter_ledger(ledger_path)
        if template_id is None or row.get("template_id") == template_id
    ]
