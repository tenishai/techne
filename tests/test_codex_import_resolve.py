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
        """Guard one of two: a row with `to_symbol` set is never re-examined."""
        conn = sqlite3.connect(repo)
        conn.execute(
            "UPDATE edges SET resolution_status='lsp', to_symbol='FAKE_LSP_ID' "
            "WHERE kind='call' AND to_name='helper'"
        )
        conn.commit()
        resolve_edges_by_import(conn)
        assert conn.execute(
            "SELECT resolution_status, to_symbol FROM edges WHERE kind='call' AND to_name='helper'"
        ).fetchone() == ("lsp", "FAKE_LSP_ID")

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
