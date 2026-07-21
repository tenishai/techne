---
card: arch-card:techne-search-embeddings
updated: 2026-07-20
anchor: techne/core/embeddings.py sha=86e5cfaf6482d008ef6477a1deacde49795217ea
anchor: techne/core/search.py sha=4a1ff0c2c8384274d38b6e52a1b9ace004acf57b
---
# search / embeddings — hybrid FTS + vector recall

WHAT: recall over knowledge/threads/observations = hybrid BM25 (FTS5) + vector (sqlite-vec, vec0) with MMR re-rank. search()/search_vec() in core/search.py; embedding-backend abstraction in core/embeddings.py.
BACKEND (techne#601/#602): ONE interface (EmbeddingModel) over TWO backends — fastembed (TextEmbedding, MiniLM-384) OR model2vec (StaticModel, potion-base-8M, 256-dim, numpy-only static). config `search.embedding_backend=''` = AUTO-SELECT (NOT disabled): model2vec on Python 3.14+ or when fastembed is absent, else fastembed.
CONSTRAINT — fastembed is UNUSABLE on Python 3.14: its dep onnxruntime ships no 3.14 wheel. On 3.14 the live backend is model2vec/potion (dim 256). Do NOT try to "enable fastembed" on 3.14 — it cannot load.
CHECK, DON'T ASSUME: the live backend is machine/python-version dependent and `embedding_backend=''` means auto — so read it from `techne vectors status`, never infer it from the (often empty) config value.
DEGRADE: vector search silently falls back to FTS-only when sqlite-vec/the backend is unavailable OR the vec tables are stale after a backend swap. Distances are normalized so model2vec L2 stays comparable to the fastembed baseline (tag_threshold ceiling is backend-dependent: potion tops ~0.59).
BUILD/REPAIR: `techne vectors backfill` embeds un-embedded rows (and does a full drop+re-embed on a backend/model swap via _ensure_vec_backend_identity).
GOTCHA (techne#382): backfill only embeds rows MISSING from *_vec — it does NOT re-embed a row whose content changed in place (e.g. after a `library cards ingest` gist edit) → the vector goes STALE until you delete the vec row (sqlite_vec-loaded conn) and re-backfill.
CONSUMERS: techne_search / recall; the per_turn_recall UserPromptSubmit rail (techne#109, hybrid search over knowledge, top-N injected per turn); Librarian gist recall; reflect/consolidate similarity. First embed triggers a HuggingFace model fetch (model2vec potion / fastembed onnx); uncached + offline degrades rather than crashing.
