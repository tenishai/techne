"""Tests for the stdlib-only hook config fast path (techne#470).

The invariant throughout: **this cache may be slow, but it must never be
wrong.** A cache that silently disagrees with its source is a FRESHNESS
contract — the failure class techne#468 exists to catch — so every miss must
degrade to the real Config rather than to a stale answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from techne.config import Config
from techne.fastconfig import (
    CACHE_NAME,
    CACHED_KEYS,
    get_bool,
    get_float,
    get_int,
    load_fast,
    write_cache,
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / ".techne"
    h.mkdir()
    (h / "config.yaml").write_text("hooks:\n  codex_grep_assist: true\n")
    monkeypatch.setenv("TECHNE_HOME", str(h))
    return h


class TestImportCheapness:
    def test_fastconfig_imports_nothing_from_techne(self):
        """THE load-bearing invariant. The whole 60ms win is that this module
        does not drag in the import graph — a single `techne.*` import here
        silently deletes the benefit, because the graph IS the cost.
        """
        # Parsed, not grepped: the module's own docstring quotes
        # `import techne.config` in its measurement table, which a string match
        # flags as an offender. Ask the AST what is actually imported.
        import ast

        src = (Path(__file__).parent.parent / "techne" / "fastconfig.py").read_text()
        offenders: list[str] = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.split(".")[0] == "techne"]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("techne"):
                offenders.append(node.module or "?")
        assert not offenders, f"fastconfig must stay stdlib-only, found: {offenders}"

    def test_importing_fastconfig_does_not_pull_yaml(self):
        """yaml alone is 17ms of the 55ms `import techne.config` cost."""
        out = subprocess.run(
            [sys.executable, "-c", "import techne.fastconfig, sys; print('yaml' in sys.modules)"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.stdout.strip() == "False", out.stderr

    def test_home_resolution_agrees_with_config(self, tmp_path, monkeypatch):
        """`_home` is duplicated from config.techne_home on purpose (importing it
        would defeat the module). Pin that the duplication stays correct."""
        from techne.config import techne_home
        from techne.fastconfig import _home

        monkeypatch.setenv("TECHNE_HOME", str(tmp_path / "x"))
        assert _home() == techne_home()
        monkeypatch.delenv("TECHNE_HOME")
        assert _home() == techne_home()


class TestFreshness:
    def test_roundtrip(self, home):
        write_cache({"db_path": "/tmp/x.db", "hooks.codex_grep_assist": 1}, home=home)
        got = load_fast(home=home)
        assert got is not None
        assert got["db_path"] == "/tmp/x.db"
        assert get_bool(got, "hooks.codex_grep_assist") is True

    def test_source_edit_invalidates(self, home):
        """THE failure this must not have: hand-edit config.yaml, cache does not
        regenerate, hooks run stale while `techne status` shows the new value.
        """
        write_cache({"db_path": "/tmp/old.db"}, home=home)
        assert load_fast(home=home) is not None

        src = home / "config.yaml"
        st = src.stat()
        src.write_text("hooks:\n  codex_grep_assist: false\n")
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

        assert load_fast(home=home) is None, "a stale cache must MISS, not serve old values"

    def test_size_change_at_same_mtime_invalidates(self, home):
        """mtime alone is not enough — size catches the same-second edit."""
        write_cache({"db_path": "/tmp/old.db"}, home=home)
        src = home / "config.yaml"
        st = src.stat()
        src.write_text("hooks:\n  codex_grep_assist: false\n  per_turn_recall: true\n")
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))  # identical mtime
        assert load_fast(home=home) is None

    def test_missing_cache_is_a_miss_not_an_error(self, home):
        assert load_fast(home=home) is None

    def test_malformed_cache_is_a_miss_not_an_error(self, home):
        (home / CACHE_NAME).write_text("this is not\x00 a cache\n@@@\n")
        assert load_fast(home=home) is None

    def test_missing_source_is_a_miss(self, home):
        write_cache({"db_path": "/tmp/x.db"}, home=home)
        (home / "config.yaml").unlink()
        assert load_fast(home=home) is None

    def test_write_without_source_writes_nothing(self, tmp_path):
        """No source → nothing to be stale against, so an uncheckable cache must
        not be written at all."""
        h = tmp_path / "nosrc"
        h.mkdir()
        write_cache({"db_path": "/tmp/x.db"}, home=h)
        assert not (h / CACHE_NAME).exists()


class TestSecretsStayOut:
    def test_allowlist_excludes_credentials(self):
        """An allowlist, not a dump: no secret may land in a plaintext file next
        to the store, and the cache must not become a second source of truth."""
        for key in CACHED_KEYS:
            assert "key" not in key and "token" not in key and "secret" not in key

    def test_unlisted_keys_are_not_written(self, home):
        write_cache({"db_path": "/tmp/x.db", "llm_api_key": "sk-SHOULD-NOT-APPEAR"}, home=home)
        assert "SHOULD-NOT-APPEAR" not in (home / CACHE_NAME).read_text()


class TestConfigIntegration:
    def test_config_load_populates_the_cache(self, home):
        assert not (home / CACHE_NAME).exists()
        Config.load()
        assert (home / CACHE_NAME).exists()
        assert load_fast(home=home) is not None

    def test_cache_matches_the_config_that_wrote_it(self, home):
        cfg = Config.load()
        fast = load_fast(home=home)
        assert fast["db_path"] == str(cfg.db_path)
        assert get_bool(fast, "hooks.codex_grep_assist") == cfg.hooks.codex_grep_assist
        assert get_float(fast, "hooks.timeout_s", -1) == cfg.hooks.timeout_s

    def test_explicit_path_does_not_clobber_the_hook_cache(self, home, tmp_path):
        """`Config.load(path=...)` with a non-default file (tests, one-off
        tooling) must not overwrite the cache the hooks read."""
        other = tmp_path / "other.yaml"
        other.write_text("hooks:\n  codex_grep_assist: false\n")
        Config.load(path=other)
        assert not (home / CACHE_NAME).exists()


class TestCoercion:
    def test_bool_forms(self):
        for truthy in ("1", "true", "True", "yes", "on"):
            assert get_bool({"k": truthy}, "k") is True
        for falsy in ("0", "false", "no", "off", ""):
            assert get_bool({"k": falsy}, "k") is False
        assert get_bool({}, "k", default=True) is True

    def test_numeric_fallbacks(self):
        assert get_float({"k": "bogus"}, "k", 5.0) == 5.0
        assert get_int({"k": "bogus"}, "k", 8) == 8
        assert get_int({}, "k", 8) == 8
