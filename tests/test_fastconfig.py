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


#: Every module `fastconfig` may import, anywhere in the file. An ALLOWLIST, not
#: a blocklist, and that inversion is the point of techne#489: the three previous
#: guards all asked "is this techne (or yaml)?", which is a question about
#: IDENTITY. The invariant is about COST. Prudence walked two imports past all 22
#: tests — a deferred `import yaml` inside load_fast() (16.0ms) and a module-level
#: `import tempfile` (11.9ms) — neither of which is techne, either of which
#: deletes most of the 55ms benefit while CI stays green. A blocklist can only
#: ever name the expensive things someone already thought of.
#:
#: Adding an entry here is deliberate and should be justified in the PR: the
#: module's contract is that it imports ONLY what interpreter startup has already
#: paid for.
#: `sys` is permitted and costs nothing: the interpreter has already imported it
#: before any user code runs, so it can never add to the graph (Prudence, #522
#: nit 1). `__future__` likewise — a compile-time directive, not a real import.
_FASTCONFIG_ALLOWED_IMPORTS = frozenset({"__future__", "os", "pathlib", "sys"})


class TestImportCheapness:
    def test_fastconfig_imports_only_allowlisted_modules(self):
        """THE load-bearing invariant. The whole 60ms win is that this module
        does not drag in the import graph — the graph IS the cost.

        Walks the WHOLE AST, so a deferred import inside a function body is
        caught exactly like a module-level one. That is the hole #471 finding 4
        found: a relative `from .config import techne_home` deferred inside
        `load_fast()` evaded all three guards while pulling yaml and three techne
        modules into the hot path, with CI green throughout.
        """
        # Parsed, not grepped: the module's own docstring quotes
        # `import techne.config` in its measurement table, which a string match
        # flags as an offender. Ask the AST what is actually imported.
        import ast

        src = (Path(__file__).parent.parent / "techne" / "fastconfig.py").read_text()
        offenders: list[str] = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                offenders += [
                    a.name
                    for a in node.names
                    if a.name.split(".")[0] not in _FASTCONFIG_ALLOWED_IMPORTS
                ]
            elif isinstance(node, ast.ImportFrom):
                # A RELATIVE import records the module WITHOUT its package
                # prefix (`from .config import x` is module="config", level=1),
                # so it can never be checked against the allowlist by name — any
                # relative import is a techne import by construction, and offends.
                if node.level > 0:
                    offenders.append(("." * node.level) + (node.module or "?"))
                elif (node.module or "").split(".")[0] not in _FASTCONFIG_ALLOWED_IMPORTS:
                    offenders.append(node.module or "?")
        assert not offenders, (
            f"fastconfig may import only {sorted(_FASTCONFIG_ALLOWED_IMPORTS)}, "
            f"found: {offenders}. The import graph is the cost — add to the "
            "allowlist deliberately, not by accident."
        )

    def test_fastconfig_pulls_nothing_beyond_its_allowlists_own_closure(self):
        """The allowlist above governs THIS file; this governs what the file's
        imports drag in behind them — the transitive half, which no AST can see.

        MEASURED AS A DELTA BETWEEN TWO SUBPROCESSES OF THE SAME INTERPRETER, and
        that framing is the whole correctness of this test:

            baseline:  python -c "import os, pathlib"        <- the allowlist
            target:    python -c "import techne.fastconfig"

        Both sides pay for interpreter startup AND for pathlib's own closure
        (pathlib -> glob -> re -> enum/functools/collections/...), so all of it
        cancels. What survives is only what fastconfig adds BEYOND the modules it
        is already allowed to import.

        THE FIRST VERSION OF THIS TEST WAS CALIBRATED THROUGH A CONTAMINATED
        BASELINE and would have gone red for most contributors (Prudence, #522).
        It captured `base = set(sys.modules)` INSIDE one process, after startup.
        Homebrew ships a sitecustomize.py that imports `re` at startup, dragging
        in eight more modules; all nine were already resident when `base` was
        taken, so they were subtracted out and read as FREE. On an interpreter
        without that file they are charged to fastconfig for real:

            interpreter                       old metric   this delta
            Homebrew 3.14 (dev box)                   13            4
            uv python-build-standalone 3.13           26            4   <- both measured

        The old number was not the cost of importing fastconfig; it was the cost
        MINUS whatever the local interpreter's startup happened to pre-load. A
        delta is only as clean as its baseline, and a baseline captured inside a
        running process inherits everything that process already did. Taking the
        baseline in a SEPARATE process, importing exactly the allowlist, removes
        the contamination instead of budgeting around it.

        Discrimination, measured the same way (2026-08-08):

            techne.fastconfig                 4   <- today
            ...plus `import yaml`            13
            ...plus `import tempfile`        23
            techne.config                    46   <- the thing this module avoids

        The bound is 8: double today's 4, and still far under 13, the cheapest
        offender anyone has actually smuggled past a guard.
        """

        def _module_count(stmt: str) -> int:
            out = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"import sys\n{stmt}\n"
                    "print(len({m.split('.')[0] for m in sys.modules}))",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert out.returncode == 0, out.stderr
            return int(out.stdout.strip())

        # Baseline imports the allowlist itself, so its transitive closure is on
        # BOTH sides of the subtraction and cannot inflate or deflate the result.
        baseline = _module_count("import os, pathlib")
        target = _module_count("import techne.fastconfig")
        delta = target - baseline

        assert delta <= 8, (
            f"techne.fastconfig pulls {delta} top-level modules beyond os+pathlib "
            "(budget 8). Something entered the import graph that the allowlist does "
            "not account for — that is the entire cost this module exists to avoid."
        )

    def test_importing_fastconfig_does_not_pull_yaml(self):
        """yaml alone is 17ms of the 55ms `import techne.config` cost.

        Subsumed by the budget test above, kept as a NAMED regression: yaml is
        the specific module that made the fast path necessary, and a named
        failure reads better than "the count went up" when it is the one at fault.
        """
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


class TestTheCacheCannotBeWrongOnlySlow:
    """The module's load-bearing claim, and the two ways it was false.

    "An optimisation that cannot be wrong, only occasionally slow" is the whole
    argument for reading a derived file in a hot path. A cache that silently
    disagrees with its source is exactly the failure class techne#468 exists to
    catch, so each hole gets a test rather than a comment.
    """

    def test_an_edit_during_the_parse_makes_the_cache_stale_not_poisoned(self, home, monkeypatch):
        """TOCTOU (#471 review, finding 1). Stamping AFTER the parse wrote v1's
        VALUES under v2's IDENTITY: permanently fresh-looking and permanently
        wrong, until some later edit happened to rewrite it. Stamping before the
        read turns the same interleaving into a miss, which self-corrects.

        The interleaving is forced the way it was found: a yaml.safe_load wrapper
        that rewrites config.yaml as it returns.
        """
        import yaml as _yaml

        from techne import config as config_mod

        source = home / "config.yaml"
        source.write_text("hooks:\n  codex_grep_assist: false\n")

        real_load = _yaml.safe_load

        def load_then_edit(stream):
            out = real_load(stream)
            # The edit lands after the parse, before the stamp would have been
            # taken by the old code.
            source.write_text("hooks:\n  codex_grep_assist: true\n")
            return out

        monkeypatch.setattr(config_mod.yaml, "safe_load", load_then_edit)
        Config.load()

        fast = load_fast(home)
        assert fast is None, (
            "the cache verified as FRESH while holding pre-edit values — "
            f"source says the rail is on, cache says {fast}"
        )

    def test_a_load_that_could_not_stamp_first_writes_no_cache(self, home):
        """The only remaining route to a post-parse stamp, closed by hand.

        `Config.load` takes the stamp before the read; if that stat fails it
        passes None, and a `write_cache` that then self-stamps would recreate
        finding 1 exactly. Unreachable through `load()` today — a failed stat
        means the `open()` two lines later fails and propagates — but that is a
        property of today's control flow, not of the invariant, so the skip is
        asserted rather than assumed (#471 review round 2, nit).
        """
        cfg = Config.load()
        (home / CACHE_NAME).unlink(missing_ok=True)
        cfg._refresh_fast_cache(home=home, source=home / "config.yaml", stamp=None)
        assert not (home / CACHE_NAME).exists(), (
            "wrote a cache stamped AFTER the parse — the TOCTOU, reintroduced"
        )

    def test_concurrent_writers_never_publish_a_torn_cache(self, home):
        """`replace()` is atomic for the RENAME, not for the CONTENT being
        renamed — a shared tmp filename let two writers interleave into one file
        which was then published under a fresh stamp (#471 review, finding 2).

        Divergent payloads: any read that mixes them is a tear.

        TWO THINGS THIS TEST HAD TO LEARN THE HARD WAY, both recorded because
        either one silently empties it:

        1. MUST BE PROCESSES, NOT THREADS. The first version used threads and
           passed with the fix reverted — `os.getpid()` is identical across
           threads, so a pid-unique tmp gives them no isolation and the test
           could not observe the fix in either direction.

        2. THE PAYLOAD MUST BE BIG ENOUGH TO SPLIT ACROSS write() SYSCALLS.
           Measured on this machine with 6 writer processes:

               ~200-byte payload (production size):  0 torn / 16,603 reads
               200KB payload, shared tmp         : 35 torn /  2,612 reads
               200KB payload, pid-unique tmp     :  0 torn /  2,843 reads

           So the hazard is real and the fix removes it, but at the cache's
           ACTUAL size a small write does not interleave in practice — which is
           a narrower window than the review's 217/2768 suggested, and worth
           saying rather than quietly inheriting the number. The padding below
           is what makes the mechanism observable; it is not claiming the
           production cache is 200KB.
        """
        import subprocess as sp
        import sys as _sys
        import time

        (home / "config.yaml").write_text("hooks:\n  codex_grep_assist: false\n")

        # Distinguishable payloads: db_path and the rail flag move together, so
        # any read that mixes them came from two different writers.
        pad = 200_000
        writer_src = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from techne.fastconfig import write_cache, stamp_source\n"
            "home = Path(sys.argv[1]); n = sys.argv[2]; pad = int(sys.argv[3])\n"
            "stamp = stamp_source(home / 'config.yaml')\n"
            "end = time.time() + 6\n"
            "while time.time() < end:\n"
            "    write_cache({'db_path': '/w' + n + 'x' * pad,"
            " 'hooks.codex_grep_assist': n}, home=home, stamp=stamp)\n"
        )
        procs = [
            sp.Popen(
                [_sys.executable, "-c", writer_src, str(home), str(n), str(pad)],
                stdout=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )
            for n in range(6)
        ]
        torn: list[str] = []
        reads = 0
        try:
            deadline = time.time() + 3
            while time.time() < deadline:
                got = load_fast(home)
                if got is None:
                    continue
                reads += 1
                expected = "/w" + str(got.get("hooks.codex_grep_assist")) + "x" * pad
                if got.get("db_path") != expected:
                    torn.append(str(got.get("db_path"))[:40])
        finally:
            for p in procs:
                p.kill()
                p.wait()

        assert reads, "never observed a fresh cache — the test would pass vacuously"
        assert not torn, f"published a torn cache {len(torn)}x of {reads} reads, e.g. {torn[0]}"

    def test_a_newline_in_a_value_refuses_the_write_rather_than_forging_a_key(self, home):
        """The flat format has no escaping, so a newline injects lines and can
        forge another key. Refusing the write degrades to SLOW; writing it
        degrades to WRONG (#471 review, nit 1).

        ASSERTS THE REFUSAL, NOT THE RESULTING VALUE. My first version checked
        that the rail still read False and passed with the fix reverted — because
        keys are written in fixed CACHED_KEYS order, so the real
        `hooks.codex_grep_assist=0` always lands after the forged line and
        overrides it. That is exactly the ordering immunity the review described,
        which means asserting on the value tests the ordering, not the escaping.
        """
        (home / "config.yaml").write_text("hooks:\n  codex_grep_assist: false\n")
        cache = home / CACHE_NAME
        assert not cache.exists()
        write_cache(
            {"db_path": "/tmp/x\nhooks.codex_grep_assist=1", "hooks.codex_grep_assist": 0},
            home=home,
        )
        assert not cache.exists(), (
            "wrote a cache from a value containing a newline: "
            f"{cache.read_text() if cache.exists() else ''!r}"
        )

    def test_the_cache_is_not_world_readable(self, home):
        """It discloses db_path, and everything else in ~/.techne is owner-only
        (#471 review, nit 2)."""
        (home / "config.yaml").write_text("hooks:\n  codex_grep_assist: false\n")
        Config.load()
        mode = (home / CACHE_NAME).stat().st_mode & 0o777
        assert mode == 0o600, f"cache is {oct(mode)}, expected 0o600"

    def test_per_turn_seed_is_cached_alongside_per_turn_recall(self, home):
        """user_prompt_submit gates on `per_turn_recall or per_turn_seed`. A fast
        path holding only the first would silently kill the seed rail whenever
        recall is off — cached now so the next commit cannot inherit the trap
        (#471 review, finding 3)."""
        (home / "config.yaml").write_text("hooks:\n  per_turn_seed: true\n")
        Config.load()
        fast = load_fast(home)
        assert fast is not None
        assert get_bool(fast, "hooks.per_turn_seed") is True, fast


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
