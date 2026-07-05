"""Tests for `techne knowledge export` (techne#218, coherence D5).

Covers:
  - export_knowledge() writes expected files
  - output is deterministic (two runs on the same DB produce identical bytes)
  - index.md contains the expected type table and entry stubs
  - per-type files contain the expected entry headings
  - empty DB produces index-only output (no type files)
  - --include-superseded flag surfaces superseded entries
  - CLI smoke test: exit_code == 0, files created, summary line present
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.knowledge import store, supersede
from techne.core.knowledge_export import export_knowledge, query_live_knowledge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_db(p).close()
    return p


@pytest.fixture
def populated_db(db: Path) -> Path:
    """DB with a mix of types, tags, and confidence levels."""
    store(db, "Use SQLite for the personal store", type="fact", tags="techne,architecture", confidence="high")
    store(db, "Run pytest before every commit", type="procedure", tags="testing", confidence="medium")
    store(db, "Switched from postgres to sqlite", type="decision", tags="architecture", confidence="high")
    store(db, "Vector search via sqlite-vss", type="evaluation", tags="techne,search", confidence="low")
    store(db, "A second fact with no tags", type="fact", confidence="low")
    return db


@pytest.fixture
def cfg(db: Path, monkeypatch) -> Config:
    """Config pointing at a fresh DB; patches Config.load() for CLI tests."""
    c = Config()
    c.db_path = db
    c.data_dir = db.parent
    c.platform = "claude-code"
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
    return c


@pytest.fixture
def populated_cfg(populated_db: Path, monkeypatch) -> Config:
    c = Config()
    c.db_path = populated_db
    c.data_dir = populated_db.parent
    c.platform = "claude-code"
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
    return c


# ---------------------------------------------------------------------------
# query_live_knowledge
# ---------------------------------------------------------------------------


class TestQueryLiveKnowledge:
    def test_empty_db_returns_empty(self, db: Path) -> None:
        rows = query_live_knowledge(db)
        assert rows == []

    def test_returns_only_live_entries(self, db: Path) -> None:
        id1 = store(db, "original fact", type="fact", confidence="high")
        supersede(db, id1, "updated fact")
        rows = query_live_knowledge(db)
        assert len(rows) == 1
        assert rows[0]["content"] == "updated fact"

    def test_include_superseded_returns_all(self, db: Path) -> None:
        id1 = store(db, "original", type="fact", confidence="high")
        supersede(db, id1, "updated")
        rows = query_live_knowledge(db, include_superseded=True)
        assert len(rows) == 2

    def test_ordering_is_type_then_id(self, populated_db: Path) -> None:
        rows = query_live_knowledge(populated_db)
        # Check (type, id) is non-decreasing
        for a, b in zip(rows, rows[1:]):
            assert (a["type"], a["id"]) <= (b["type"], b["id"])

    def test_ordering_is_deterministic_on_repeated_calls(self, populated_db: Path) -> None:
        run1 = query_live_knowledge(populated_db)
        run2 = query_live_knowledge(populated_db)
        assert [r["id"] for r in run1] == [r["id"] for r in run2]


# ---------------------------------------------------------------------------
# export_knowledge — files written
# ---------------------------------------------------------------------------


class TestExportKnowledgeFiles:
    def test_empty_db_writes_only_index(self, db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = export_knowledge(db, out)
        assert result["total"] == 0
        assert result["by_type"] == {}
        assert (out / "index.md").exists()
        # No type files should be written for an empty DB
        md_files = list(out.glob("*.md"))
        assert md_files == [out / "index.md"]

    def test_creates_output_dir_if_missing(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "dir" / "out"
        assert not out.exists()
        export_knowledge(populated_db, out)
        assert out.is_dir()

    def test_writes_type_files_for_populated_db(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        result = export_knowledge(populated_db, out)
        assert (out / "index.md").exists()
        assert (out / "fact.md").exists()
        assert (out / "decision.md").exists()
        assert (out / "procedure.md").exists()
        assert (out / "evaluation.md").exists()
        assert result["total"] == 5
        assert result["by_type"]["fact"] == 2
        assert result["by_type"]["decision"] == 1
        assert result["by_type"]["procedure"] == 1
        assert result["by_type"]["evaluation"] == 1

    def test_superseded_entries_excluded_by_default(self, db: Path, tmp_path: Path) -> None:
        id1 = store(db, "old fact", type="fact", confidence="high")
        supersede(db, id1, "new fact")
        out = tmp_path / "out"
        result = export_knowledge(db, out)
        assert result["total"] == 1
        fact_text = (out / "fact.md").read_text()
        assert "new fact" in fact_text
        assert "old fact" not in fact_text

    def test_include_superseded_shows_all(self, db: Path, tmp_path: Path) -> None:
        id1 = store(db, "old fact", type="fact", confidence="high")
        supersede(db, id1, "new fact")
        out = tmp_path / "out"
        result = export_knowledge(db, out, include_superseded=True)
        assert result["total"] == 2


# ---------------------------------------------------------------------------
# export_knowledge — index.md content
# ---------------------------------------------------------------------------


class TestIndexContent:
    def test_index_contains_type_table(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        index = (out / "index.md").read_text()
        assert "| fact |" in index
        assert "| decision |" in index
        assert "| procedure |" in index
        assert "| evaluation |" in index

    def test_index_links_to_type_files(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        index = (out / "index.md").read_text()
        assert "[fact.md](fact.md)" in index
        assert "[decision.md](decision.md)" in index

    def test_index_stub_links_use_explicit_anchor(
        self, populated_db: Path, tmp_path: Path
    ) -> None:
        """Index stubs must link to `#e{id}` (the explicit anchor), not `#{id}`
        (which GitHub's heading auto-slug would never match)."""
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        index = (out / "index.md").read_text()
        # fact id 1 exists in the populated fixture
        assert "(fact.md#e1)" in index
        # A bare `#1` fragment must NOT appear as a link target.
        assert "(fact.md#1)" not in index

    def test_index_anchors_and_type_file_anchors_are_in_lockstep(
        self, populated_db: Path, tmp_path: Path
    ) -> None:
        """Every `#e{id}` the index links to must have a matching `id="e{id}"`
        anchor in the corresponding type file — guards against drift where a
        link points at an anchor that no longer exists (or vice versa)."""
        import re

        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        index = (out / "index.md").read_text()

        # Collect (ktype, id) pairs the index links to via #e{id}.
        link_pairs = set(re.findall(r"\(([a-z_]+)\.md#e(\d+)\)", index))
        assert link_pairs, "expected at least one #e{id} link in the index"

        for ktype, eid in link_pairs:
            type_text = (out / f"{ktype}.md").read_text()
            assert f'id="e{eid}"' in type_text, (
                f'index links {ktype}.md#e{eid} but {ktype}.md has no id="e{eid}" anchor'
            )

    def test_index_contains_entry_stubs(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        index = (out / "index.md").read_text()
        assert "Use SQLite for the personal store" in index
        assert "Run pytest before every commit" in index

    def test_index_scope_line_live_only(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        index = (out / "index.md").read_text()
        assert "superseded_by IS NULL" in index

    def test_index_scope_line_include_superseded(self, db: Path, tmp_path: Path) -> None:
        store(db, "a fact", type="fact")
        out = tmp_path / "out"
        export_knowledge(db, out, include_superseded=True)
        index = (out / "index.md").read_text()
        assert "including superseded" in index

    def test_index_total_count_correct(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        index = (out / "index.md").read_text()
        assert "**Total entries:** 5" in index


# ---------------------------------------------------------------------------
# export_knowledge — type file content
# ---------------------------------------------------------------------------


class TestTypeFileContent:
    def test_fact_file_contains_entries(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        text = (out / "fact.md").read_text()
        assert "Use SQLite for the personal store" in text
        assert "A second fact with no tags" in text

    def test_entry_shows_confidence(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        text = (out / "fact.md").read_text()
        assert "**Confidence:** high" in text

    def test_entry_shows_tags(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        text = (out / "fact.md").read_text()
        assert "`techne`" in text
        assert "`architecture`" in text

    def test_uncertain_status_has_badge(self, db: Path, tmp_path: Path) -> None:
        eid = store(db, "base fact", type="fact", confidence="high")
        # Force to uncertain by direct update
        conn = connect(db)
        conn.execute("UPDATE knowledge SET status = 'uncertain' WHERE id = ?", (eid,))
        conn.commit()
        conn.close()
        out = tmp_path / "out"
        export_knowledge(db, out)
        text = (out / "fact.md").read_text()
        assert "⚠ uncertain" in text

    def test_read_only_notice_present(self, populated_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        for f in out.glob("*.md"):
            text = f.read_text()
            assert "Read-only projection" in text


# ---------------------------------------------------------------------------
# Determinism — the core diffability guarantee
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_exports_produce_identical_files(self, populated_db: Path, tmp_path: Path) -> None:
        """Same DB, two export calls, byte-identical output."""
        out1 = tmp_path / "run1"
        out2 = tmp_path / "run2"
        export_knowledge(populated_db, out1)
        export_knowledge(populated_db, out2)

        files1 = sorted(f.name for f in out1.glob("*.md"))
        files2 = sorted(f.name for f in out2.glob("*.md"))
        assert files1 == files2, "file sets differ between runs"

        for name in files1:
            text1 = (out1 / name).read_text()
            text2 = (out2 / name).read_text()
            assert text1 == text2, f"{name} differs between runs"

    def test_adding_entry_changes_only_relevant_files(
        self, populated_db: Path, tmp_path: Path
    ) -> None:
        """Adding a fact changes fact.md and index.md, not decision.md."""
        out1 = tmp_path / "before"
        out2 = tmp_path / "after"
        export_knowledge(populated_db, out1)

        store(populated_db, "A brand new fact", type="fact", confidence="medium")
        export_knowledge(populated_db, out2)

        # fact.md and index.md should differ
        assert (out1 / "fact.md").read_text() != (out2 / "fact.md").read_text()
        assert (out1 / "index.md").read_text() != (out2 / "index.md").read_text()
        # decision.md should be identical
        assert (out1 / "decision.md").read_text() == (out2 / "decision.md").read_text()

    def test_no_generated_at_timestamp_in_output(self, populated_db: Path, tmp_path: Path) -> None:
        """Output must not contain a 'Generated at <datetime>' style header
        that would change on every run regardless of DB state."""
        out = tmp_path / "out"
        export_knowledge(populated_db, out)
        for f in out.glob("*.md"):
            text = f.read_text()
            assert "Generated at" not in text
            assert "generated at" not in text
            assert "exported at" not in text
            assert "Exported at" not in text


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def _invoke(args, env=None):
    runner = CliRunner()
    return runner.invoke(main, args, catch_exceptions=False, env=env)


class TestKnowledgeExportCLI:
    def test_command_exits_cleanly(self, populated_cfg: Config, tmp_path: Path) -> None:
        out = tmp_path / "cli-out"
        result = _invoke(["knowledge", "export", "--output", str(out)])
        assert result.exit_code == 0, result.output

    def test_command_creates_files(self, populated_cfg: Config, tmp_path: Path) -> None:
        out = tmp_path / "cli-out"
        result = _invoke(["knowledge", "export", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert (out / "index.md").exists()
        assert (out / "fact.md").exists()

    def test_output_summary_line(self, populated_cfg: Config, tmp_path: Path) -> None:
        out = tmp_path / "cli-out"
        result = _invoke(["knowledge", "export", "--output", str(out)])
        assert "Exported" in result.output
        assert "5 entries" in result.output

    def test_default_output_path_used_if_not_specified(
        self, populated_cfg: Config, tmp_path: Path, monkeypatch
    ) -> None:
        """Without --output, default 'techne-knowledge' relative dir is used."""
        monkeypatch.chdir(tmp_path)
        result = _invoke(["knowledge", "export"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "techne-knowledge" / "index.md").exists()

    def test_include_superseded_flag(self, cfg: Config, tmp_path: Path) -> None:
        id1 = store(cfg.db_path, "old fact", type="fact", confidence="high")
        supersede(cfg.db_path, id1, "new fact")
        out = tmp_path / "out"
        result = _invoke(["knowledge", "export", "--output", str(out), "--include-superseded"])
        assert result.exit_code == 0, result.output
        text = (out / "fact.md").read_text()
        assert "old fact" in text
        assert "new fact" in text

    def test_cli_deterministic_across_two_runs(self, populated_cfg: Config, tmp_path: Path) -> None:
        """Two CLI invocations on the same DB produce byte-identical files."""
        out1 = tmp_path / "run1"
        out2 = tmp_path / "run2"
        r1 = _invoke(["knowledge", "export", "--output", str(out1)])
        r2 = _invoke(["knowledge", "export", "--output", str(out2)])
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        files1 = sorted(f.name for f in out1.glob("*.md"))
        files2 = sorted(f.name for f in out2.glob("*.md"))
        assert files1 == files2
        for name in files1:
            assert (out1 / name).read_text() == (out2 / name).read_text(), f"{name} differs"

    def test_no_db_prints_init_message(self, tmp_path: Path, monkeypatch) -> None:
        """Pointing at a non-existent DB gives a friendly error, not a crash."""
        c = Config()
        c.db_path = tmp_path / "nonexistent.db"
        c.data_dir = tmp_path
        c.platform = "claude-code"
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: c))
        result = _invoke(["knowledge", "export", "--output", str(tmp_path / "out")])
        assert result.exit_code == 0
        assert "techne not initialized" in result.output
