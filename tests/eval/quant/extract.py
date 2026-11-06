"""Pull float vectors and embedded recall queries out of a techne.db backup (issue #1341).

    python tests/eval/quant/extract.py --db COPY.db --out vectors.npz

Writes the observation and knowledge vec tables' rowids and float32 embeddings, plus
the embeddings of the distinct per-turn recall queries logged in context_injections.
The queries are `trigger_snippet`, the prompt's first 200 characters, while search()
embeds the whole prompt.

The .npz holds no text, but it is not anonymous. The model is public and static, and
an embedding is close to an average of token vectors, so a search over the vocabulary
recovers a prompt's keywords. Treat the file like the store: keep it out of git (it
is ignored), move it only to hosts you trust with the store, and delete it when the
numbers are settled.

Run it against a `sqlite3 techne.db ".backup COPY.db"` copy, never the live store, and
with the same Python as the live install so the query embeddings come from the live
backend.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

import numpy as np

log = logging.getLogger("quant.extract")

# The tables neighbors.py measures; threads_vec is not exported.
TABLES = ("observations_vec", "knowledge_vec")


def main() -> int:
    from techne.core.embeddings import _get_embedding_model
    from techne.core.search import _ensure_sqlite_vec, _query_embedding, clean_query_text

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    if not _ensure_sqlite_vec(conn):
        raise SystemExit("sqlite-vec did not load")
    arrays: dict[str, np.ndarray] = {}
    for t in TABLES:
        rows = conn.execute(f"SELECT rowid, embedding FROM {t}").fetchall()
        arrays[f"{t}_ids"] = np.array([r[0] for r in rows], dtype=np.int64)
        arrays[t] = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(
            len(rows), -1
        )
        log.info("%s: %d x %d", t, *arrays[t].shape)

    queries = [
        q
        for (q,) in conn.execute(
            "SELECT DISTINCT trigger_snippet FROM context_injections"
            " WHERE source = 'per_turn_recall' AND trigger_snippet IS NOT NULL"
            " ORDER BY trigger_snippet"
        )
    ]
    conn.close()
    vecs = []
    for q in queries:
        v = _query_embedding(clean_query_text(q))
        if v is not None:
            # float32 bytes, the form search_vec binds.
            vecs.append(
                np.frombuffer(v, dtype=np.float32)
                if isinstance(v, bytes)
                else np.asarray(v, dtype=np.float32)
            )
    arrays["queries"] = np.stack(vecs)
    model = _get_embedding_model()
    log.info("queries: %d of %d embedded with %s", len(vecs), len(queries), model.name)
    np.savez(args.out, model=np.array(model.name), **arrays)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
