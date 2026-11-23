"""Opt-in cross-encoder reranking for architect search (core/rerank.py).

* OFF BY DEFAULT, AND OFF MEANS UNCHANGED: search_patterns' result is returned as is.
* ON: the top `rerank_top_k` hits are reordered by score, the rest keep their place.
* FAILS SOFT: a load or scoring failure keeps the BM25 order and reports the reason.
* THE FORWARD PASS: padding does not change a score (the mask works), and on the real
  model the logits match sentence-transformers (skipped when the model is not cached).
"""

import json
from pathlib import Path

import pytest

from techne.config import Config
from techne.core import patterns, rerank


def _hits(n):
    return [{"id": f"p{i}", "name": f"pattern {i}", "body": f"body {i}"} for i in range(n)]


class _FakeModel:
    """Scores a hit by the number in its id: higher id, higher score."""

    def score(self, query, docs):
        return [float(d.split(".")[0].split()[-1]) for d in docs]


def _config(**architect):
    return Config._from_dict({"architect": architect})


@pytest.fixture(autouse=True)
def _clear_cache():
    rerank._loaded.clear()
    yield
    rerank._loaded.clear()


def test_off_by_default_returns_search_patterns_unchanged(monkeypatch):
    calls = []
    hits = _hits(5)
    monkeypatch.setattr(
        patterns, "search_patterns", lambda db, q, limit: calls.append(limit) or hits
    )
    monkeypatch.setattr(rerank, "load", lambda *a, **k: pytest.fail("reranker loaded while off"))
    out, info = rerank.search_architect(Config._from_dict({}), Path("x.db"), "q", limit=3)
    assert out is hits and info is None and calls == [3]


def test_on_reorders_the_top_k_and_keeps_the_tail(monkeypatch):
    calls = []
    monkeypatch.setattr(
        patterns, "search_patterns", lambda db, q, limit: calls.append(limit) or _hits(8)
    )
    monkeypatch.setattr(rerank, "load", lambda *a, **k: _FakeModel())
    cfg = _config(rerank="minilm", rerank_top_k=5)
    out, info = rerank.search_architect(cfg, Path("x.db"), "q", limit=7)
    assert calls == [7]
    assert [h["id"] for h in out] == ["p4", "p3", "p2", "p1", "p0", "p5", "p6"]
    assert out[0]["rerank_score"] == 4.0 and "rerank_score" not in out[5]
    assert info["applied"] is True and info["model"] == "minilm"


def test_a_small_limit_still_reranks_the_full_candidate_set(monkeypatch):
    calls = []
    monkeypatch.setattr(
        patterns, "search_patterns", lambda db, q, limit: calls.append(limit) or _hits(limit)
    )
    monkeypatch.setattr(rerank, "load", lambda *a, **k: _FakeModel())
    out, _ = rerank.search_architect(_config(rerank="minilm"), Path("x.db"), "q", limit=3)
    assert calls == [20]
    assert [h["id"] for h in out] == ["p19", "p18", "p17"]


@pytest.mark.parametrize(
    "exc, expect",
    [
        (ImportError("no", name="tokenizers"), "tenish-techne[rerank]"),
        (OSError("not cached"), "OSError"),
    ],
)
def test_a_failed_load_keeps_bm25_order_and_says_why(monkeypatch, exc, expect):
    def boom(*a, **k):
        raise exc

    hits = _hits(4)
    monkeypatch.setattr(patterns, "search_patterns", lambda db, q, limit: hits)
    monkeypatch.setattr(rerank, "load", boom)
    out, info = rerank.search_architect(_config(rerank="minilm"), Path("x.db"), "q", limit=4)
    assert [h["id"] for h in out] == ["p0", "p1", "p2", "p3"]
    assert info["applied"] is False and expect in info["reason"]


def test_unknown_model_name_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(patterns, "search_patterns", lambda db, q, limit: _hits(3))
    _, info = rerank.search_architect(_config(rerank="nope"), Path("x.db"), "q", limit=3)
    assert info["applied"] is False and "unknown reranker" in info["reason"]


def test_config_parses_clamps_and_round_trips():
    a = _config(rerank=" MiniLM ", rerank_top_k=1, rerank_max_tokens=9999).architect
    assert (a.rerank, a.rerank_top_k, a.rerank_max_tokens) == ("minilm", 2, 512)
    d = Config._from_dict({"architect": {"rerank": "minilm", "rerank_top_k": 30}}).to_yaml_dict()[
        "architect"
    ]
    assert (d["rerank"], d["rerank_top_k"], d["rerank_max_tokens"]) == ("minilm", 30, 512)
    assert Config._from_dict({}).architect.rerank == ""


def test_pattern_text_matches_the_eval():
    long = {"name": "N", "body": "x" * 5000}
    assert rerank.pattern_text({"name": "Name", "body": "Body"}) == "Name. Body"
    assert len(rerank.pattern_text(long)) == rerank.TEXT_CHARS


# --- the forward pass ---------------------------------------------------------


@pytest.fixture
def np():
    # Per test, not per module: the wiring tests above need none of these.
    for mod in ("tokenizers", "safetensors"):
        pytest.importorskip(mod)
    return pytest.importorskip("numpy")


_WORDS = ["alpha", "beta", "gamma", "delta", "crash", "resume", "task", "token"]


def _tiny_bert(np, folder: Path, hidden=8, heads=2, layers=2, inter=16, seed=0):
    from safetensors.numpy import save_file
    from tokenizers import Tokenizer, normalizers, pre_tokenizers, processors
    from tokenizers.models import WordPiece

    vocab = {t: i for i, t in enumerate(["[PAD]", "[UNK]", "[CLS]", "[SEP]"] + _WORDS)}
    tok = Tokenizer(WordPiece(vocab, unk_token="[UNK]"))
    tok.normalizer = normalizers.BertNormalizer(lowercase=True)
    tok.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    tok.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", 2), ("[SEP]", 3)],
    )
    folder.mkdir(parents=True, exist_ok=True)
    tok.save(str(folder / "tokenizer.json"))
    rng = np.random.default_rng(seed)
    r = lambda *s: rng.normal(scale=0.3, size=s).astype("float32")  # noqa: E731
    W = {
        "bert.embeddings.word_embeddings.weight": r(len(vocab), hidden),
        "bert.embeddings.position_embeddings.weight": r(64, hidden),
        "bert.embeddings.token_type_embeddings.weight": r(2, hidden),
        "bert.embeddings.LayerNorm.weight": np.ones(hidden, "float32"),
        "bert.embeddings.LayerNorm.bias": np.zeros(hidden, "float32"),
        "bert.pooler.dense.weight": r(hidden, hidden),
        "bert.pooler.dense.bias": r(hidden),
        "classifier.weight": r(1, hidden),
        "classifier.bias": r(1),
    }
    for i in range(layers):
        p = f"bert.encoder.layer.{i}."
        for n in (
            "attention.self.query",
            "attention.self.key",
            "attention.self.value",
            "attention.output.dense",
        ):
            W[p + n + ".weight"], W[p + n + ".bias"] = r(hidden, hidden), r(hidden)
        W[p + "intermediate.dense.weight"], W[p + "intermediate.dense.bias"] = (
            r(inter, hidden),
            r(inter),
        )
        W[p + "output.dense.weight"], W[p + "output.dense.bias"] = r(hidden, inter), r(hidden)
        for n in ("attention.output.LayerNorm", "output.LayerNorm"):
            W[p + n + ".weight"], W[p + n + ".bias"] = (
                np.ones(hidden, "float32"),
                np.zeros(hidden, "float32"),
            )
    save_file(W, str(folder / "model.safetensors"))
    (folder / "config.json").write_text(
        json.dumps({"num_attention_heads": heads, "num_hidden_layers": layers})
    )
    return [folder / f for f in ("config.json", "model.safetensors", "tokenizer.json")]


def test_padding_does_not_change_a_score(np, tmp_path):
    m = rerank.CrossEncoder.from_files(*_tiny_bert(np, tmp_path), max_tokens=64)
    short, long = "alpha beta", "gamma delta crash resume task token alpha beta gamma"
    alone = m.score("resume task", [short])[0]
    batched = m.score("resume task", [short, long])
    assert batched[0] == pytest.approx(alone, abs=1e-5)
    assert batched[1] != pytest.approx(alone, abs=1e-3)


def test_truncation_bounds_the_input(np, tmp_path):
    m = rerank.CrossEncoder.from_files(*_tiny_bert(np, tmp_path), max_tokens=16)
    # 64 position embeddings: without the cut this pair would index past them.
    assert len(m.score("alpha", ["beta " * 200])) == 1


# Computed with sentence-transformers 5.x CrossEncoder(max_length=512), identity
# activation, 2026-11-22.
_REFERENCE = [
    (
        "resume work after a crash without losing the task",
        "Checkpoint each step to a ledger. On restart, the engine replays the ledger and resumes "
        "from the last committed step.",
        -7.40206241607666,
    ),
    (
        "resume work after a crash without losing the task",
        "A colour palette for dark mode, defined as CSS custom properties.",
        -11.346186637878418,
    ),
    (
        "how do agents share credentials safely",
        "Each hop re-mints a short-lived token scoped to the target, so a leaked token reaches "
        "one handler for minutes.",
        -11.417499542236328,
    ),
    (
        "how do agents share credentials safely",
        "Retry an HTTP request with exponential backoff and jitter.",
        -11.418191909790039,
    ),
]


def test_minilm_matches_sentence_transformers(np):
    pytest.importorskip("huggingface_hub")
    try:
        m = rerank.load("minilm", 512, allow_download=False)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MiniLM not in the local HF cache: {exc!r}")
    for q, d, want in _REFERENCE:
        assert m.score(q, [d])[0] == pytest.approx(want, abs=5e-5)
