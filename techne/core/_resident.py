"""Shared JSON extraction and prompt-hash utilities for resident-model round-trips.

Three passes (reflect, sleepdream, memory_claims) share the same tolerant-JSON-parse
pattern: strip ``` fences, locate the first/last bracket pair, json.loads. This module
centralises that boilerplate without touching the divergent post-parse validation steps
in each caller.
"""

import hashlib
import json


def extract_json_array_or_none(raw: str) -> list | None:
    """Tolerant extraction of a JSON array, or None when there ISN'T ONE.

    Same tolerance as ``extract_json_array`` — strips markdown fences, finds the
    first '[' and last ']', json.loads the slice — but it DISTINGUISHES the two
    outcomes that the list-returning version conflates: a successfully parsed
    EMPTY array (``[]``) and a response with no parseable array at all (``None``).

    That collapse is what made a discarded reflection indistinguishable from a
    model that genuinely had nothing to say (techne#465). Callers that need to
    tell those apart must use this; ``extract_json_array`` remains for callers
    that legitimately do not care.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(ln for ln in lines[1:] if not ln.startswith("```")).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end < start:
        return None
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list):
        return None
    return items


def extract_json_array(raw: str) -> list:
    """Tolerant extraction of a JSON array from raw LLM output.

    Returns [] on any parse failure, missing brackets, or if the parsed value is
    not a list. ONE IMPLEMENTATION, delegating — a second copy of the fence/slice
    logic would drift from this one, and the tolerance is the whole value here.
    """
    items = extract_json_array_or_none(raw)
    return [] if items is None else items


def extract_json_object(raw: str) -> dict:
    """Tolerant extraction of a JSON object from raw LLM output.

    Strips markdown code fences (``` or ```json), finds the first '{' and last '}',
    and json.loads the slice. Returns {} on any parse failure, missing brackets, or
    if the parsed value is not a dict.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(ln for ln in lines[1:] if not ln.startswith("```")).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end < start:
        return {}
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    return obj


def prompt_hash(text: str) -> str:
    """sha256[:16] hex digest of text, encoded as UTF-8.

    Stable identity for a rendered prompt — used to dedup and trace
    knowledge_candidates back to the prompt that generated them.
    Equivalent to ``hashlib.sha256(text.encode()).hexdigest()[:16]``.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:16]
