"""Tests for the LSP client foundation (techne#320).

Hermetic tests drive a fake LSP server (no real language server needed). An opt-in
integration test drives real pyright to reproduce the techne#319 spike — gated on
`RUN_LSP_INTEGRATION=1` + pyright installed, so it never slows the default suite.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from techne.core.lsp_client import CallSite, Location, LspClient

FAKE = Path(__file__).parent / "fixtures" / "lsp" / "fake_lsp_server.py"


@pytest.fixture()
def fake(tmp_path):
    with LspClient([sys.executable, str(FAKE)], tmp_path) as client:
        yield client


def test_capabilities_parsed(fake):
    assert fake.supports("referencesProvider")
    assert fake.supports("callHierarchyProvider")
    assert fake.supports("definitionProvider")
    assert not fake.supports("somethingProvider")


def test_references(fake):
    assert fake.references("/repo/a.py", 0, 0) == [
        Location("/repo/a.py", 10, 4),
        Location("/repo/b.py", 20, 8),
    ]


def test_definition(fake):
    assert fake.definition("/repo/a.py", 0, 0) == [Location("/repo/a.py", 0, 4)]


def test_incoming_calls(fake):
    assert fake.incoming_calls("/repo/a.py", 0, 0) == [CallSite("caller_fn", 12, "/repo/c.py", 5, 4)]


def test_outgoing_calls(fake):
    assert fake.outgoing_calls("/repo/a.py", 0, 0) == [CallSite("callee_fn", 12, "/repo/d.py", 8, 4)]


def test_graceful_degradation_when_capability_absent(tmp_path, monkeypatch):
    # Server advertises no capabilities → every query returns None (fall back to
    # the tree-sitter graph), not an empty list or a crash.
    monkeypatch.setenv("FAKE_LSP_NO_CAPS", "1")
    with LspClient([sys.executable, str(FAKE)], tmp_path) as client:
        assert not client.supports("referencesProvider")
        assert client.references("/repo/a.py", 0, 0) is None
        assert client.incoming_calls("/repo/a.py", 0, 0) is None
        assert client.definition("/repo/a.py", 0, 0) is None


# --- opt-in real-pyright integration: reproduce the techne#319 spike ----------

@pytest.mark.skipif(
    not (shutil.which("pyright-langserver") and os.environ.get("RUN_LSP_INTEGRATION")),
    reason="set RUN_LSP_INTEGRATION=1 (and install pyright-langserver) to run",
)
def test_pyright_resolves_the_real_supersede_caller():
    repo = Path(__file__).resolve().parent.parent  # techne repo root
    target = repo / "techne/core/patterns.py"
    lines = target.read_text().splitlines()
    li = next(i for i, line in enumerate(lines) if line.startswith("def supersede("))
    ch = lines[li].index("supersede")
    with LspClient(["pyright-langserver", "--stdio"], repo) as lsp:
        assert lsp.supports("callHierarchyProvider")
        lsp.open_file(target)
        callers = lsp.incoming_calls(target, li, ch, settle=(20, 2.0))
    names = {c.name for c in callers}
    # The query codex botched: the real caller resolves, the unrelated same-named
    # knowledge.supersede does not bleed in.
    assert "tag_pattern" in names
