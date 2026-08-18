"""No module outside `techne/platforms/` may bind a platform at IMPORT time (#626).

Part of #57 (platform decoupling). The 2026-08-17 re-audit measured the real
coupling: 2 top-level imports of `platforms.claude_code`, 17 lazy ones inside
function bodies, and 0 hardcoded `~/.claude` literals in executable code.

THE 17 LAZY IMPORTS ARE THE INTENDED PATTERN and this test must never flag them —
they keep the dependency soft and fire only on the Claude Code path. What it
guards is the STRUCTURAL kind: a module-scope import binds the platform for every
consumer of that module, including ones running on another client.

This exists because the two that were found were found by an audit, not by a
failure. An invariant nothing enforces regrows — the same reasoning the fork-guard
repetition and the arch-card gate are built on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TECHNE_ROOT = Path(__file__).resolve().parent.parent / "techne"
PLATFORMS_DIR = TECHNE_ROOT / "platforms"


def _module_scope_platform_imports(path: Path) -> list[str]:
    """Platform imports at MODULE scope only — not those nested in a function/class."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in tree.body:  # body only == module scope; nested ones never appear here
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("techne.platforms."):
            offenders.append(f"{path.name}:{node.lineno} from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("techne.platforms."):
                    offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
    return offenders


def _core_modules() -> list[Path]:
    return [
        p
        for p in TECHNE_ROOT.rglob("*.py")
        if PLATFORMS_DIR not in p.parents and p.name != "__init__.py"
    ]


def test_no_module_scope_platform_import_outside_platforms():
    offenders: list[str] = []
    for path in _core_modules():
        offenders.extend(_module_scope_platform_imports(path))
    assert not offenders, (
        "module-scope import of a platform module outside techne/platforms/ "
        f"(techne#626 / #57): {offenders}\n"
        "Move it inside the function that needs it — a lazy import keeps the "
        "dependency soft and is the pattern the other 17 already follow."
    )


def test_the_guard_can_actually_fail(tmp_path):
    """A guard nobody has watched go red is not a guard.

    Proves the detector fires on the exact shape it exists to catch, AND that it
    stays silent on the lazy form — otherwise a green run could mean either "no
    structural imports" or "the detector never looks".
    """
    structural = tmp_path / "structural.py"
    structural.write_text(
        "from techne.platforms.claude_code.paths import memory_project_slug\n\n"
        "def f(p):\n    return memory_project_slug(p)\n"
    )
    assert _module_scope_platform_imports(structural), "the detector missed a real offender"

    lazy = tmp_path / "lazy.py"
    lazy.write_text(
        "def f(p):\n"
        "    from techne.platforms.claude_code.paths import memory_project_slug\n\n"
        "    return memory_project_slug(p)\n"
    )
    assert not _module_scope_platform_imports(lazy), (
        "the detector flagged a LAZY import — that is the intended pattern and "
        "flagging it would push people back to structural coupling"
    )


@pytest.mark.parametrize("module", ["techne.core.state", "techne.core.memory_claims"])
def test_core_modules_import_without_a_platform_package(module, monkeypatch):
    """Import-time independence, driven rather than asserted from source: these
    modules must load even if the platform package is unimportable."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("techne.platforms"):
            raise ImportError(f"platform package blocked for this test: {name}")
        return real_import(name, *args, **kwargs)

    for mod in [m for m in list(__import__("sys").modules) if m.startswith("techne.platforms")]:
        monkeypatch.delitem(__import__("sys").modules, mod, raising=False)
    monkeypatch.delitem(__import__("sys").modules, module, raising=False)
    monkeypatch.setattr(builtins, "__import__", _blocked)

    importlib.import_module(module)  # must not raise
