"""LongMemEval-S retrieval against an isolated techne store, one store per question.

    python tests/eval/longmemeval/run.py --data longmemeval_s_cleaned.json --out OUT \
        [--arm fts|hybrid] [--max-questions N]

Each question's haystack becomes its own techne.db under a temp dir: every haystack
session is a `sessions` row, every turn an `observations` row ("role: content",
event_at from the session date). The question is the search() query. Nothing reads
or writes ~/.techne: TECHNE_HOME points at the temp dir before techne is imported,
and every db path is checked to be under it.

Writes OUT/results.jsonl (one row per question) and OUT/summary.md. Run it on a
build host (mash1-run), not a dev machine: the full set is 470 stores of ~500 turns.
The data comes from Hugging Face xiaowu0162/longmemeval-cleaned and is never
committed here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lme_metrics import SESSION_KS, TURN_KS, score_question  # noqa: E402

log = logging.getLogger("longmemeval")

# "2023/05/20 (Sat) 02:21" -> "2023-05-20 02:21:00"
_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2}) \(\w+\) (\d{2}):(\d{2})")


def iso_date(s: str) -> str | None:
    m = _DATE.match(s or "")
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:00" if m else None


def load_haystack(db_path: Path, item: dict) -> tuple[dict[int, tuple[str, str]], set[str], int]:
    """Write one question's haystack.

    Returns observation id -> (session, turn), the gold turns, and how many repeated
    sessions were skipped. Some LongMemEval-S haystacks list a session id twice; the
    repeat is not stored again, but its evidence flags still count as gold.
    """
    from techne.core.database import connect, init_db

    init_db(db_path).close()
    rows: dict[int, tuple[str, str]] = {}
    gold_turns: set[str] = set()
    seen: set[str] = set()
    repeats = 0
    conn = connect(db_path)
    try:
        for sid, date, turns in zip(
            item["haystack_session_ids"],
            item["haystack_dates"],
            item["haystack_sessions"],
            strict=True,
        ):
            gold_turns |= {f"{sid}_{i}" for i, t in enumerate(turns) if t.get("has_answer")}
            if sid in seen:
                repeats += 1
                continue
            seen.add(sid)
            when = iso_date(date)
            conn.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, COALESCE(?, CURRENT_TIMESTAMP))",
                (sid, when),
            )
            for i, turn in enumerate(turns):
                tid = f"{sid}_{i}"
                cur = conn.execute(
                    "INSERT INTO observations (session_id, content, tool_name, event_at)"
                    " VALUES (?, ?, ?, ?)",
                    (sid, f"{turn['role']}: {turn['content']}", "longmemeval", when),
                )
                rows[cur.lastrowid] = (sid, tid)
        conn.commit()
    finally:
        conn.close()
    return rows, gold_turns, repeats


def run_question(root: Path, item: dict, arm: str, limit: int) -> dict:
    from techne.core.search import backfill_vectors, search

    db_path = root / item["question_id"] / "techne.db"
    db_path.parent.mkdir(parents=True)
    assert db_path.resolve().is_relative_to(root.resolve()), db_path

    t0 = time.monotonic()
    rows, gold_turns, repeats = load_haystack(db_path, item)
    if arm == "hybrid":
        backfill_vectors(db_path, allow_download=True)
    t1 = time.monotonic()
    # Observation recency always decays at a fixed 30-day half-life from now
    # (_rerank calls _recency_score(ts) without a half-life; search()'s half_life_days
    # is knowledge-confidence decay only). With 2023 dates that term is under 1e-9 for
    # every row, so the FTS order here is effectively BM25.
    results = search(
        db_path,
        item["question"],
        table="observations",
        limit=limit,
        use_vectors=arm == "hybrid",
    )
    t2 = time.monotonic()

    ranked = [
        rows[r["id"]]
        for r in results
        if r.get("source_table") == "observation" and r.get("id") in rows
    ]
    metrics = score_question(ranked, set(item["answer_session_ids"]), gold_turns)
    return {
        "question_id": item["question_id"],
        "question_type": item["question_type"],
        "n_turns": len(rows),
        "repeated_sessions": repeats,
        "n_results": len(ranked),
        "load_s": round(t1 - t0, 3),
        "search_s": round(t2 - t1, 3),
        "metrics": metrics,
    }


def summarize(out: list[dict], arm: str, limit: int) -> str:
    names = [("session", f"{m}@{k}") for k in SESSION_KS for m in ("recall_all", "ndcg_any")]
    names += [("turn", f"{m}@{k}") for k in TURN_KS for m in ("recall_all", "ndcg_any")]

    def mean(rows: list[dict], level: str, name: str) -> float:
        return statistics.fmean(r["metrics"][level][name] for r in rows)

    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in out:
        by_type[r["question_type"]].append(r)
    groups = [("all", out), *sorted(by_type.items())]

    head = "| group | n | " + " | ".join(f"{lvl} {n}" for lvl, n in names) + " |"
    lines = [
        f"# LongMemEval-S retrieval, arm `{arm}`",
        "",
        f"{len(out)} questions (abstention dropped), search(limit={limit}, table='observations'). "
        "Recency adds ~0 to every row (2023 dates, fixed 30-day half-life). "
        "Sessions are turn hits collapsed in first-hit order.",
        f"Zero-result queries: {sum(1 for r in out if r['n_results'] == 0)}. "
        f"Repeated sessions skipped: {sum(r['repeated_sessions'] for r in out)}. "
        f"Median load {statistics.median(r['load_s'] for r in out):.2f}s, "
        f"median search {statistics.median(r['search_s'] for r in out):.3f}s.",
        "",
        head,
        "|" + "---|" * (len(names) + 2),
    ]
    for g, rows in groups:
        lines.append(
            f"| {g} | {len(rows)} | "
            + " | ".join(f"{mean(rows, lvl, n):.3f}" for lvl, n in names)
            + " |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--arm", choices=("fts", "hybrid"), default="fts")
    ap.add_argument("--limit", type=int, default=100, help="search() limit; >=50 to fill turn@50")
    ap.add_argument("--max-questions", type=int, default=0, help="0 = all")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    with tempfile.TemporaryDirectory(prefix="techne-lme-") as tmp:
        root = Path(tmp)
        # Before any techne import, so config and the default store resolve here.
        os.environ["TECHNE_HOME"] = str(root / "home")
        items = [
            x for x in json.loads(args.data.read_text()) if not x["question_id"].endswith("_abs")
        ]
        if args.max_questions:
            items = items[: args.max_questions]
        args.out.mkdir(parents=True, exist_ok=True)
        out: list[dict] = []
        with (args.out / "results.jsonl").open("w") as fh:
            for n, item in enumerate(items, 1):
                r = run_question(root, item, args.arm, args.limit)
                out.append(r)
                fh.write(json.dumps(r) + "\n")
                log.debug("%s %s", r["question_id"], r["metrics"]["session"])
                if n % 25 == 0:
                    log.info("%d/%d questions", n, len(items))
    (args.out / "summary.md").write_text(summarize(out, args.arm, args.limit))
    print((args.out / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
