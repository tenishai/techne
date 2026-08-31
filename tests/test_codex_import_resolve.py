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
            ("from x import a, b\n", [("a", "x"), ("b", "x")]),
        ],
    )
    def test_from_import_carries_its_module(self, src, expected):
        assert _extract_import_names(_py_node(src, "import_from_statement"), "python") == expected

    def test_an_aliased_from_import_carries_NO_module(self):
        """CHANGED IN r3 — this case used to expect ``[("d", "a.b")]``.

        Prudence r3 Critical 1: the name and the module are each correct and
        wrong TOGETHER. `to_name` must be the binding in the importing scope so
        a call site matches it, but the resolver then looks that same name up
        INSIDE the source module — and after ``from a.b import c as d`` module
        ``a.b`` has no ``d``. The original name is recorded nowhere, so the pair
        cannot be resolved truthfully.

        It is not hypothetical: `documents.py` does
        ``from techne.core.stores import plain_connect as connect`` and four
        calls were bound to `DomainStore.connect`, a method none of them calls,
        and stamped `import_resolved` — which `include_inferred=False` reports
        as `exact`.
        """
        assert _extract_import_names(
            _py_node("from a.b import c as d\n", "import_from_statement"), "python"
        ) == [("d", None)]

    def test_an_aliased_ts_import_carries_no_module_either(self):
        """The same rule on the other side; `X as X` re-export is unaffected."""
        pytest.importorskip("tree_sitter_typescript")
        assert _extract_import_names(
            _ts_node("import { real as helper } from 'pkg';"), "typescript"
        ) == [("helper", None)]
        assert _extract_import_names(
            _ts_node("import { plain } from 'pkg';"), "typescript"
        ) == [("plain", "pkg")]

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
    from techne.core.migrator import user_version_io

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
    assert user_version_io.read(conn) == 3, (
        "fixture is not stamped v3 — a v4 stamp or a missing symbols table makes "
        "the assertion below fail with the CHAIN's message, sending the next "
        "reader into codex.py after a fixture bug (Prudence r2 nit 4)"
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


class TestNestedLayoutsAndLanguage:
    """Prudence r2 Importants 1 and 2 — the r1 fix, one layout and one language out.

    r1 narrowed `external` to require positive evidence; r2 showed the evidence
    test itself was flat-layout-only, so a `src/` tree still asserted the same
    false fact. techne is flat Python, which is why neither round could surface
    these from this repo's own numbers.

    EVERY TEST HERE MUST CALL `resolve_edges_by_import` ITSELF. `_db` only walks
    and indexes; it does not run the pass. The first draft of this class omitted
    that call in all five tests, so each one read the indexer's untouched
    `unresolved` and passed against every mutation — green, well-named, and
    exercising nothing. Caught by mutating `_in_repo_names` and watching nothing
    go red.
    """

    _db = TestExternalNeedsPositiveEvidence._db
    _calls = TestExternalNeedsPositiveEvidence._calls

    def test_an_unplaceable_module_under_src_stays_unresolved(self, tmp_path):
        """IMPORTANT 1. The flat version of this fixture already passed.

        `_in_repo_names` used to be the repo's TOP-level names, which under
        `src/` are {'src'} and never the package — so `mypkg` could not match
        and an unplaceable in-repo module fell through to `external`.
        """
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "src/mypkg/__init__.py": "",
                "src/mypkg/app.py": (
                    "from mypkg.nowhere import ghost\n\n\ndef run():\n    return ghost()\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        assert self._calls(db)["ghost"][0] != "external", (
            "a src/-layout module was asserted out-of-repo; the evidence test is "
            "still flat-layout-only"
        )

    def test_a_ts_monorepo_package_is_not_asserted_external(self, tmp_path):
        """IMPORTANT 1, the TypeScript half — an ordinary pnpm/turborepo tree.

        `packages/foo/src/index.ts` is indexed and is the callee. This pass
        still cannot PLACE it (`_module_candidates` offers `foo/index.ts`, not
        `foo/src/index.ts`), and that is fine — `unresolved` is the honest word.
        Asserting `external` about an indexed file is not.
        """
        pytest.importorskip("tree_sitter_typescript")
        db = self._db(
            tmp_path,
            {
                "packages/foo/src/index.ts": "export function helper() { return 1; }\n",
                "packages/bar/src/app.ts": (
                    "import { helper } from 'foo';\n"
                    "export function run() { return helper(); }\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        assert self._calls(db)["helper"][0] != "external"

    def test_a_python_call_never_binds_to_a_typescript_symbol(self, tmp_path):
        """IMPORTANT 2. Measured zero times on this repo, latent everywhere else.

        A module name absent in one language and present in the other is
        ordinary in a full-stack repo — `api`, `utils`, `types`, `config` — and
        the default `--language` list indexes both.
        """
        pytest.importorskip("tree_sitter_python")
        pytest.importorskip("tree_sitter_typescript")
        db = self._db(
            tmp_path,
            {
                "web/utils/index.ts": "export function format_date() { return 1; }\n",
                "backend/main.py": (
                    "from utils import format_date\n\n\ndef run():\n    return format_date()\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        status, to_symbol = self._calls(db)["format_date"]
        assert status != "import_resolved", (
            f"a Python call was bound to a TypeScript symbol: {to_symbol}"
        )

    def test_a_typescript_call_never_binds_to_a_python_symbol(self, tmp_path):
        """The same gate from the other side."""
        pytest.importorskip("tree_sitter_python")
        pytest.importorskip("tree_sitter_typescript")
        db = self._db(
            tmp_path,
            {
                "backend/utils.py": "def formatDate():\n    return 1\n",
                "web/app.ts": (
                    "import { formatDate } from 'utils';\n"
                    "export function run() { return formatDate(); }\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        status, to_symbol = self._calls(db)["formatDate"]
        assert status != "import_resolved", (
            f"a TypeScript call was bound to a Python symbol: {to_symbol}"
        )

    def test_two_exact_candidates_are_refused_like_two_suffix_candidates(self, tmp_path):
        """IMPORTANT 2, second half: the two branches must answer alike.

        A module that is BOTH `api.py` and `api/__init__.py` gave every exact
        hit to a `LIMIT 1` with no `ORDER BY`, so the answer was row order. The
        suffix branch already refused this.
        """
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "api.py": "def helper():\n    return 1\n",
                "api/__init__.py": "def helper():\n    return 2\n",
                "app.py": (
                    "from api import helper\n\n\ndef run():\n    return helper()\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        assert self._calls(db)["helper"][1] is None, "picked one of two exact candidates"


def test_an_external_edge_does_not_arm_name_inference(tmp_path):
    """`external` must COST something, or it is a fact with no consumer.

    Prudence r2 nit 3: `resolution_status` was written by both resolvers and read
    on codex edges by nothing — `codegraph`'s four fallbacks gated on
    `to_symbol IS NULL` alone, so an edge this pass had RULED OUT of the repo
    still name-matched a local symbol and invented exactly the hop the status
    proves impossible. Measured 1 of 599 on this repo before the fix: small, and
    a contradiction regardless.

    The fixture is the collision itself — a local `Path` alongside a call bound
    to `pathlib.Path` — because that is the only shape where the two answers
    differ.
    """
    pytest.importorskip("tree_sitter_python")
    from techne.core.codegraph import callees

    (tmp_path / "local.py").write_text("class Path:\n    pass\n")
    (tmp_path / "app.py").write_text(
        "from pathlib import Path\n\n\ndef run():\n    return Path('/tmp')\n"
    )
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.commit()
    c.close()
    for rel in ("local.py", "app.py"):
        index_file(db, tmp_path / rel, tmp_path)

    conn = sqlite3.connect(db)
    run_id = conn.execute("SELECT id FROM symbols WHERE name='run'").fetchone()[0]
    inferred_before = [c for c in callees(db, run_id) if c["resolution"] == "name-inferred"]
    conn.close()
    assert any(c["name"] == "Path" for c in inferred_before), (
        "fixture must produce a name-inferred hop BEFORE resolution or this "
        "test cannot show the status removing one"
    )

    resolve_edges_by_import(sqlite3.connect(db))

    conn = sqlite3.connect(db)
    status = conn.execute(
        "SELECT resolution_status FROM edges WHERE kind='call' AND to_name='Path'"
    ).fetchone()[0]
    conn.close()
    assert status == "external", f"fixture did not produce an external edge: {status}"

    after = [c["name"] for c in callees(db, run_id) if c["resolution"] == "name-inferred"]
    assert "Path" not in after, (
        "an edge ruled OUT of this repo still name-matched a local symbol; "
        "`external` is not being read by the inference fallback"
    )


class TestTheLookupCannotInventASymbol:
    """Prudence r3 Critical 1 — `import_resolved` was guessing, and it was LIVE.

    Three rounds argued about `external` while `import_resolved` sat unexamined.
    Four false edges existed on this repo at 0482e6f: `documents.py` does
    `from techne.core.stores import plain_connect as connect`, `stores.py` has no
    module-level `connect` but does have `DomainStore.connect`, and four calls to
    `plain_connect` were bound to a method none of them calls — reported by
    `include_inferred=False` as `exact`, i.e. 52 symbols of "proven" blast radius
    reached through edges that do not exist.

    TWO INDEPENDENT MECHANISMS, one test each. Neither needs the other.
    """

    _db = TestExternalNeedsPositiveEvidence._db
    _calls = TestExternalNeedsPositiveEvidence._calls

    def test_an_alias_never_binds_to_a_same_named_stranger(self, tmp_path):
        """MECHANISM ONE: the bound name is looked up in the source module."""
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/util.py": "def real():\n    return 1\n\n\ndef helper():\n    return 99\n",
                "app.py": (
                    "from pkg.util import real as helper\n\n\ndef run():\n    return helper()\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        status, to_symbol = self._calls(db)["helper"]
        assert to_symbol is None, (
            f"bound an aliased import to an unrelated same-named symbol: {to_symbol}"
        )
        assert status != "import_resolved"

    def test_a_module_level_import_never_binds_to_a_method(self, tmp_path):
        """MECHANISM TWO: no alias needed — a re-export plus a same-named method.

        This is the case the module docstring already promises stays
        `unresolved`: "the callee may be re-exported, conditionally defined, or
        simply not a top-level symbol — guessing here is how name-matching got
        its reputation."
        """
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/util.py": "class Thing:\n    def helper(self):\n        return 1\n",
                "app.py": (
                    "from pkg.util import helper\n\n\ndef run():\n    return helper()\n"
                ),
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        status, to_symbol = self._calls(db)["helper"]
        assert to_symbol is None, f"bound a module-level import to a method: {to_symbol}"
        assert status != "import_resolved"

    def test_a_plain_from_import_of_a_real_function_still_resolves(self, tmp_path):
        """The narrowing must not cost the status its purpose."""
        pytest.importorskip("tree_sitter_python")
        db = self._db(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/util.py": "def helper():\n    return 1\n",
                "app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper()\n",
            },
        )
        resolve_edges_by_import(sqlite3.connect(db))
        status, to_symbol = self._calls(db)["helper"]
        assert status == "import_resolved", "the honest case stopped resolving"
        assert to_symbol == "pkg/util.py::helper::function::1"


def test_a_nested_file_never_shadows_the_stdlib(tmp_path):
    """Prudence r3 Important 2 — the mirror of the r1 Criticals.

    `myapp/app.py` doing `from logging import getLogger` means the stdlib:
    `myapp` is a package, not a `sys.path` entry, so Python's import machinery
    could never reach `myapp/logging.py` that way. Suffix matching had no notion
    of stdlib and ended happily at `/logging.py`. A module that wraps the stdlib
    module it shadows is the archetype, and `logging.py`, `json.py`, `types.py`
    and `csv.py` are ordinary file names. Zero occurrences on this repo, so it
    was latent, not live.
    """
    pytest.importorskip("tree_sitter_python")
    (tmp_path / "myapp").mkdir()
    (tmp_path / "myapp" / "__init__.py").write_text("")
    (tmp_path / "myapp" / "logging.py").write_text("def getLogger(name):\n    return name\n")
    (tmp_path / "myapp" / "app.py").write_text(
        "from logging import getLogger\n\n\ndef run():\n    return getLogger('x')\n"
    )
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.commit()
    c.close()
    for rel in ("myapp/__init__.py", "myapp/logging.py", "myapp/app.py"):
        index_file(db, tmp_path / rel, tmp_path)
    resolve_edges_by_import(sqlite3.connect(db))

    conn = sqlite3.connect(db)
    try:
        to_symbol = conn.execute(
            "SELECT to_symbol FROM edges WHERE kind='call' AND to_name='getLogger'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert to_symbol is None, f"bound a stdlib import to a nested repo file: {to_symbol}"


def test_the_memo_is_keyed_by_language_not_module_alone(tmp_path):
    """Prudence r3 Important 4 — a one-line drift restores the r2 defect in full.

    The language gate lives in `_module_candidates`, but its answer is CACHED.
    Key the memo on the module alone and the first caller's language decides
    every later caller's candidates, so the second language reads the first
    one's answer out of the cache and binds across languages anyway.

    The existing cross-language tests cannot catch this: each has a single call
    edge, so nothing is ever served from the cache. This needs TWO callers, TWO
    languages and ONE module name.
    """
    pytest.importorskip("tree_sitter_python")
    pytest.importorskip("tree_sitter_typescript")
    files = {
        "backend/utils.py": "def shared():\n    return 1\n",
        "web/utils/index.ts": "export function shared() { return 2; }\n",
        "backend/main.py": "from utils import shared\n\n\ndef run():\n    return shared()\n",
        "web/app.ts": (
            "import { shared } from 'utils';\nexport function go() { return shared(); }\n"
        ),
    }
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
    resolve_edges_by_import(sqlite3.connect(db))

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT s.language, e.to_symbol FROM edges e JOIN symbols s ON s.id = e.from_symbol "
            "WHERE e.kind='call' AND e.to_name='shared' AND e.to_symbol IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "fixture resolved nothing; it cannot show a cross-language bind"
    for language, to_symbol in rows:
        expected = ".py" if language == "python" else ".ts"
        assert expected in to_symbol, (
            f"a {language} call was served a {to_symbol} binding — the memo is "
            "keyed by module alone, so the first language's answer was cached "
            "for the second"
        )


def test_an_lsp_row_with_a_null_symbol_is_still_never_overwritten(tmp_path):
    """`_OVERWRITABLE` must exclude `lsp` ON ITS OWN — Prudence r3 Important 4.

    This is the claim the PR repeats most: module docstring, arch card and PR
    body all call the ordering "a claim about evidence quality". Widening the
    tuple to `("unresolved", "lsp")` left the whole suite green, because
    `lsp_resolve` always writes `to_symbol` alongside the status and the
    `to_symbol IS NULL` clause excluded the row anyway. That is safe by
    coincidence of another component's behaviour, not by this one's rule — and
    it is precisely the invariant `_codex_v2_to_v3` exists to RESTORE, i.e. one
    the codebase already treats as violable.

    The isolating state is therefore `lsp` with a NULL symbol.
    """
    pytest.importorskip("tree_sitter_python")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "util.py").write_text("def helper():\n    return 1\n")
    (tmp_path / "app.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    return helper()\n"
    )
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.commit()
    c.close()
    for rel in ("pkg/__init__.py", "pkg/util.py", "app.py"):
        index_file(db, tmp_path / rel, tmp_path)

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE edges SET resolution_status='lsp', to_symbol=NULL "
        "WHERE kind='call' AND to_name='helper'"
    )
    conn.commit()
    assert conn.execute(
        "SELECT resolution_status, to_symbol FROM edges WHERE kind='call' AND to_name='helper'"
    ).fetchone() == ("lsp", None), "fixture must isolate _OVERWRITABLE, not the to_symbol clause"

    resolve_edges_by_import(conn)
    assert conn.execute(
        "SELECT resolution_status, to_symbol FROM edges WHERE kind='call' AND to_name='helper'"
    ).fetchone() == ("lsp", None), (
        "an lsp row was re-examined; _OVERWRITABLE is not excluding it on status alone"
    )
    conn.close()


def test_every_fallback_site_refuses_an_external_edge(tmp_path):
    """All four, not just `callees` — Prudence r3 Important 4.

    Dropping `_NOT_EXTERNAL` from `callers` or from either `reachable` direction
    left the suite green; only the `callees` site had a witness. The constant is
    interpolated into four different SQL shapes — two inside larger OR clauses,
    one a conditional fragment, one a JOIN condition — so one witness does not
    cover the others.
    """
    pytest.importorskip("tree_sitter_python")
    from techne.core.codegraph import callers, callees, reachable

    (tmp_path / "local.py").write_text("class Path:\n    pass\n")
    (tmp_path / "app.py").write_text(
        "from pathlib import Path\n\n\ndef run():\n    return Path('/tmp')\n"
    )
    db = tmp_path / "codex.db"
    c = sqlite3.connect(db)
    c.executescript(CODEX_SCHEMA)
    c.commit()
    c.close()
    for rel in ("local.py", "app.py"):
        index_file(db, tmp_path / rel, tmp_path)

    conn = sqlite3.connect(db)
    run_id = conn.execute("SELECT id FROM symbols WHERE name='run'").fetchone()[0]
    path_id = conn.execute("SELECT id FROM symbols WHERE name='Path'").fetchone()[0]
    conn.close()

    # The phantom hop must EXIST first, or none of the four assertions mean anything.
    assert any(x["name"] == "Path" for x in callees(db, run_id)), "no phantom callee to remove"
    assert any(x["id"] == run_id for x in callers(db, path_id)), "no phantom caller to remove"

    resolve_edges_by_import(sqlite3.connect(db))

    assert not [x for x in callees(db, run_id) if x["name"] == "Path"], "callees site"
    assert not [x for x in callers(db, path_id) if x["id"] == run_id], "callers site"
    assert path_id not in [
        x["id"] for x in reachable(db, run_id, direction="out", max_depth=2)
    ], "reachable direction=out site"
    assert run_id not in [
        x["id"] for x in reachable(db, path_id, direction="in", max_depth=2)
    ], "reachable direction=in site"
