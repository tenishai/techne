"""Compare quantized vec0 indexes against float on real vectors (issue #1341, steps 2-3).

    python tests/eval/quant/neighbors.py --npz vectors.npz --out OUT [--max-queries N]

Reads extract.py's .npz. For each vec table, builds one vec0 index per format with
sqlite-vec's own functions and runs the same `embedding MATCH ? AND k = ?` query that
search_vec runs:

    float          float[256], what techne stores today
    int8_unit      int8[256] via vec_quantize_int8(v, 'unit')
    int8_scaled    int8[256], each value * 127 / max|x| over the stored vectors
    bit            bit[256] via vec_quantize_binary(v)
    bit_rescore    bit top k*OVERSAMPLE, re-ranked by float L2 (vec_distance_L2)

Reports neighbor recall@5/10/20 against float, the distance distributions (what a
cutoff retune needs), median and p95 query time, and each index's size on disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("quant.neighbors")

K = 20
KS = (5, 10, 20)
OVERSAMPLE = 8
FORMATS = ("float", "int8_unit", "int8_scaled", "bit", "bit_rescore")
TABLES = ("observations_vec", "knowledge_vec")


def _open(path: Path) -> sqlite3.Connection:
    from techne.core.search import _ensure_sqlite_vec

    conn = sqlite3.connect(path)
    if not _ensure_sqlite_vec(conn):
        raise SystemExit("sqlite-vec did not load")
    return conn


def build(
    path: Path, fmt: str, ids: np.ndarray, vecs: np.ndarray, scale: float
) -> sqlite3.Connection:
    """One index for one format. bit_rescore keeps the floats in a plain table too."""
    conn = _open(path)
    dim = vecs.shape[1]
    col, insert = {
        "float": (f"float[{dim}]", "?"),
        "int8_unit": (f"int8[{dim}]", "vec_quantize_int8(?, 'unit')"),
        "int8_scaled": (f"int8[{dim}]", "vec_int8(?)"),
        "bit": (f"bit[{dim}]", "vec_quantize_binary(?)"),
        "bit_rescore": (f"bit[{dim}]", "vec_quantize_binary(?)"),
    }[fmt]
    conn.execute(f"CREATE VIRTUAL TABLE v USING vec0(embedding {col})")
    data = _to_int8(vecs, scale) if fmt == "int8_scaled" else vecs
    conn.executemany(
        f"INSERT INTO v (rowid, embedding) VALUES (?, {insert})",
        ((int(i), row.tobytes()) for i, row in zip(ids, data, strict=True)),
    )
    if fmt == "bit_rescore":
        conn.execute("CREATE TABLE f (id INTEGER PRIMARY KEY, embedding BLOB)")
        conn.executemany(
            "INSERT INTO f VALUES (?, ?)",
            ((int(i), row.tobytes()) for i, row in zip(ids, vecs, strict=True)),
        )
    conn.commit()
    return conn


def _to_int8(vecs: np.ndarray, scale: float) -> np.ndarray:
    return np.clip(np.rint(vecs * scale), -127, 127).astype(np.int8)


def query(
    conn: sqlite3.Connection, fmt: str, q: np.ndarray, scale: float, k: int = K
) -> list[tuple[int, float]]:
    qb = q.tobytes()
    if fmt == "float":
        sql, arg = "SELECT rowid, distance FROM v WHERE embedding MATCH ? AND k = ?", qb
    elif fmt == "int8_unit":
        sql = (
            "SELECT rowid, distance FROM v"
            " WHERE embedding MATCH vec_quantize_int8(?, 'unit') AND k = ?"
        )
        arg = qb
    elif fmt == "int8_scaled":
        sql, arg = (
            "SELECT rowid, distance FROM v WHERE embedding MATCH vec_int8(?) AND k = ?",
            _to_int8(q[None, :], scale)[0].tobytes(),
        )
    else:
        sql, arg = (
            "SELECT rowid, distance FROM v WHERE embedding MATCH vec_quantize_binary(?) AND k = ?",
            qb,
        )
    if fmt != "bit_rescore":
        return conn.execute(sql, (arg, k)).fetchall()
    cand = conn.execute(sql, (arg, k * OVERSAMPLE)).fetchall()
    marks = ",".join("?" * len(cand))
    return conn.execute(
        f"SELECT id, vec_distance_L2(embedding, ?) AS d FROM f WHERE id IN ({marks})"
        " ORDER BY d LIMIT ?",
        (qb, *[c[0] for c in cand], k),
    ).fetchall()


def pct(xs: list[float], p: float) -> float:
    return float(np.percentile(xs, p)) if xs else float("nan")


def run_table(
    name: str, ids: np.ndarray, vecs: np.ndarray, queries: np.ndarray, work: Path
) -> dict:
    scale = 127.0 / float(np.abs(vecs).max())
    norms = np.linalg.norm(vecs, axis=1)
    log.info(
        "%s: %d vectors, max|x| %.4f, norm %.3f-%.3f",
        name,
        len(ids),
        127 / scale,
        norms.min(),
        norms.max(),
    )
    res: dict = {
        "n": len(ids),
        "max_abs": 127 / scale,
        "norm_min": float(norms.min()),
        "norm_max": float(norms.max()),
        "n_zero": int((norms < 1e-6).sum()),
        "formats": {},
    }
    float_top: list[list[int]] = []
    float_dist: list[dict[int, float]] = []  # per query: rowid -> float distance
    for fmt in FORMATS:
        path = work / f"{name}-{fmt}.db"
        path.unlink(missing_ok=True)
        conn = build(path, fmt, ids, vecs, scale)
        times, recalls = [], {k: [] for k in KS}
        top1, top10, pairs = [], [], []
        for qi, q in enumerate(queries):
            t = time.perf_counter()
            hits = query(conn, fmt, q, scale)
            times.append(time.perf_counter() - t)
            got = [h[0] for h in hits]
            if fmt == "float":
                float_top.append(got)
                float_dist.append({h[0]: h[1] for h in hits})
            ref = float_top[qi]
            for k in KS:
                recalls[k].append(len(set(got[:k]) & set(ref[:k])) / k)
            if hits:
                top1.append(hits[0][1])
                top10.extend(h[1] for h in hits[:10])
                # The format's distance against float's, for rows both put in their top 20.
                pairs.extend(
                    (float_dist[qi][h[0]], h[1]) for h in hits[:10] if h[0] in float_dist[qi]
                )
        conn.close()
        fd = np.array([p[0] for p in pairs]) if pairs else np.array([])
        qd = np.array([p[1] for p in pairs]) if pairs else np.array([])
        fit = np.polyfit(fd, qd, 1).tolist() if len(pairs) > 2 else [float("nan")] * 2
        res["formats"][fmt] = {
            "bytes": path.stat().st_size,
            **{f"recall@{k}": statistics.fmean(v) for k, v in recalls.items()},
            "median_ms": 1000 * statistics.median(times),
            "p95_ms": 1000 * pct(times, 95),
            "top1": [pct(top1, p) for p in (5, 50, 95)],
            "top10": [pct(top10, p) for p in (5, 50, 95)],
            "pearson_vs_float": float(np.corrcoef(fd, qd)[0, 1])
            if len(pairs) > 2
            else float("nan"),
            "fit_vs_float": fit,  # format distance ~= fit[0] * float distance + fit[1]
        }
        log.info("%s %s: %s", name, fmt, json.dumps(res["formats"][fmt]))
        path.unlink(missing_ok=True)
    return res


def report(out: dict) -> str:
    def trio(xs: list[float]) -> str:
        return "/".join(f"{x:.3f}" for x in xs)

    lines = [
        "# Quantized vec0 vs float on live vectors (issue #1341)",
        "",
        f"Model: {out['model']}. Queries: {out['n_queries']} logged per-turn recall prompts."
        f" k={K}, bit_rescore oversample {OVERSAMPLE}x.",
        "",
    ]
    for t, r in out["tables"].items():
        lines += [
            f"## {t}: {r['n']} vectors, {r['n_zero']} zero (max|x| {r['max_abs']:.3f},"
            f" norms {r['norm_min']:.3f}-{r['norm_max']:.3f})",
            "",
            "| format | MB | recall@5 | @10 | @20 | median ms | p95 ms"
            " | top1 dist p5/p50/p95 | top10 dist p5/p50/p95 | r vs float |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for f, m in r["formats"].items():
            lines.append(
                f"| {f} | {m['bytes'] / 1e6:.1f}"
                f" | {m['recall@5']:.3f} | {m['recall@10']:.3f} | {m['recall@20']:.3f}"
                f" | {m['median_ms']:.2f} | {m['p95_ms']:.2f}"
                f" | {trio(m['top1'])} | {trio(m['top10'])} | {m['pearson_vs_float']:.3f} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--npz", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-queries", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args.out.mkdir(parents=True, exist_ok=True)
    data = np.load(args.npz)
    queries = data["queries"]
    if args.max_queries:
        queries = queries[: args.max_queries]
    out = {"model": str(data["model"]), "n_queries": len(queries), "tables": {}}
    for t in TABLES:
        out["tables"][t] = run_table(t, data[f"{t}_ids"], data[t], queries, args.out)
    (args.out / "results.json").write_text(json.dumps(out, indent=1))
    text = report(out)
    (args.out / "summary.md").write_text(text)
    print(text)
    print("RESULTS_JSON " + json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
