"""Issue 1217: core/query_embed.py embeds a search query without numpy.

What must hold:
  * EQUALITY: the same float32 bytes as the numpy path (`EmbeddingBackend.embed`
    over `static_direct`), bit for bit, on fixture models whose dims exercise every
    branch of numpy's pairwise sum, and on the cached potion-base-8M when present;
  * FALLBACK: anything it does not reproduce exactly returns None and
    `_query_embedding` still answers through the numpy path;
  * SEARCH: a vector search returns the same rows, in the same order, at the same
    distances, through either path;
  * SIDECAR: the cached median token length is reused, re-derived when the
    tokenizer changes, and a corrupt sidecar is not an error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("tokenizers")
pytest.importorskip("safetensors")

from techne.config import Config, SearchConfig  # noqa: E402
from techne.core import embeddings, query_embed, static_direct  # noqa: E402

_WORDS = (
    "alpha beta gamma delta search hook recall vector a b the model embed turn prompt knowledge"
).split()
_SUBWORDS = ["##s", "##ing", "##ed", "##er"]


def write_model(
    folder: Path,
    *,
    dim=8,
    normalize=True,
    dtype="float32",
    extra_tensors=None,
    seed=0,
    with_unk=True,
    rows_delta=0,
    table=None,
):
    """A tiny model2vec-layout model: WordPiece tokenizer, random table, config."""
    from safetensors.numpy import save_file
    from tokenizers import Tokenizer, normalizers, pre_tokenizers
    from tokenizers.models import WordPiece

    vocab_list = ["[PAD]", "[UNK]", "[CLS]", "[SEP]"] + _WORDS + _SUBWORDS
    if not with_unk:
        vocab_list.remove("[UNK]")
    vocab = {tok: i for i, tok in enumerate(vocab_list)}
    tok = Tokenizer(WordPiece(vocab, unk_token="[UNK]"))
    tok.normalizer = normalizers.BertNormalizer(lowercase=True)
    tok.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    folder.mkdir(parents=True, exist_ok=True)
    tok.save(str(folder / "tokenizer.json"))
    rng = np.random.default_rng(seed)
    if table is None:
        table = rng.normal(size=(len(vocab_list) + rows_delta, dim)).astype(dtype)
    tensors = {"embeddings": table}
    tensors.update(extra_tensors or {})
    save_file(tensors, str(folder / "model.safetensors"))
    (folder / "config.json").write_text(
        json.dumps({"model_type": "model2vec", "normalize": normalize, "hidden_dim": dim})
    )
    return folder


def prompts() -> list[str]:
    return [
        "alpha beta gamma",
        "",  # no ids
        "zzqxj wibble",  # all [UNK], filtered out: a zero row
        "Searching the HOOK recalled embedded vectors",
        "a b " * 300,  # 600 tokens: the 512-token cut applies
        "alpha " * 1000,  # the character pre-cut applies
        "Überprüfung naïve café 日本語 alpha",
        "techne#1096 _get_embedding_model() hook",
        "the the the the",
        "delta",
        "gamma " * 3 + "zzz",
        "knowledge prompt turn embed model",
        "searching searched searcher",
        "\t\n  alpha beta  ",
        "ALPHA BETA",
        "a",
        "vector, recall; hook: prompt!",
        "model" * 50,
        "embed the knowledge of the turn",
        "b a b a b a",
    ]


def numpy_bytes(folder_or_name: str, text: str) -> bytes:
    impl = static_direct.load(folder_or_name)
    backend = embeddings.EmbeddingBackend("model2vec", folder_or_name, impl, impl.dim)
    return np.stack(list(backend.embed([text])))[0].astype("float32").tobytes()


def _cfg(model: str) -> Config:
    cfg = Config()
    cfg.search = SearchConfig(embedding_backend="model2vec", embedding_model=model)
    return cfg


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(query_embed, "_MODELS", {})
    embeddings._reset_embedding_model()
    yield
    embeddings._reset_embedding_model()


class TestEquality:
    # dim 5: n < 8 branch; 8: one unrolled block; 300: the recursive split,
    # with a block whose length is not a multiple of 8.
    @pytest.mark.parametrize("dim", [5, 8, 300])
    @pytest.mark.parametrize("normalize", [True, False])
    def test_fixture_bytes_match_the_numpy_path(self, tmp_path, dim, normalize):
        folder = str(write_model(tmp_path / "m", dim=dim, normalize=normalize, seed=dim))
        for text in prompts():
            got = query_embed.embed_query_bytes(text, _cfg(folder))
            assert got is not None, text
            assert got == numpy_bytes(folder, text), text

    def test_matches_static_direct_encode(self):
        """The cached potion-base-8M, 20 prompts. Never downloads; skips when absent.

        The spec's bar was cosine > 1-1e-6 and max abs diff < 1e-6; the bytes are
        equal, which is stricter, and both are asserted so a future numpy that
        changes its summation order shows which one it broke."""
        name = embeddings._DEFAULT_MODEL2VEC_MODEL
        try:
            static_direct.resolve_local(name)
        except static_direct.Unsupported:
            pytest.skip(f"{name} not in the local HF cache")
        cfg = _cfg(name)
        for text in prompts():
            got = query_embed.embed_query_bytes(text, cfg)
            want = numpy_bytes(name, text)
            assert got is not None, text
            a, b = np.frombuffer(got, "f4"), np.frombuffer(want, "f4")
            assert np.abs(a - b).max() < 1e-6, text
            if a.any():
                assert float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b)) > 1 - 1e-6
            assert got == want, text

    def test_running_sum_rounds_to_float32_each_step(self, tmp_path):
        """numpy's axis-0 mean adds row by row in float32. Summing in double and
        rounding once gives a different answer when small rows are added to a
        large one: 1 + 2**-25 is 1 in float32, every time."""
        table = np.full((24, 8), 2.0**-25, dtype="float32")
        alpha = 4 + _WORDS.index("alpha")
        table[alpha] = 1.0
        table[alpha, 0] = -1.0  # not a multiple of the ones vector
        folder = str(write_model(tmp_path / "m", table=table, normalize=False))
        text = "alpha " + "beta " * 40
        assert query_embed.embed_query_bytes(text, _cfg(folder)) == numpy_bytes(folder, text)

    def test_a_built_singleton_names_the_model(self, tmp_path):
        """With the singleton already built, the fast path embeds with ITS model,
        not with a fresh AUTO choice."""
        folder = str(write_model(tmp_path / "m", dim=16))
        impl = static_direct.load(folder)
        embeddings._embedding_model = embeddings.EmbeddingBackend("model2vec", folder, impl, 16)
        assert query_embed.embed_query_bytes("alpha beta") == numpy_bytes(folder, "alpha beta")

    def test_a_non_model2vec_singleton_is_left_alone(self, tmp_path):
        embeddings._embedding_model = embeddings.EmbeddingBackend("fastembed", "x/y", None, 4)
        assert query_embed.embed_query_bytes("alpha") is None


class TestFallback:
    def test_falls_back_on_f16_table_and_quantized_model(self, tmp_path):
        f16 = str(write_model(tmp_path / "h", dtype="float16"))
        assert query_embed.embed_query_bytes("alpha beta", _cfg(f16)) is None
        quant = write_model(tmp_path / "q", extra_tensors={"mapping": np.arange(24, dtype="int32")})
        assert query_embed.embed_query_bytes("alpha beta", _cfg(str(quant))) is None
        assert query_embed.embed_query_bytes("alpha", _cfg(str(tmp_path / "nowhere"))) is None
        # static_direct refuses these two, so this must too.
        short = write_model(tmp_path / "r", rows_delta=-1)
        assert query_embed.embed_query_bytes("alpha beta", _cfg(str(short))) is None
        no_unk = write_model(tmp_path / "u", with_unk=False)
        with pytest.raises(static_direct.Unsupported):
            static_direct.load(str(no_unk))
        assert query_embed.embed_query_bytes("alpha beta", _cfg(str(no_unk))) is None
        assert query_embed.embed_query_bytes("alpha", _cfg("org/not-cached-model")) is None

    def test_query_embedding_still_answers_through_numpy(self, tmp_path, monkeypatch):
        from techne.core import search as search_mod
        from techne.core import vec_ext

        monkeypatch.setattr(vec_ext, "available", lambda: True)
        f16 = str(write_model(tmp_path / "h", dtype="float16"))
        impl = static_direct.load(f16)
        embeddings._embedding_model = embeddings.EmbeddingBackend("model2vec", f16, impl, impl.dim)
        vec = search_mod._query_embedding("alpha beta")
        assert isinstance(vec, np.ndarray)
        assert vec.astype("float32").tobytes() == numpy_bytes(f16, "alpha beta")

    def test_query_embedding_returns_bytes_on_the_fast_path(self, tmp_path, monkeypatch):
        from techne.core import search as search_mod
        from techne.core import vec_ext

        monkeypatch.setattr(vec_ext, "available", lambda: True)
        folder = str(write_model(tmp_path / "m", dim=16))
        impl = static_direct.load(folder)
        embeddings._embedding_model = embeddings.EmbeddingBackend("model2vec", folder, impl, 16)
        assert search_mod._query_embedding("alpha beta") == numpy_bytes(folder, "alpha beta")

    def test_a_replaced_resolver_is_honoured(self, tmp_path, monkeypatch):
        """Tests and callers that swap `_get_embedding_model` get THEIR embedder."""
        from techne.core import search as search_mod
        from techne.core import vec_ext

        monkeypatch.setattr(vec_ext, "available", lambda: True)
        folder = str(write_model(tmp_path / "m", dim=16))
        impl = static_direct.load(folder)
        embeddings._embedding_model = embeddings.EmbeddingBackend("model2vec", folder, impl, 16)

        class _Stub:
            def embed(self, texts):
                yield np.ones(16, dtype="float32")

        monkeypatch.setattr(search_mod, "_get_embedding_model", lambda **_kw: _Stub())
        vec = search_mod._query_embedding("alpha beta")
        assert np.array_equal(vec, np.ones(16, dtype="float32"))


class TestSearchParity:
    def test_search_results_identical_to_numpy_path(self, tmp_path, monkeypatch):
        pytest.importorskip("sqlite_vec")
        from techne.core import search as search_mod
        from techne.core.database import init_db
        from techne.core.knowledge import store

        folder = str(write_model(tmp_path / "m", dim=32, seed=7))
        impl = static_direct.load(folder)
        embeddings._embedding_model = embeddings.EmbeddingBackend("model2vec", folder, impl, 32)

        db = tmp_path / "t.db"
        init_db(db).close()
        texts = [
            "alpha beta gamma",
            "search hook recall",
            "vector model embed",
            "the turn prompt knowledge",
            "delta searching",
            "beta beta alpha",
            "recalled vectors embedded",
            "gamma delta the",
        ]
        for i, text in enumerate(texts):
            store(db, f"{text} row{i}", type="fact")
        search_mod.backfill_vectors(db)

        calls = []
        real = query_embed.embed_query_bytes

        def _spy(*a, **k):
            out = real(*a, **k)
            calls.append(out is not None)
            return out

        def run():
            out = []
            for q in ["alpha gamma", "hook recall vector", "the knowledge", "embedded searching"]:
                rows = search_mod.search_vec(db, q, table="knowledge", limit=8)
                out.append([(r["id"], r["distance"]) for r in rows])
            return out

        monkeypatch.setattr(query_embed, "embed_query_bytes", _spy)
        fast = run()
        assert calls and all(calls), "the fast path must actually have been taken"
        monkeypatch.setattr(query_embed, "embed_query_bytes", lambda *a, **k: None)
        slow = run()
        assert all(page for page in fast), "an empty page compares nothing"
        assert fast == slow


class TestSidecar:
    def test_median_sidecar_roundtrip_and_invalidation(self, tmp_path, monkeypatch):
        folder = write_model(tmp_path / "m", dim=8)
        cfg = _cfg(str(folder))
        sidecar = Path(os.environ["TECHNE_HOME"]) / query_embed.SIDECAR_NAME
        want = numpy_bytes(str(folder), "alpha beta")

        assert query_embed.embed_query_bytes("alpha beta", cfg) == want
        data = json.loads(sidecar.read_text())
        assert data["folder"] == str(folder)
        assert data["median"] == static_direct.load(str(folder)).median_token_length
        assert data["vocab_size"] == 24

        # Reused: a planted value is read back rather than recomputed.
        data["median"] = 1
        sidecar.write_text(json.dumps(data))
        monkeypatch.setattr(query_embed, "_MODELS", {})
        query_embed.embed_query_bytes("alpha", cfg)
        assert query_embed._MODELS[str(folder)].median_token_length == 1

        # Re-derived when tokenizer.json changes (mtime_ns is part of the key).
        tok = folder / "tokenizer.json"
        st = tok.stat()
        os.utime(tok, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        monkeypatch.setattr(query_embed, "_MODELS", {})
        assert query_embed.embed_query_bytes("alpha beta", cfg) == want
        true_median = static_direct.load(str(folder)).median_token_length
        assert query_embed._MODELS[str(folder)].median_token_length == true_median
        assert json.loads(sidecar.read_text())["median"] == true_median

        # A corrupt sidecar is ignored and rewritten.
        sidecar.write_text("{not json")
        monkeypatch.setattr(query_embed, "_MODELS", {})
        assert query_embed.embed_query_bytes("alpha beta", cfg) == want
        assert json.loads(sidecar.read_text())["folder"] == str(folder)
        assert not list(sidecar.parent.glob(f"{query_embed.SIDECAR_NAME}.*.tmp"))
