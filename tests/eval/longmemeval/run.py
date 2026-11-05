"""LongMemEval-S retrieval against an isolated techne store, one store per question.

    python tests/eval/longmemeval/run.py --data longmemeval_s_cleaned.json --out OUT \
        [--arm fts|hybrid|compare] [--max-questions N]

Each question's haystack becomes its own techne.db under a temp dir: every haystack
session is a `sessions` row, every turn an `observations` row ("role: content",
event_at from the session date). The question is the search() query. Nothing reads
or writes ~/.techne: TECHNE_HOME points at the temp dir before techne is imported,
and every db path is checked to be under it.

Writes OUT/results.jsonl (one row per question) and OUT/summary.md. Run it on a
build host, not a dev machine: the full set is 470 stores of ~500 turns. The data
comes from Hugging Face xiaowu0162/longmemeval-cleaned and is never committed here.

The metric names are upstream's, but the corpus, gold and session rule are this
harness's own (see lme_metrics.py), so the numbers do not compare with upstream's.

`--arm hybrid` refuses to report unless every turn was embedded and the vector
channel contributed results: search() falls back to FTS without a backend, and
would otherwise publish FTS numbers under a hybrid label.

`--arm compare` embeds each haystack once and scores four rankings of it: `fts`
(search without vectors), `vec` (search_vec alone), `hybrid` (search's vec-first
union) and `rrf` (_rrf_fuse over the fts and vec channels, the pre-#401 hybrid,
built as tests/eval/test_ranking_ablation_vec.py builds it). Each row also
records, for hybrid's top 10, which channel every result came from and whether
its session is gold, to show what displaces evidence turns (issue #1342).
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


def _key(r: dict) -> tuple:
    return (r.get("source_table"), r.get("id"))


def _rrf(fts: list[dict], vec: list[dict], limit: int) -> list[dict]:
    from techne.core.search import _rrf_fuse

    by_key = {_key(r): r for r in [*vec, *fts]}
    fused = _rrf_fuse(
        [
            [(_key(r), i) for i, r in enumerate(fts, 1)],
            [(_key(r), i) for i, r in enumerate(vec, 1)],
        ]
    )
    return [by_key[k] for k, _ in fused[:limit]]


def _top10_sources(results: list[dict], rows: dict, gold_sessions: set[str]) -> list[list]:
    """[match_source, session is gold] for each of the first 10 observation results."""
    out = []
    for r in results:
        if r.get("source_table") == "observation" and r.get("id") in rows:
            out.append([r.get("match_source") or "fts", rows[r["id"]][0] in gold_sessions])
        if len(out) == 10:
            break
    return out


def run_question(root: Path, item: dict, arm: str, limit: int) -> dict:
    from techne.core.search import backfill_vectors, search, search_vec

    db_path = root / item["question_id"] / "techne.db"
    db_path.parent.mkdir(parents=True)
    assert db_path.resolve().is_relative_to(root.resolve()), db_path

    t0 = time.monotonic()
    rows, gold_turns, repeats = load_haystack(db_path, item)
    if arm in ("hybrid", "compare"):
        embedded = backfill_vectors(db_path, allow_download=True).get("observations", 0)
        if embedded != len(rows):
            raise SystemExit(
                f"{item['question_id']}: embedded {embedded} of {len(rows)} turns; "
                "install the vectors extra and an embedding backend, or run --arm fts"
            )
    t1 = time.monotonic()
    # Observation recency always decays at a fixed 30-day half-life from now
    # (_rerank calls _recency_score(ts) without a half-life; search()'s half_life_days
    # is knowledge-confidence decay only). With 2023 dates that term is under 1e-9 for
    # every row, so the FTS order here is effectively BM25.
    q = item["question"]
    gold_sessions = set(item["answer_session_ids"])
    if arm == "compare":
        fts = search(db_path, q, table="observations", limit=limit, use_vectors=False)
        vec = search_vec(db_path, q, table="observations", limit=limit)
        hyb = search(db_path, q, table="observations", limit=limit, use_vectors=True)
        arms = {"fts": fts, "vec": vec, "hybrid": hyb, "rrf": _rrf(fts, vec, limit)}
    else:
        arms = {
            arm: search(db_path, q, table="observations", limit=limit, use_vectors=arm == "hybrid")
        }
    t2 = time.monotonic()

    def ranked(results: list[dict]) -> list[tuple[str, str]]:
        return [
            rows[r["id"]]
            for r in results
            if r.get("source_table") == "observation" and r.get("id") in rows
        ]

    scored = {a: score_question(ranked(res), gold_sessions, gold_turns) for a, res in arms.items()}
    results = arms["hybrid"] if "hybrid" in arms else arms[arm]
    extra = {}
    if arm == "compare":
        extra = {
            "arms": scored,
            "hybrid_top10": _top10_sources(results, rows, gold_sessions),
        }
    return {
        "question_id": item["question_id"],
        "question_type": item["question_type"],
        "n_turns": len(rows),
        "repeated_sessions": repeats,
        "n_results": len(ranked(results)),
        "vec_results": sum(1 for r in results if "vec" in (r.get("match_source") or "")),
        "load_s": round(t1 - t0, 3),
        "search_s": round(t2 - t1, 3),
        "metrics": scored["hybrid"] if "hybrid" in scored else scored[arm],
        **extra,
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
        "Corpus is user and assistant turns, gold is every has_answer turn, and a session "
        "counts once any of its turns is retrieved: upstream's metric names, not its "
        "corpus or rules, so not comparable with upstream numbers.",
        f"Zero-result queries: {sum(1 for r in out if r['n_results'] == 0)}. "
        f"Results from the vector channel: {sum(r['vec_results'] for r in out)}. "
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


def displacement(out: list[dict]) -> str:
    """What hybrid's top 10 holds on the questions where FTS@10 has every gold turn
    and hybrid@10 does not."""
    lost = [
        r
        for r in out
        if r["arms"]["fts"]["turn"]["recall_all@10"]
        and not r["arms"]["hybrid"]["turn"]["recall_all@10"]
    ]
    gained = sum(
        1
        for r in out
        if r["arms"]["hybrid"]["turn"]["recall_all@10"]
        and not r["arms"]["fts"]["turn"]["recall_all@10"]
    )
    slots = [s for r in lost for s in r["hybrid_top10"]]
    by_src: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for src, in_gold in slots:
        by_src[src][0] += 1
        by_src[src][1] += int(in_gold)
    lines = [
        "## Hybrid vs FTS at turn recall_all@10",
        "",
        f"FTS has every gold turn in its top 10 and hybrid does not: {len(lost)} questions. "
        f"The reverse: {gained}.",
        "",
        "On those questions, hybrid's top-10 slots by channel:",
        "",
        "| match_source | slots | in a gold session |",
        "|---|---|---|",
    ]
    for src, (n, g) in sorted(by_src.items()):
        lines.append(f"| {src} | {n} | {g} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--arm", choices=("fts", "hybrid", "compare"), default="fts")
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
    if args.arm != "fts" and not any(r["vec_results"] for r in out):
        raise SystemExit(f"--arm {args.arm}: no result came from the vector channel")
    if args.arm == "compare":
        text = "".join(
            summarize([{**r, "metrics": r["arms"][a]} for r in out], a, args.limit) + "\n"
            for a in ("fts", "vec", "hybrid", "rrf")
        )
        text += displacement(out)
    else:
        text = summarize(out, args.arm, args.limit)
    (args.out / "summary.md").write_text(text)
    print((args.out / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
