"""Standalone guard: every entry point the `techne` distribution declares must
resolve and import (techne#1059).

Runnable two ways:

  - ``pytest tests/test_entry_points.py`` — collected into the normal dev suite,
    where it guards the *declaration* in ``pyproject.toml`` against a typo'd
    module/attribute path. Run this way it exercises whatever `techne` the dev
    venv has on `sys.path` — usually the editable checkout.
  - ``python tests/test_entry_points.py`` — no pytest required. This is what
    ``scripts/acceptance.sh`` calls after installing the *built wheel* into a
    fresh venv and `cd`-ing away from the checkout, which is what actually
    proves the point: an editable install always satisfies these imports from
    the source tree, so a module or package-data file that never made it into
    the wheel is invisible to every other job in test.yml. This file is the
    same guard either way; only what's importable underneath it differs.

Every failure is collected and reported together rather than stopping at the
first — the point of running this after a real install is "what, if anything,
is broken", not "the first thing alphabetically".
"""

from __future__ import annotations

import importlib.metadata

import techne


def _techne_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Every entry point declared BY THE TECHNE DISTRIBUTION, any group.

    Filtered by *owning distribution*, not by group name. ``console_scripts``
    is a shared namespace — another package installed in the same venv could
    register into it. ``techne.sync_adapters`` is techne's own namespace but is
    deliberately open to other distributions registering adapters into it
    (techne/sync/adapter.py) — so "which group" and "who owns this entry point"
    are different questions, and only the second is what this file owns.
    ``Distribution.entry_points`` answers exactly that: entry points read off
    the ``techne`` distribution's own metadata.
    """
    dist = techne._distribution()
    return list(dist.entry_points)


def check_entry_points() -> list[str]:
    """Resolve and import every techne entry point target.

    Returns a list of failure descriptions (empty means every entry point
    resolved and imported cleanly). ``EntryPoint.load()`` imports the target
    module and looks up the attribute — it does not call/construct anything,
    so this proves importability, not behavior.
    """
    eps = _techne_entry_points()
    if not eps:
        return [
            f"no entry points found for distribution {techne._distribution().metadata['Name']!r} "
            "at all — metadata is missing"
        ]

    failures = []
    for ep in sorted(eps, key=lambda e: (e.group, e.name)):
        try:
            ep.load()
        except Exception as exc:  # noqa: BLE001 — collect every failure, whatever its class
            failures.append(f"[{ep.group}] {ep.name} = {ep.value} -> {type(exc).__name__}: {exc}")
    return failures


def test_all_techne_entry_points_resolve_and_import():
    failures = check_entry_points()
    assert not failures, "entry point(s) failed to resolve/import:\n" + "\n".join(
        f"  - {f}" for f in failures
    )


def test_console_scripts_group_is_not_empty():
    """A gate that finds nothing to check protects nothing (techne#488's shape)."""
    names = {ep.name for ep in _techne_entry_points() if ep.group == "console_scripts"}
    assert "techne" in names, f"expected the 'techne' console_script, found {sorted(names)!r}"


def test_sync_adapters_group_is_not_empty():
    names = {ep.name for ep in _techne_entry_points() if ep.group == "techne.sync_adapters"}
    assert {"noop", "filesystem"} <= names, (
        f"expected the built-in noop/filesystem adapters, found {sorted(names)!r}"
    )


if __name__ == "__main__":
    import sys

    failures = check_entry_points()
    if failures:
        print("FAIL: entry point(s) failed to resolve/import:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)

    n = len(_techne_entry_points())
    print(f"OK: all {n} techne entry points resolved and imported.")
