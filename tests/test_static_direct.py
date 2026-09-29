"""techne#1096: core/static_direct.py loads a model2vec model without importing model2vec.

What must hold, one class each:
  * EQUALITY — same vectors as model2vec's StaticModel.encode, bit for bit, on a
    small fixture model (always) and on the cached potion-base-8M (when present);
  * FALLBACK — anything the direct loader cannot reproduce goes to model2vec, with
    a debug line and no error;
  * OFFLINE — the fast path reads local files only and never imports the
    huggingface_hub download machinery;
  * IDENTITY — the backend it builds stamps the same identity model2vec's does, so
    `_ensure_vec_backend_identity` sees no swap and drops nothing.
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("tokenizers")
pytest.importorskip("safetensors")

from techne.core import embeddings, static_direct  # noqa: E402

_WORDS = (
    "alpha beta gamma delta search hook recall vector a b the model embed turn prompt knowledge"
)
_WORDS = _WORDS.split()
_SUBWORDS = ["##s", "##ing", "##ed", "##er"]


def _write_model(
    folder: Path,
    *,
    normalize=True,
    extra_tensors=None,
    model_type="model2vec",
    rows_delta=0,
    dtype="float32",
    seed=0,
):
    """A tiny model2vec-layout model: WordPiece tokenizer + random table + config."""
    from safetensors.numpy import save_file
    from tokenizers import Tokenizer, normalizers, pre_tokenizers
    from tokenizers.models import WordPiece

    vocab_list = ["[PAD]", "[UNK]", "[CLS]", "[SEP]"] + _WORDS + _SUBWORDS
    vocab = {tok: i for i, tok in enumerate(vocab_list)}
    tok = Tokenizer(WordPiece(vocab, unk_token="[UNK]"))
    tok.normalizer = normalizers.BertNormalizer(lowercase=True)
    tok.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    folder.mkdir(parents=True, exist_ok=True)
    tok.save(str(folder / "tokenizer.json"))

    rng = np.random.default_rng(seed)
    table = rng.normal(size=(len(vocab_list) + rows_delta, 8)).astype(dtype)
    tensors = {"embeddings": table}
    tensors.update(extra_tensors or {})
    save_file(tensors, str(folder / "model.safetensors"))
    cfg = {"model_type": model_type, "normalize": normalize, "hidden_dim": 8}
    (folder / "config.json").write_text(json.dumps(cfg))
    return folder


def _sentences():
    long_tokens = "a b " * 700  # 1400 one-char tokens: the 512-token cut applies
    long_chars = "alpha " * 1000  # 6000 chars: the character pre-cut applies
    return [
        "alpha beta gamma",
        "",  # no ids at all: model2vec's float64 zero row
        "zzqxj wibble",  # every token is [UNK], filtered out: zero row again
        "Searching the HOOK recalled embedded vectors",
        long_tokens,
        long_chars,
        "Überprüfung naïve café 日本語 alpha",
        "techne#1096 _get_embedding_model() hook",
    ]


@pytest.fixture
def tiny(tmp_path):
    return _write_model(tmp_path / "tiny")


def _model2vec():
    return pytest.importorskip("model2vec")


class TestEquality:
    @pytest.mark.parametrize("normalize", [True, False])
    def test_fixture_vectors_match_model2vec_bit_for_bit(self, tmp_path, normalize):
        m2v = _model2vec()
        folder = _write_model(tmp_path / "m", normalize=normalize)
        ours = static_direct.load(str(folder))
        ref = m2v.StaticModel.from_pretrained(str(folder), force_download=False)
        xs = _sentences()
        a, b = ours.encode(xs), ref.encode(xs)
        assert a.dtype == b.dtype
        assert np.array_equal(a, b)
        assert ours.dim == ref.dim
        # single-string form returns 1-D, like StaticModel.encode
        assert np.array_equal(ours.encode(xs[0]), ref.encode(xs[0]))

    def test_batches_match_across_the_1024_boundary(self, tiny):
        """A zero row promotes its whole 1024-sentence batch to float64 in model2vec,
        so batching changes the low bits. The direct loader must batch the same way."""
        m2v = _model2vec()
        xs = ["alpha beta"] * 1030
        xs[1027] = ""  # the empty row lands in the SECOND batch only
        ours = static_direct.load(str(tiny))
        ref = m2v.StaticModel.from_pretrained(str(tiny), force_download=False)
        assert np.array_equal(ours.encode(xs), ref.encode(xs))

    def test_float16_table_matches(self, tmp_path):
        m2v = _model2vec()
        folder = _write_model(tmp_path / "h", dtype="float16")
        xs = _sentences()
        ref = m2v.StaticModel.from_pretrained(str(folder), force_download=False)
        assert np.array_equal(static_direct.load(str(folder)).encode(xs), ref.encode(xs))

    def test_cached_default_model_matches(self):
        """The real potion-base-8M, when this machine has it cached (never downloads)."""
        m2v = _model2vec()
        name = embeddings._DEFAULT_MODEL2VEC_MODEL
        try:
            ours = static_direct.load(name)
        except static_direct.Unsupported:
            pytest.skip(f"{name} not in the local HF cache")
        ref = m2v.StaticModel.from_pretrained(name, force_download=False)
        xs = _sentences() + ["how does the per turn recall hook pick knowledge rows"]
        assert np.array_equal(ours.encode(xs), ref.encode(xs))


class TestFallback:
    """Each case must make load() raise Unsupported, and _build_backend must then
    hand the name to model2vec (spied, so no real load or network) with a debug line."""

    CASES = {
        "vocab_quantized": dict(extra_tensors={"mapping": np.arange(24, dtype="int64")}),
        "token_weights": dict(extra_tensors={"weights": np.ones(24, dtype="float32")}),
        "other_model_type": dict(model_type="bert"),
        "table_rows_mismatch": dict(rows_delta=3),
        "integer_table": dict(dtype="int8"),
    }

    @pytest.mark.parametrize("case", sorted(CASES) + ["no_tokenizer_json", "not_cached"])
    def test_unsupported_falls_back_to_model2vec(self, tmp_path, caplog, case):
        m2v = _model2vec()
        if case == "not_cached":
            name = "nobody/not-a-cached-model"
        else:
            folder = _write_model(tmp_path / case, **self.CASES.get(case, {}))
            if case == "no_tokenizer_json":
                (folder / "tokenizer.json").unlink()
            name = str(folder)

        with pytest.raises(static_direct.Unsupported):
            static_direct.load(name)

        class _Fake:
            dim = 8

        calls = []

        def _spy(path, **kw):
            calls.append((path, kw))
            return _Fake()

        with (
            patch.dict(os.environ, {"HF_HUB_CACHE": str(tmp_path / "empty-hub")}),
            patch.object(m2v.StaticModel, "from_pretrained", staticmethod(_spy)),
            caplog.at_level(logging.DEBUG, logger="techne.core.embeddings"),
        ):
            backend = embeddings._build_backend("model2vec", name, allow_download=False)

        assert calls == [(name, {"force_download": False})]
        assert isinstance(backend._impl, _Fake)
        assert backend.name == f"model2vec:{name}"
        assert any("static_direct unavailable" in r.getMessage() for r in caplog.records)

    def test_supported_model_does_not_touch_model2vec(self, tiny):
        m2v = _model2vec()

        def _boom(*a, **k):
            raise AssertionError("model2vec loader called for a supported model")

        with patch.object(m2v.StaticModel, "from_pretrained", staticmethod(_boom)):
            backend = embeddings._build_backend("model2vec", str(tiny), allow_download=False)
        assert isinstance(backend._impl, static_direct.DirectStaticModel)


def _hub_with(tmp_path: Path, repo: str) -> Path:
    hub = tmp_path / "hub"
    snap = hub / f"models--{repo.replace('/', '--')}" / "snapshots" / "rev1"
    _write_model(snap)
    return hub


class TestOffline:
    def test_hub_cache_hit_needs_no_network(self, tmp_path, monkeypatch):
        """Resolve an org/name id from the HF cache with every socket refused and the
        hub's download functions rigged to raise: the fast path must not care."""
        import socket

        hub = _hub_with(tmp_path, "org/tiny")
        monkeypatch.setenv("HF_HUB_CACHE", str(hub))
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")

        def _no_net(*a, **k):
            raise AssertionError("network touched")

        monkeypatch.setattr(socket.socket, "connect", _no_net)
        try:
            import huggingface_hub

            monkeypatch.setattr(huggingface_hub, "snapshot_download", _no_net)
            monkeypatch.setattr(huggingface_hub, "hf_hub_download", _no_net)
        except ImportError:
            pass

        backend = embeddings._build_backend("model2vec", "org/tiny", allow_download=True)
        assert isinstance(backend._impl, static_direct.DirectStaticModel)
        assert backend.dim == 8

    def test_newest_snapshot_wins_like_model2vec(self, tmp_path, monkeypatch):
        hub = tmp_path / "hub"
        snaps = hub / "models--org--tiny" / "snapshots"
        old = _write_model(snaps / "old", seed=1)
        new = _write_model(snaps / "new", seed=2)
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        monkeypatch.setenv("HF_HUB_CACHE", str(hub))
        assert static_direct.resolve_local("org/tiny") == new

    def test_fresh_process_imports_neither_model2vec_nor_huggingface_hub(self, tmp_path):
        """The whole point of #1096, checked where it is decided: a new interpreter
        (a hook) builds the singleton and embeds without either module in sys.modules."""
        hub = _hub_with(tmp_path, "org/tiny")
        script = textwrap.dedent(
            """
            import sys
            from techne.core import embeddings as E
            E._select_backend = lambda cfg: ("model2vec", "org/tiny")
            assert E.backend_available()
            m = E._get_embedding_model(allow_download=False)
            v = list(m.embed(["alpha beta"]))
            loaded = ["model2vec" in sys.modules, "huggingface_hub" in sys.modules]
            print(m.name, m.dim, len(v[0]), *loaded)
            """
        )
        env = {**os.environ, "HF_HUB_CACHE": str(hub), "HF_HUB_OFFLINE": "1"}
        out = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["model2vec:org/tiny", "8", "8", "False", "False"]


class TestIdentity:
    def test_same_name_and_dim_as_model2vec_backend(self, tiny):
        _model2vec()
        fast = embeddings._build_backend("model2vec", str(tiny), allow_download=False)
        with patch.object(static_direct, "load", side_effect=static_direct.Unsupported("forced")):
            slow = embeddings._build_backend("model2vec", str(tiny), allow_download=False)
        assert type(fast._impl).__name__ == "DirectStaticModel"
        assert type(slow._impl).__name__ == "StaticModel"
        assert (fast.name, fast.dim) == (slow.name, slow.dim)
        xs = _sentences()
        assert np.array_equal(np.stack(list(fast.embed(xs))), np.stack(list(slow.embed(xs))))

    def test_stamp_from_model2vec_is_not_a_backend_swap(self, tmp_path, tiny):
        """A DB stamped by a model2vec-built backend, reopened with the fast one: no
        drop, no re-embed. A different identity would DROP the vec tables."""
        from techne.core.database import init_db
        from techne.core.extension_state import get_state
        from techne.core.search import _VEC_BACKEND_STATE_NAME, _ensure_vec_backend_identity

        _model2vec()
        with patch.object(static_direct, "load", side_effect=static_direct.Unsupported("forced")):
            slow = embeddings._build_backend("model2vec", str(tiny), allow_download=False)
        fast = embeddings._build_backend("model2vec", str(tiny), allow_download=False)

        db = tmp_path / "t.db"
        init_db(db).close()
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE IF EXISTS knowledge_vec")
        assert _ensure_vec_backend_identity(conn, slow) is False  # fresh: stamps slow's identity
        conn.execute("CREATE TABLE knowledge_vec (x)")  # stand-in for an embedded table
        conn.commit()

        assert _ensure_vec_backend_identity(conn, fast) is False
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_vec'").fetchone()
        assert get_state(conn, _VEC_BACKEND_STATE_NAME) == {"name": slow.name, "dim": slow.dim}
        conn.close()


class TestInstalledProbe:
    def test_probe_does_not_import_model2vec(self):
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from techne.core import embeddings as E; "
                "print(E._model2vec_importable(), 'model2vec' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        installed = __import__("importlib.util").util.find_spec("model2vec") is not None
        assert out.stdout.split() == [str(installed), "False"]

    def test_none_in_sys_modules_means_not_installed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "model2vec", None)
        assert embeddings._model2vec_importable() is False
