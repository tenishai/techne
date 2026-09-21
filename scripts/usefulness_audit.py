#!/usr/bin/env python3
"""Would memory have prevented the operator's corrections? (techne#995)

`techne audit`'s RECALL ATTRIBUTION measures whether injected rows get cited, and
splits that by origin (reflect / consolidate / hand). It cannot say whether the
store held what mattered. This asks the outcome question offline: find user
prompts that look like corrections, pull the knowledge rows written BEFORE each
one that best match it, and mark whether each was injected earlier in that
session.

  corrections-emit    write a judging file, one record per distinct candidate.
                      Whether a row "had the answer" is a judgment, not a regex,
                      so a person or agent labels each record.
  corrections-report  read the verdicts back and split real corrections into
                      coverage failures (no row had it), surfacing failures (a row
                      had it, never injected) and use failures (injected, and the
                      correction came anyway), with the answering row's origin.

Verdict files quote operator messages; keep them out of the repo. Transcripts
are Claude Code jsonl under ~/.claude/projects; both logs are UTC.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from techne.core.utilization import knowledge_origin

log = logging.getLogger("usefulness_audit")

DEFAULT_DB = Path.home() / ".techne" / "techne.db"
DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"

#: Distinctive terms per correction used to query the store, and rows kept.
QUERY_TERMS = 10
TOP_K = 5
#: Chars of the preceding reply that, with the prompt, identify a replayed turn.
REPLAY_TAIL = 300

_TOKEN = re.compile(r"[a-z0-9_][a-z0-9_.\-/]{3,}")
_STOP = frozenset(
    "that this with from have were been will would should could their there then than "
    "when what which while where into over also only just like more most some such very "
    "they them does done make made each every because about after before other these those "
    "your yours ours here being must still even much many used using uses user agent".split()
)

# Corrections: short, direct pushback. Deliberately broad; the judging pass is
# the precision backstop, and `not_a_correction` is a valid verdict.
_CORRECTION = re.compile(
    r"(^\s*(no|nope|wrong|incorrect|stop)\b|\bthat'?s not\b|\bthat is not\b|\bnot what i\b"
    r"|\byou('| a)re (wrong|mistaken)\b|\bi (said|told you|asked)\b|\bwhy did you\b"
    r"|\byou missed\b|\bdidn'?t i\b|\bactually,|\bcorrection\b|\bwe (already|don'?t)\b)",
    re.IGNORECASE,
)


def tokens(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall(text.lower()):
        t = t.strip(".-/")
        if len(t) >= 4 and not t.isdigit() and t not in _STOP:
            out.append(t)
    return out


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def rare_terms(text: str, df: Counter, k: int = QUERY_TERMS) -> list[str]:
    """The text's k rarest distinct terms across the corpus (ties broken by term)."""
    uniq = set(tokens(text))
    return sorted(uniq, key=lambda t: (df.get(t, 0), t))[:k]


def parse_ts(s: str) -> float:
    """Injection 'YYYY-MM-DD HH:MM:SS' or transcript ISO-Z, both UTC, to epoch."""
    s = s.strip().replace("Z", "+00:00")
    if "T" not in s:
        s = s.replace(" ", "T") + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()


# --------------------------------------------------------------------------- transcripts


@dataclass
class Turn:
    """One real user prompt and everything the assistant wrote before the next one."""

    ts: float
    prompt: str
    reply: list[str] = field(default_factory=list)

    @property
    def reply_text(self) -> str:
        return "\n".join(self.reply)


def _real_prompt(content) -> str | None:
    """User text typed by a person, or None for tool results and harness noise."""
    if isinstance(content, str):
        text = content
    else:
        if any(b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return None
    return text


def _assistant_parts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    parts = []
    for b in content:
        kind = b.get("type")
        if kind == "text":
            parts.append(b.get("text", ""))
        elif kind == "thinking":
            parts.append(b.get("thinking", ""))
        elif kind == "tool_use":
            parts.append(json.dumps(b.get("input", {}), ensure_ascii=False))
    return parts


def load_turns(path: Path) -> list[Turn]:
    turns: list[Turn] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("isSidechain") or rec.get("type") not in ("user", "assistant"):
                continue
            msg, ts = rec.get("message") or {}, rec.get("timestamp")
            if not ts:
                continue
            if rec["type"] == "user":
                prompt = _real_prompt(msg.get("content", ""))
                if prompt is not None:
                    turns.append(Turn(parse_ts(ts), prompt))
            elif turns:
                turns[-1].reply.extend(_assistant_parts(msg.get("content", [])))
    return turns


def index_transcripts(projects: Path, session_ids: set[str]) -> dict[str, list[Turn]]:
    found: dict[str, list[Turn]] = {}
    for path in projects.glob("*/*.jsonl"):
        if path.stem in session_ids and path.stem not in found:
            found[path.stem] = load_turns(path)
    log.debug("transcripts: %d of %d sessions found", len(found), len(session_ids))
    return found


# --------------------------------------------------------------------------- store


def load_store(db: Path):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = {
        r["id"]: dict(r)
        for r in conn.execute("SELECT id, content, source, created_at FROM knowledge")
    }
    injections = [
        dict(r)
        for r in conn.execute(
            "SELECT id, source, session_id, timestamp, hit_ids FROM context_injections "
            "WHERE hit_ids IS NOT NULL AND session_id IS NOT NULL ORDER BY timestamp"
        )
    ]
    for inj in injections:
        try:
            inj["hit_ids"] = [int(x) for x in json.loads(inj["hit_ids"])]
        except (ValueError, TypeError):
            inj["hit_ids"] = []
        inj["ts"] = parse_ts(inj["timestamp"])
    df: Counter = Counter()
    for r in rows.values():
        df.update(set(tokens(r["content"] or "")))
    return conn, rows, injections, df


# --------------------------------------------------------------------------- corrections


def _fts_query(terms: list[str]) -> str:
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms)


def run_corrections_emit(db: Path, projects: Path, out: Path, top_k: int = TOP_K) -> int:
    conn, rows, injections, df = load_store(db)
    sessions = {p.stem for p in projects.glob("*/*.jsonl")}
    turns_by_session = index_transcripts(projects, sessions)
    inj_by_session: dict[str, list[dict]] = defaultdict(list)
    for inj in injections:
        inj_by_session[inj["session_id"]].append(inj)

    # Resumed/forked sessions copy earlier turns into a new transcript with new
    # timestamps, so the same correction appears more than once. Keep the earliest.
    # The key is the prompt AND the tail of the reply it answered: stock pushback
    # ("no, that's wrong") is typed word-for-word in unrelated sessions, and keying
    # on the prompt alone merged those into one record (Prudence, PR #996 r1). A
    # replay copies the preceding reply too. Record uuids are no substitute: a
    # re-created transcript assigns new ones, so they miss real replays.
    found: dict[tuple[str, str], tuple[float, str, list[Turn], int]] = {}
    for sid, turns in turns_by_session.items():
        for i, turn in enumerate(turns):
            if i == 0 or len(turn.prompt) > 600 or not _CORRECTION.search(turn.prompt):
                continue
            key = (_norm(turn.prompt), _norm(turns[i - 1].reply_text[-REPLAY_TAIL:]))
            if key not in found or turn.ts < found[key][0]:
                found[key] = (turn.ts, sid, turns, i)
    log.debug("corrections: %d distinct after dedup", len(found))

    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for _, sid, turns, i in sorted(found.values(), key=lambda f: f[0]):
            turn = turns[i]
            prior = turns[i - 1].reply_text[-1500:]
            terms = rare_terms(turn.prompt + "\n" + prior, df)
            if not terms:
                continue
            when = datetime.fromtimestamp(turn.ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            try:
                hits = conn.execute(
                    "SELECT k.id FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid "
                    "WHERE knowledge_fts MATCH ? AND k.created_at < ? "
                    "ORDER BY bm25(knowledge_fts) LIMIT ?",
                    (_fts_query(terms), when, top_k),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                log.debug("fts failed for %s@%s: %s", sid, when, exc)
                hits = []
            seen_before = {
                rid
                for inj in inj_by_session.get(sid, [])
                if inj["ts"] < turn.ts
                for rid in inj["hit_ids"]
            }
            rec = {
                "key": f"{sid}@{when}",
                "session": sid,
                "ts": when,
                "correction": turn.prompt,
                "prior_reply_tail": prior[-600:],
                "candidates": [
                    {
                        "id": h["id"],
                        "origin": knowledge_origin(rows[h["id"]]["source"]),
                        "created_at": rows[h["id"]]["created_at"],
                        "injected_before": h["id"] in seen_before,
                        "content": (rows[h["id"]]["content"] or "")[:400],
                    }
                    for h in hits
                ],
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    log.info("corrections: wrote %d candidates to %s", n, out)
    return n


def summarize_corrections(emitted: list[dict], verdicts: dict[str, dict]) -> dict:
    """Split judged corrections into coverage, surfacing and use failures.

    Verdict per key: {"verdict": "covered" | "not_covered" | "not_a_correction",
    "answer_id": int | None}.

    When the answering row is not among the emitted candidates, the verdict must
    also carry "answer_origin" and "injected_before" (checked by hand)."""
    out = Counter()
    by_origin = Counter()
    for rec in emitted:
        v = verdicts.get(rec["key"])
        if v is None:
            out["unjudged"] += 1
            continue
        verdict = v.get("verdict")
        if verdict == "not_a_correction":
            out["not_a_correction"] += 1
            continue
        out["corrections"] += 1
        if verdict == "not_covered":
            out["coverage_failure"] += 1
            continue
        cand = next((c for c in rec["candidates"] if c["id"] == v.get("answer_id")), None)
        if cand is None and "answer_origin" in v and "injected_before" in v:
            # The judge found the answering row outside the FTS top-k (a retrieval
            # miss by this script, not by techne) and recorded its facts directly.
            cand = {"origin": v["answer_origin"], "injected_before": v["injected_before"]}
            out["answer_outside_candidates"] += 1
        if cand is None:
            out["bad_verdict"] += 1
            continue
        by_origin[cand["origin"]] += 1
        out["use_failure" if cand["injected_before"] else "surfacing_failure"] += 1
    return {"counts": dict(out), "answering_row_origin": dict(by_origin)}


def run_corrections_report(emitted_path: Path, verdicts_path: Path) -> dict:
    emitted = [json.loads(ln) for ln in emitted_path.open(encoding="utf-8") if ln.strip()]
    verdicts = {}
    for line in verdicts_path.open(encoding="utf-8"):
        if line.strip():
            v = json.loads(line)
            verdicts[v["key"]] = v
    return summarize_corrections(emitted, verdicts)


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("corrections-emit")
    e.add_argument("out", type=Path)
    r = sub.add_parser("corrections-report")
    r.add_argument("emitted", type=Path)
    r.add_argument("verdicts", type=Path)
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    if args.cmd == "corrections-emit":
        run_corrections_emit(args.db, args.projects, args.out)
    else:
        print(json.dumps(run_corrections_report(args.emitted, args.verdicts), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
