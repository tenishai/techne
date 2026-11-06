"""LongMemEval-S vector-only recall per quantized format (issue #1341, step 4).

    python tests/eval/quant/longmemeval_quant.py --data longmemeval_s_cleaned.json --out OUT \
        [--limit 100] [--max-questions N]

Each question's haystack is loaded and embedded exactly as tests/eval/longmemeval/run.py
does. Its float vectors are then indexed once per format with neighbors.py's builders and
queried with the question's embedding, so every format ranks the same vectors with the
same query. The float arm is the same KNN search_vec runs, so it should match the
harness's `vec` arm. Scores are paired per question against float.

int8_scaled uses one fixed scale for every haystack (--int8-max, default the live
observations' max|x|), as a stored index would, not a per-haystack one.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import statistics
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "longmemeval"))
from lme_metrics import score_question  # noqa: E402
from neighbors import FORMATS, build, query  # noqa: E402

log = logging.getLogger("quant.lme")

# (level, metric) pairs the paired comparison reports.
PAIRED = (("session", "recall_all@5"), ("session", "recall_all@10"), ("turn", "recall_all@10"))


def run_question(root: Path, item: dict, limit: int, scale: float) -> dict:
    import run as lme  # tests/eval/longmemeval/run.py

    from techne.core.search import (
        _ensure_sqlite_vec,
        _query_embedding,
        backfill_vectors,
        clean_query_text,
    )

    qdir = root / item["question_id"]
    qdir.mkdir(parents=True)
    db_path = qdir / "techne.db"
    rows, gold_turns, _ = lme.load_haystack(db_path, item)
    embedded = backfill_vectors(db_path, allow_download=True).get("observations", 0)
    if embedded != len(rows):
        raise SystemExit(f"{item['question_id']}: embedded {embedded} of {len(rows)} turns")
    import sqlite3

    conn = sqlite3.connect(db_path)
    _ensure_sqlite_vec(conn)
    got = conn.execute("SELECT rowid, embedding FROM observations_vec").fetchall()
    conn.close()
    ids = np.array([r[0] for r in got], dtype=np.int64)
    vecs = np.frombuffer(b"".join(r[1] for r in got), dtype=np.float32).reshape(len(got), -1)
    qv = _query_embedding(clean_query_text(item["question"]))
    q = np.frombuffer(qv, dtype=np.float32) if isinstance(qv, bytes) else np.asarray(qv, np.float32)

    gold_sessions = set(item["answer_session_ids"])
    metrics = {}
    for fmt in FORMATS:
        idx = build(qdir / f"{fmt}.db", fmt, ids, vecs, scale)
        hits = query(idx, fmt, q, scale, k=min(limit, len(ids)))
        idx.close()
        ranked = [rows[h[0]] for h in hits if h[0] in rows]
        metrics[fmt] = score_question(ranked, gold_sessions, gold_turns)
    return {
        "question_id": item["question_id"],
        "question_type": item["question_type"],
        "n_turns": len(rows),
        "metrics": metrics,
    }


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial p for wins vs losses, ties dropped."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


def summarize(out: list[dict], limit: int, scale: float, model: str) -> str:
    def mean(fmt: str, lvl: str, m: str) -> float:
        return statistics.fmean(r["metrics"][fmt][lvl][m] for r in out)

    cols = [
        ("session", "recall_all@5"),
        ("session", "recall_all@10"),
        ("turn", "recall_all@5"),
        ("turn", "recall_all@10"),
        ("turn", "recall_all@50"),
    ]
    lines = [
        "# LongMemEval-S vector-only recall by format (issue #1341)",
        "",
        f"{len(out)} questions, k={limit}, embedding {model}, int8_scaled scale {scale:.1f}.",
        "",
        "| format | " + " | ".join(f"{lvl} {m}" for lvl, m in cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for fmt in FORMATS:
        lines.append(
            f"| {fmt} | " + " | ".join(f"{mean(fmt, lvl, m):.3f}" for lvl, m in cols) + " |"
        )
    lines += [
        "",
        "## Paired against float (questions better / worse, exact sign test)",
        "",
        "| format | " + " | ".join(f"{lvl} {m}" for lvl, m in PAIRED) + " |",
        "|---|" + "---|" * len(PAIRED),
    ]
    for fmt in FORMATS[1:]:
        cells = []
        for lvl, m in PAIRED:
            d = [r["metrics"][fmt][lvl][m] - r["metrics"]["float"][lvl][m] for r in out]
            w, lo = sum(x > 0 for x in d), sum(x < 0 for x in d)
            cells.append(f"+{w} / -{lo} (p={sign_test(w, lo):.3f})")
        lines.append(f"| {fmt} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--max-questions", type=int, default=0, help="0 = all")
    ap.add_argument(
        "--int8-max", type=float, default=0.545, help="max|x| the int8 scale maps to 127"
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    scale = 127.0 / args.int8_max

    with tempfile.TemporaryDirectory(prefix="techne-lmeq-") as tmp:
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
                r = run_question(root, item, args.limit, scale)
                out.append(r)
                fh.write(json.dumps(r) + "\n")
                if n % 25 == 0:
                    log.info("%d/%d questions", n, len(items))
    from techne.core.embeddings import _get_embedding_model

    text = summarize(out, args.limit, scale, _get_embedding_model().name)
    (args.out / "summary.md").write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
