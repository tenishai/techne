"""find_repo probes each directory once per pass (techne#1155).

Its memo was keyed by full path only, so distinct paths sharing ancestors probed
those ancestors again. A 789-row session of Linux container paths re-probed
`/home` 175 times; on macOS `/home` is an autofs automount at ~20 ms a stat, so
thread detection took 12 s and the 5 s `session_end` hook timed out every turn.

The memo now also holds one answer per directory, and on the real filesystem a
directory that is not there is tested once instead of probed for five markers.
Both must leave the answer identical to the uncached walk.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

from techne.core.threads.features import REPO_MARKERS, find_repo


def _counting(markers: dict[str, set[str]] | None = None):
    """A virtual filesystem: ``markers[dir]`` is the set of markers it holds."""
    markers = markers or {}
    seen: Counter = Counter()

    def probe(p: Path, m: str) -> bool:
        seen[(str(p), m)] += 1
        return m in markers.get(str(p), set())

    return probe, seen


def test_shared_ancestors_are_probed_once_per_pass():
    probe, seen = _counting()
    cache: dict = {}
    paths = [
        "/home/dev/.claude/settings.json",
        "/home/dev/.claude/skills/a/SKILL.md",
        "/home/dev/.cabal/config",
        "/home/dev2/.local/bin/techne",
        "/home/dev/.claude/settings.json",
    ]
    for p in paths:
        assert find_repo(p, fs_check=probe, cache=cache) is None

    dirs = {str(a) for p in paths for a in Path(p).parents}
    assert max(seen.values()) == 1, "no (directory, marker) probed twice"
    assert len(seen) == len(dirs) * len(REPO_MARKERS)
    assert sum(n for (d, _), n in seen.items() if d == "/home") == len(REPO_MARKERS)


def test_a_hit_is_memoized_for_the_paths_under_it():
    probe, seen = _counting({"/work/proj": {"pyproject.toml"}})
    cache: dict = {}

    assert find_repo("/work/proj/src/a.py", fs_check=probe, cache=cache) == "proj"
    before = sum(seen.values())
    assert find_repo("/work/proj/src/b.py", fs_check=probe, cache=cache) == "proj"
    assert sum(seen.values()) == before, "both ancestors already answered"
    assert not any(d in ("/work", "/") for d, _ in seen), "the walk stops at the hit"


def test_the_memo_never_changes_an_answer():
    """Random trees and orders: the memoized walk agrees with the plain one."""
    rng = random.Random(1155)
    names = ["home", "dev", "work", "proj", "src", "a", "b"]
    for _ in range(200):
        tree = {
            "/" + "/".join(rng.choices(names, k=rng.randint(1, 4))): {rng.choice(REPO_MARKERS)}
            for _ in range(rng.randint(0, 4))
        }
        paths = ["/" + "/".join(rng.choices(names, k=rng.randint(2, 6))) for _ in range(12)]
        probe, _ = _counting(tree)
        cache: dict = {}
        for p in paths:
            assert find_repo(p, fs_check=probe, cache=cache) == find_repo(p, fs_check=probe)


def test_an_unreadable_directory_is_still_no_marker_with_the_memo():
    """techne#963 is unchanged: a probe that raises means "no marker here", and
    the walk goes on to the next directory, memo or not."""

    def probe(p: Path, m: str) -> bool:
        if str(p) == "/srv/locked":
            raise PermissionError(13, "Permission denied")
        return str(p) == "/srv" and m == ".git"

    cache: dict = {}
    assert find_repo("/srv/locked/x/f.py", fs_check=probe, cache=cache) == "srv"
    assert find_repo("/srv/locked/y/g.py", fs_check=probe, cache=cache) == "srv"
    assert cache["\x00dir:/srv/locked"] is False


def test_on_the_real_filesystem_a_missing_directory_skips_its_markers(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    ghost = tmp_path / "ghost" / "deep"
    calls: list[str] = []
    real_exists = Path.exists

    def exists(self, *a, **k):
        calls.append(str(self.parent))
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", exists)

    assert find_repo(str(ghost / "f.py"), cache={}) == tmp_path.name
    assert str(ghost) not in calls and str(ghost.parent) not in calls
    assert calls.count(str(tmp_path)) == 1, ".git is the first marker and it is found"


def test_the_full_path_memo_still_answers_directly():
    probe, seen = _counting({"/r": {".git"}})
    cache: dict = {}
    find_repo("/r/a/b.py", fs_check=probe, cache=cache)
    n = sum(seen.values())

    assert find_repo("/r/a/b.py", fs_check=probe, cache=cache) == "r"
    assert sum(seen.values()) == n


def test_a_denied_marker_does_not_hide_the_next_one_in_the_same_directory():
    """techne#963's guard is marker-granular: a `.git` that raises while
    `pyproject.toml` reads fine still resolves the directory. Moving the probes
    into `_dir_has_marker` must keep that, memo or not."""

    def probe(p: Path, m: str) -> bool:
        if str(p) == "/srv/app" and m == ".git":
            raise PermissionError(13, "Permission denied")
        return str(p) == "/srv/app" and m == "pyproject.toml"

    assert find_repo("/srv/app/src/f.py", fs_check=probe) == "app"
    assert find_repo("/srv/app/src/f.py", fs_check=probe, cache={}) == "app"


def test_a_directory_whose_stat_raises_still_has_its_markers_probed(tmp_path, monkeypatch):
    """The `is_dir()` short-cut falls through on OSError instead of answering, so
    "could not look" is never a new early exit (Prudence, PR #1156 r1)."""
    (tmp_path / ".git").mkdir()
    real_is_dir = Path.is_dir

    def is_dir(self, *a, **k):
        if self == tmp_path:
            raise PermissionError(13, "Permission denied")
        return real_is_dir(self, *a, **k)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    assert find_repo(str(tmp_path / "src" / "f.py"), cache={}) == tmp_path.name
