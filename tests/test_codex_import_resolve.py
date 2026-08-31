"""Import-binding resolution (techne#679).

The defect: an import edge recorded a bare name with no source module, so a call
to that name could not be tied to the module that supplied it. Measured on the
live corpus 2026-08-31 — 33,081 call edges, 1 with a resolved target, and that
one in a demo shard. Every production repo had zero.
"""

from __future__ import annotations

import sqlite3

import pytest

from techne.codex.import_resolve import resolve_edges_by_import
from techne.codex.source import _extract_import_names, index_file
from techne.core.codex import CODEX_SCHEMA


def _py_node(src: str, kind: str):
    import tree_sitter_python as tsp
    from tree_sitter import Language, Parser

    tree = Parser(Language(tsp.language())).parse(src.encode())
    for n in tree.root_node.children:
        if n.type == kind:
            return n
    raise AssertionError(f"no {kind} in {src!r}")


class TestImportParsing:
    """The module must be recorded, and its ABSENCE must be meaningful."""

    @pytest.mark.parametrize(
        "src,expected",
        [
            # A plain import binds a MODULE. `None` is the discriminator, not a
            # gap: before #679 these were indistinguishable from from-imports and
            # 890 distinct to_name values mixed the two with nothing to tell them
            # apart.
            ("import os\n", [("os", None)]),
            ("import a.b.c\n", [("c", None)]),
            # The ALIAS is what is in scope, so it is what a call site will name.
            # Recording `json` here would match no call.
            ("import json as j\n", [("j", None)]),
        ],
    )
    def test_plain_import_has_no_source_module(self, src, expected):
        assert _extract_import_names(_py_node(src, "import_statement"), "python") == expected

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("from pathlib import Path\n", [("Path", "pathlib")]),
            ("from a.b import c\n", [("c", "a.b")]),
            ("from a.b import c as d\n", [("d", "a.b")]),
            ("from x import a, b\n", [("a", "x"), ("b", "x")]),
        ],
    )
    def test_from_import_carries_its_module(self, src, expected):
        assert _extract_import_names(_py_node(src, "import_from_statement"), "python") == expected

    def test_a_relative_import_is_left_unbound_rather_than_guessed(self):
        """`from . import x` yields no module, and that is deliberate.

        Resolving a relative import needs the importing file's own package
        position, which this function does not have. Inventing a module here
        would produce a binding that resolves to the wrong file — worse than no
        binding, because `external` and `import_resolved` are both asserted as
        facts downstream.
        """
        got = _extract_import_names(
            _py_node("from . import rel\n", "import_from_statement"), "python"
        )
        assert got == [("rel", None)]


def _ts_node(src: str, kind: str = "import_statement"):
    import tree_sitter_typescript as tst
    from tree_sitter import Language, Parser

    tree = Parser(Language(tst.language_typescript())).parse(src.encode())
    for n in tree.root_node.children:
        if n.type == kind:
            return n
    raise AssertionError(f"no {kind} in {src!r}")


class TestTypeScriptImportParsing:
    """The TS branch of `_extract_import_names` — Prudence r1 Important 6.

    Every case in `TestImportParsing` is Python. The TS `module` capture is new,
    does its own quote-stripping, and is where the false-`external` of Critical 2
    lived. It went in unwitnessed.
    """

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("import { a, b } from 'pkg';", [("a", "pkg"), ("b", "pkg")]),
            ("import X from 'pkg';", [("X", "pkg")]),
            ("import * as ns from 'pkg';", [("ns", "pkg")]),
            ("import { a } from \"pkg/sub\";", [("a", "pkg/sub")]),
        ],
    )
    def test_a_bare_specifier_is_carried_with_its_quotes_stripped(self, src, expected):
        pytest.importorskip("tree_sitter_typescript")
        assert _extract_import_names(_ts_node(src), "typescript") == expected

    @pytest.mark.parametrize(
        "src",
        [
            "import { helper } from './util';",
            "import { y } from '../lib/a';",
            "import { x } from '@/components/X';",
        ],
    )
    def test_a_relative_or_aliased_specifier_is_left_unbound(self, src):
        """The Critical-2 fix, at the parser.

        A relative specifier is a path relative to the IMPORTING FILE and an
        `@/` specifier is a tsconfig alias; this parser has neither the file's
        position nor the config. Passing them through as modules sent every
        in-repo TS call edge to `external` — a false fact — because
        `_module_candidates` maps '.' to '/' and './util' becomes '//util.ts'.
        """
        pytest.importorskip("tree_sitter_typescript")
        names = _extract_import_names(_ts_node(src), "typescript")
        assert names, f"no name extracted from {src!r}"
        assert all(module is None for _, module in names), names

    def test_a_side_effect_import_binds_nothing(self):
        """`import 'x';` introduces no name, so there is nothing to bind."""
        pytest.importorskip("tree_sitter_typescript")
        assert _extract_import_names(_ts_node("import 'side-effect';"), "typescript") == []


class TestResolution:
    @pytest.fixture
    def repo(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("")
        (tmp_path / "pkg" / "util.py").write_text(
            "def helper():\n    return 1\n\n\nclass Widget:\n    pass\n"
        )
        (tmp_path / "app.py").write_text(
            "from pathlib import Path\n"
            "from pkg.util import helper, Widget\n"
            "\n\n"
            "def run():\n"
            "    helper()\n"
            "    Widget()\n"
            "    Path('/tmp')\n"
            "    mystery()\n"
        )
        db = tmp_path / "codex.db"
        c = sqlite3.connect(db)
        c.executescript(CODEX_SCHEMA)
        c.commit()
        c.close()
        for f in ("pkg/__init__.py", "pkg/util.py", "app.py"):
            index_file(db, tmp_path / f, tmp_path)
        return db

    def _statuses(self, db):
        conn = sqlite3.connect(db)
        return dict(
            conn.execute(
                "SELECT to_name, resolution_status FROM edges WHERE kind='call'"
            ).fetchall()
        )

    def test_an_in_repo_binding_resolves_to_a_symbol(self, repo):
        conn = sqlite3.connect(repo)
        rep = resolve_edges_by_import(conn)
        assert rep["resolved"] == 2, rep
        st = self._statuses(repo)
        assert st["helper"] == "import_resolved"
        assert st["Widget"] == "import_resolved"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind='call' AND to_symbol IS NOT NULL"
            ).fetchone()[0]
            == 2
        )

    def test_an_out_of_repo_binding_is_RULED_OUT_not_left_unknown(self, repo):
        """`external` is the half that makes the pass worth running.

        `unresolved` means "we could not tell". `external` means "we checked, the
        callee is `pathlib.Path`, and no symbol in this repo can be it." Only the
        second lets a caller ask for a call graph with guesses removed and get
        something back — before #679 `include_inferred=False` returned an empty
        graph on every store, because nothing had ever set `to_symbol`.
        """
        resolve_edges_by_import(sqlite3.connect(repo))
        assert self._statuses(repo)["Path"] == "external"

    def test_a_call_with_no_import_binding_stays_unresolved(self, repo):
        """Honest silence. `mystery()` is imported from nowhere, so nothing is
        claimed about it — not resolved, and not ruled out either."""
        resolve_edges_by_import(sqlite3.connect(repo))
        assert self._statuses(repo)["mystery"] == "unresolved"

    def test_rerunning_resolves_nothing_new(self, repo):
        conn = sqlite3.connect(repo)
        first = resolve_edges_by_import(conn)
        second = resolve_edges_by_import(conn)
        assert first["resolved"] == 2
        assert second["resolved"] == 0, "the pass must be idempotent on an unchanged index"

    def test_an_already_resolved_edge_is_skipped(self, repo):
        """Guard one of two: a row with `to_symbol` set is never re-examined.

        THE STATUS IS LEFT `unresolved` ON PURPOSE, and that is the whole test.
        An earlier version set `resolution_status='lsp'` AND `to_symbol`, so
        `_OVERWRITABLE` excluded the row on status alone and the `to_symbol IS
        NULL` clause was never load-bearing — deleting that clause left the
        suite green. Prudence r1 Important 4, caught by exactly the mutation
        this file already documents once, one guard over. The two tests are
        split so each isolates a single clause; splitting them is not enough if
        both halves still set both columns.
        """
        conn = sqlite3.connect(repo)
        conn.execute(
            "UPDATE edges SET to_symbol='ALREADY_BOUND' "
            "WHERE kind='call' AND to_name='helper'"
        )
        conn.commit()
        assert conn.execute(
            "SELECT resolution_status FROM edges WHERE kind='call' AND to_name='helper'"
        ).fetchone() == ("unresolved",), (
            "the fixture must leave the status OVERWRITABLE or _OVERWRITABLE "
            "excludes the row and this tests guard two again"
        )

        resolve_edges_by_import(conn)
        assert conn.execute(
            "SELECT resolution_status, to_symbol FROM edges WHERE kind='call' AND to_name='helper'"
        ).fetchone() == ("unresolved", "ALREADY_BOUND"), "guard one did not hold"

    def test_a_non_unresolved_status_is_skipped_even_with_a_null_symbol(self, repo):
        """Guard two of two, and it is the one `_OVERWRITABLE` actually carries.

        AN EARLIER VERSION OF THIS TEST WAS TOOTHLESS AND SAID SO IN ITS OWN
        DOCSTRING. It set both `resolution_status='lsp'` AND `to_symbol=<id>`,
        so the row was excluded by the query's `to_symbol IS NULL` clause and
        passed with `_OVERWRITABLE` widened to include `'lsp'` — proving the
        wrong guard. Caught by installing that exact mutation (#679 build).

        The state that isolates `_OVERWRITABLE` is a non-`unresolved` status with
        a NULL symbol, which `external` is: the pass ruled the callee out without
        resolving it. Re-examining those on every run would re-mark work already
        done and break idempotency for the ruled-out half.
        """
        conn = sqlite3.connect(repo)
        resolve_edges_by_import(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE resolution_status='external'"
        ).fetchone()[0]
        assert before == 1, "fixture must produce an external row or this tests nothing"

        second = resolve_edges_by_import(conn)
        assert second["external"] == 0, (
            "an already-external edge was re-examined; _OVERWRITABLE is not "
            "excluding non-unresolved statuses"
        )
        # ONE, not two: of the four call edges, `helper` and `Widget` are
        # excluded by `to_symbol IS NULL` and `Path` by `_OVERWRITABLE`, leaving
        # only `mystery`. Asserting two here was my own miscount — I treated the
        # external row as still a candidate, which is the exact behaviour this
        # test exists to forbid. Caught by running it.
        assert second["call_edges_examined"] == 1, (
            "only the genuinely unresolved edge should remain a candidate; got "
            f"{second['call_edges_examined']}"
        )


def test_a_pre_v4_database_gains_the_column(tmp_path):
    """The column arrives by migration on an existing index (techne#679).

    Without it the widened INSERT raises on the first indexed file, and codex
    indexing dies on every db written before v4.
    """
    from techne.core.codex import _codex_v3_to_v4

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE edges (
               from_symbol TEXT NOT NULL, to_name TEXT NOT NULL, to_symbol TEXT,
               kind TEXT NOT NULL, src_file_hash TEXT,
               resolution_status TEXT NOT NULL DEFAULT 'unresolved',
               PRIMARY KEY (from_symbol, to_name, kind))"""
    )
    conn.commit()
    _codex_v3_to_v4(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)")}
    assert "to_module" in cols
    _codex_v3_to_v4(conn)  # re-entrant
    assert "to_module" in {r[1] for r in conn.execute("PRAGMA table_info(edges)")}
    conn.close()


def test_the_v4_step_is_wired_into_the_migration_chain(tmp_path):
    """The step must be REACHED, not merely correct — Prudence r1 Important 5.

    The test above calls `_codex_v3_to_v4` directly, so it passes against a
    migration that was never registered in `_run_codex_steps`. Deleting the
    `if current < 4:` entry left the FULL suite green, which makes this the only
    test that can catch it — the same reasoning `docs/arch/codex.md` and
    `tests/test_codex_freshness.py` already give for the chain in general.

    The failure it guards is silent where it matters most. Through `techne codex
    index` a missing column surfaces as a non-zero exit (#610 doing its job),
    but through the reindex-on-edit rail `index_file` swallows `IndexFailure`
    and returns 0 — so every edit quietly stops entering the graph, with the
    widened INSERT raising against a table that never gained the column.
    """
    from techne.core.codex import CODEX_VERSION, _apply_codex_migration

    db = tmp_path / "pre_v4.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        # `symbols` MUST EXIST. `_apply_codex_migration` treats a db without it
        # as fresh, stamps the version and returns WITHOUT running any step — so
        # a fixture of `edges` alone reaches the assertion below with the version
        # already at CODEX_VERSION and no column, which is how the first draft of
        # this test failed against correct code.
        """CREATE TABLE symbols (
               id TEXT PRIMARY KEY, file TEXT NOT NULL, name TEXT NOT NULL,
               kind TEXT NOT NULL, file_hash TEXT);
           CREATE TABLE edges (
               from_symbol TEXT NOT NULL, to_name TEXT NOT NULL, to_symbol TEXT,
               kind TEXT NOT NULL, src_file_hash TEXT,
               resolution_status TEXT NOT NULL DEFAULT 'unresolved',
               PRIMARY KEY (from_symbol, to_name, kind));
           PRAGMA user_version = 3;"""
    )
    conn.commit()
    assert "to_module" not in {r[1] for r in conn.execute("PRAGMA table_info(edges)")}, (
        "fixture is not pre-v4; this test would prove nothing"
    )
    conn.close()

    _apply_codex_migration(db)

    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)")}
        assert "to_module" in cols, (
            "the v3->v4 step is not registered in the migration chain; a db "
            "written before v4 never gains the column"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CODEX_VERSION
    finally:
        conn.close()


class TestExternalNeedsPositiveEvidence:
    """`external` is asserted as a FACT, so it must not be a synonym for "no match".

    Prudence r1 Criticals 2 and 3, both demonstrated with two-file fixtures: a
    TypeScript relative import and a `src/`-layout Python import each produced a
    row claiming no symbol in this repo could be the callee, about a file that
    was sitting in the index. techne is flat-layout Python, which is exactly why
    the PR's own measurement could not surface either one.
    """

    def _db(self, tmp_path, files: dict[str, str]):
        for rel, body in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        db = tmp_path / "codex.db"
        c = sqlite3.connect(db)
        c.executescript(CODEX_SCHEMA)
        c.commit()
        c.close()
        for rel in files:
            index_file(db, tmp_path / rel, tmp_path)
        return db

    def _calls(self, db):
        conn = sqlite3.connect(db)
        try:
            return {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    "SELECT to_name, resolution_status, to_symbol FROM edges WHERE kind='call'"
                )
            }
        finally:
            conn.close()

    def test_a_src_layout_import_resolves_instead_of_claiming_external(self, tmp_path):
        """PRUDENCE r1 CRITICAL 3. `src/mypkg/util.py` is indexed and is the callee."""
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "src/mypkg/__init__.py": "",
                "src/mypkg/util.py": "def helper():\n    return 1\n",
                "src/mypkg/app.py": (
                    "from mypkg.util import helper\n\n\ndef run():\n    return helper()\n"
                ),
            },
        )
        rep = resolve_edges_by_import(sqlite3.connect(db))
        status, to_symbol = self._calls(db)["helper"]
        assert status == "import_resolved", (
            f"a file in the index was not found: {status} (report={rep})"
        )
        assert to_symbol is not None

    def test_a_ts_relative_import_is_not_asserted_external(self, tmp_path):
        """PRUDENCE r1 CRITICAL 2. './util' is a path, not a dotted module.

        It stays `unresolved` rather than resolving: this pass still cannot
        place a specifier relative to the importing file. `unresolved` is the
        honest word for that; `external` was a false fact.
        """
        pytest.importorskip("tree_sitter_typescript")
        db = self._db(
            tmp_path,
            {
                "src/util.ts": "export function helper() { return 1; }\n",
                "src/app.ts": (
                    "import { helper } from './util';\n"
                    "export function run() { return helper(); }\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        status, _ = self._calls(db)["helper"]
        assert status != "external", (
            "a relative TS specifier was asserted as out-of-repo while src/util.ts "
            "sits in the index"
        )

    def test_a_genuinely_out_of_repo_module_is_still_external(self, tmp_path):
        """The narrowing must not cost the status its meaning.

        `pathlib` names nothing at the top level of this repo, which is the
        positive evidence `external` requires.
        """
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {"app.py": "from pathlib import Path\n\n\ndef run():\n    return Path('/tmp')\n"},
        )
        resolve_edges_by_import(sqlite3.connect(db))
        assert self._calls(db)["Path"][0] == "external"

    def test_an_in_repo_shaped_module_we_cannot_place_stays_unresolved(self, tmp_path):
        """Root segment names a real top-level package, but no file matches.

        This is the case the old code called `external`. It is not evidence of
        anything except that this resolver could not map the path.
        """
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "mypkg/__init__.py": "",
                "mypkg/app.py": (
                    "from mypkg.nowhere import ghost\n\n\ndef run():\n    return ghost()\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        assert self._calls(db)["ghost"][0] == "unresolved"

    def test_two_candidates_are_refused_rather_than_guessed(self, tmp_path):
        """Suffix matching must not pick a winner when the repo offers two."""
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "a/mypkg/util.py": "def helper():\n    return 1\n",
                "b/mypkg/util.py": "def helper():\n    return 2\n",
                "a/app.py": (
                    "from mypkg.util import helper\n\n\ndef run():\n    return helper()\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        status, to_symbol = self._calls(db)["helper"]
        assert to_symbol is None, "picked one of two plausible callees"
        assert status == "unresolved"


def test_a_rebound_name_resolves_to_the_LAST_import(tmp_path):
    """A shadowed import replaces its module rather than being skipped.

    Prudence r1 nit 7: `source.py`'s nine-line comment argues this correctly and
    nothing witnessed it — reverting the replace to first-write-wins left the
    suite green. The pattern is routine, not exotic:

        try:    from ujson import loads
        except ImportError: from json import loads

    The edges table's PRIMARY KEY is (from_symbol, to_name, kind), so both
    imports collide on one row. After `from a import x` then `from b import x`
    the name `x` refers to b's, and recording a's would bind every call to the
    wrong file — with `import_resolved` asserting it as fact.
    """
    pytest.importorskip("tree_sitter_python")
    (tmp_path / "a.py").write_text("def x():\n    return 1\n")
    (tmp_path / "b.py").write_text("def x():\n    return 2\n")
    (tmp_path / "app.py").write_text(
        "from a import x\nfrom b import x\n\n\ndef run():\n    return x()\n"
    )
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.commit()
    c.close()
    for rel in ("a.py", "b.py", "app.py"):
        index_file(db, tmp_path / rel, tmp_path)

    resolve_edges_by_import(sqlite3.connect(db))

    conn = sqlite3.connect(db)
    try:
        to_symbol = conn.execute(
            "SELECT to_symbol FROM edges WHERE kind='call' AND to_name='x'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert to_symbol is not None, "the shadowed call did not resolve at all"
    assert to_symbol.startswith("b.py"), (
        f"resolved to the SHADOWED import instead of the live one: {to_symbol}"
    )
