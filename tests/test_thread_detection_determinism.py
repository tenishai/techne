"""Thread detection must not depend on PYTHONHASHSEED (techne#871).

`_segment_dominant_repo` built its counts by iterating `ObsFeatures.repos`, a
SET — so the dict's insertion order was hash-seeded, and `max()` returns the
FIRST of any tie. Which repo counted as dominant therefore varied per process,
that decides segment merges, and the whole segmentation moved with it.

Measured on one 521-observation live session: the boundary SCORES were
byte-identical across seeds while the thread count came out 13 or 15. Thread ids
are content-addressed on the resulting segments, so ids moved too — which is how
a reflection-queue row stopped matching its own session's detection and read as
"superseded" one run in five, and why `reconcile --apply` (which DELETES what
that classifier returns) was deciding by coin-flip.

The unit test pins the tie-break directly. The subprocess test pins the property
end to end, because the unit test cannot see an ordering that only differs
between processes — which is exactly why this survived every in-process test the
repo already had.
"""

import subprocess
import sys

from techne.core.threads.segmentation import _segment_dominant_repo


class _F:
    def __init__(self, repos):
        self.repos = repos
        self.entities = set()
        self.is_meta = False
        self.tool_family = "bash"


def test_a_tie_resolves_by_name_not_by_insertion_order():
    """The mechanism, without needing two processes: same counts, orders swapped."""
    a = _segment_dominant_repo([0], [_F({"alpha", "beta"})])
    b = _segment_dominant_repo([0, 1], [_F({"beta"}), _F({"alpha"})])
    c = _segment_dominant_repo([0, 1], [_F({"alpha"}), _F({"beta"})])
    assert a == b == c == "alpha", (
        f"a tie resolved to {a!r}/{b!r}/{c!r} — the winner must not depend on the "
        "order the repos happened to arrive in"
    )


def test_a_clear_winner_still_wins():
    """The guard: breaking ties by name must not become 'always pick the lowest
    name'. A repo with more observations wins regardless of alphabet."""
    got = _segment_dominant_repo(
        [0, 1, 2], [_F({"zeta"}), _F({"zeta"}), _F({"alpha"})]
    )
    assert got == "zeta", f"dominance returned {got!r} — count must beat name"


def test_a_repo_tie_must_not_decide_a_merge():
    """The end-to-end guard, at `merge_pass` rather than through two processes
    (Prudence, #873 r1 — she supplied this fixture).

    REPLACES A SUBPROCESS TEST THAT WAS TESTING THE WRONG CODEBASE ENTIRELY.
    `python /tmp/.../probe.py` puts the SCRIPT's directory on `sys.path[0]`, not
    the cwd — so `import techne` fell through to the editable install and the
    probe read the dev checkout while the fix under test sat in a worktree. That,
    not the fixture, is why reverting the fix left it green across eight seeds.
    The fixture was independently degenerate (one segment; `merge_pass` returns
    early at `len(segments) <= 1`), but the import path would have defeated any
    replacement. I had diagnosed the fixture and been wrong about the cause.

    This needs no subprocesses. Rule 3 merges adjacent segments sharing a
    dominant repo, so segment A is built with a two-way repo tie and segment B
    dominated outright, with different families so Rule 4 cannot merge them on
    its own. Tie->alpha leaves the repos unequal and the segments stand;
    tie->beta merges them. Reverting `_segment_dominant_repo` gives 2 vs 1.
    """
    from datetime import datetime, timedelta

    from techne.core.threads.features import ObsFeatures
    from techne.core.threads.segmentation import merge_pass

    t0 = datetime(2026, 9, 1, 12, 0, 0)

    def _f(i, repos, fam):
        return ObsFeatures(
            index=i,
            timestamp=t0 + timedelta(seconds=60 * i),
            tool_family=fam,
            repos=set(repos),
            file_paths=set(),
            entities={f"e{i}"},
            is_meta=False,
        )

    def build(order):
        head = [_f(i, order[i], "git") for i in range(4)]
        return head + [_f(i, ["beta"], "web") for i in range(4, 7)]

    a = merge_pass([[0, 1, 2, 3], [4, 5, 6]], build([["alpha"], ["alpha"], ["beta"], ["beta"]]))
    b = merge_pass([[0, 1, 2, 3], [4, 5, 6]], build([["beta"], ["beta"], ["alpha"], ["alpha"]]))
    assert len(a) == len(b), (
        f"a repo tie changed the SEGMENTATION: {len(a)} vs {len(b)} segments. "
        "Which repo wins a tie must not decide whether two segments merge — "
        "thread ids are content-addressed on the resulting boundaries."
    )


def test_the_family_tie_break_is_path_independent():
    """`_segment_dominant_family` was changed with nothing pinning it (Prudence,
    #873 r1 Q5: 115 tests pass with it reverted), and an unpinned change should
    not stand.

    It is NOT a live hash-order defect — `tool_family` is a scalar and the loop
    runs over `segment`, a list, so its insertion order is already deterministic.
    The change is kept for the reason her Q3 gives for preferring name-ordering
    over first-seen generally: first-seen is PATH-DEPENDENT. The same set of
    observations reached by a different merge sequence arrives in a different
    order, so a tie resolves differently for a segment that is otherwise
    identical — and thread ids are content-addressed on what segmentation
    produces. Name-ordering is arbitrary but path-independent, which is the
    property that matters here.

    So this pins the property directly: one segment, two arrival orders, same
    winner.
    """
    from techne.core.threads.segmentation import _segment_dominant_family

    class _F:
        def __init__(self, fam):
            self.tool_family = fam
            self.is_meta = False

    forward = _segment_dominant_family([0, 1], [_F("git"), _F("bash")])
    reverse = _segment_dominant_family([1, 0], [_F("git"), _F("bash")])
    assert forward == reverse == "bash", (
        f"a family tie resolved to {forward!r} in one arrival order and {reverse!r} "
        "in another — the winner must not depend on the path that built the segment"
    )
    clear = _segment_dominant_family([0, 1, 2], [_F("zsh"), _F("zsh"), _F("bash")])
    assert clear == "zsh", f"count must beat name, got {clear!r}"


def test_the_entity_tie_break_matches_the_repo_one():
    """`_segment_dominant_entity` is the same defect in the file that imports the
    other two helpers (Prudence, #873 r1) — 8 distinct winners across 12
    processes on a 12-way tie, reaching `threads.label`, which is FTS-indexed and
    which `link_threads` searches to choose a persisted `parent_thread_id`."""
    from techne.core.threads.labels import _segment_dominant_entity

    class _E:
        def __init__(self, entities):
            self.entities = entities

    one = _segment_dominant_entity([0, 1], [_E({"alpha"}), _E({"beta"})])
    two = _segment_dominant_entity([0, 1], [_E({"beta"}), _E({"alpha"})])
    assert one == two == "alpha", (
        f"entity tie resolved to {one!r}/{two!r} — it must break by name, exactly "
        "as the repo and family helpers do"
    )
    clear = _segment_dominant_entity([0, 1, 2], [_E({"zeta"}), _E({"zeta"}), _E({"alpha"})])
    assert clear == "zeta", f"count must beat name, got {clear!r}"


def test_a_truncated_summary_does_not_depend_on_set_order():
    """`fallback_summary` and `_recent_files` slice their output, so hash order
    decided WHICH FIVE were shown, not merely the order (Prudence, #873 r1) —
    12 distinct summaries across 12 seeds. Both land in FTS-indexed columns."""
    from techne.core.threads.summary import _recent_files

    obs = [
        {
            "tool_name": "Read",
            "content": " ".join(f"/repo/f{i:02d}.py" for i in range(12)),
            "timestamp": "2026-09-01T12:00:00",
        }
    ]
    runs = {tuple(_recent_files(obs, limit=5)) for _ in range(5)}
    assert len(runs) == 1, f"_recent_files returned {len(runs)} different lists: {runs}"
    got = next(iter(runs))
    assert got == tuple(sorted(got)), (
        f"_recent_files returned {got} — a truncated list built from a set must be "
        "sorted, or which entries survive the cut is hash-seeded"
    )


def test_detection_is_identical_under_different_hash_seeds(tmp_path):
    """End to end, in two real processes.

    An in-process test cannot observe this: PYTHONHASHSEED is fixed for the life
    of the interpreter, so every in-process assertion agrees with itself. That is
    precisely why the defect survived the existing suite.

    THE FIXTURE NEEDS REAL REPO ROOTS. `ObsFeatures.repos` is populated by
    `find_repo`, which walks the filesystem for a `.git` — so observations naming
    invented paths produce an EMPTY repo set, no tie, and a test that passes
    against the bug. A first version of this test did exactly that. Two real repo
    dirs, and observations that name BOTH in one line, are what produce the
    two-element set whose iteration order the defect read.

    THIS TEST DOES NOT CURRENTLY DEMONSTRATE TEETH, AND SAYING SO IS THE POINT.
    Reverting the fix leaves it GREEN across all eight seeds: reproducing the
    original divergence needs a tie at a point where the dominant repo actually
    decides a MERGE, and the live 521-observation session had that while forty
    constructed observations do not reliably produce it. The unit tests above are
    what hold the fix; this one is a property guard, kept because the property is
    the real requirement and a future regression of a different shape would trip
    it. It is labelled rather than deleted so the next reader does not count it as
    coverage it has not earned.
    """
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "from techne.config import Config\n"
        "from techne.core.state import get_observations\n"
        "from techne.core.thread_orchestrator import detect_summarize_with_metadata\n"
        "db = Path(sys.argv[1])\n"
        "cfg = Config()\n"
        "cfg.db_path = db\n"
        "obs = get_observations(db, 's-871')\n"
        "threads, _ = detect_summarize_with_metadata(obs, cfg)\n"
        "rows = [[t.thread_num, t.boundary_signal, list(t.observation_indices)]\n"
        "        for t in threads]\n"
        "print(json.dumps(rows))\n"
    )

    from techne.core.database import init_db
    from techne.core.state import add_observation, ensure_session

    repos = {}
    for name in ("alpha", "beta"):
        (tmp_path / name / ".git").mkdir(parents=True)
        (tmp_path / name / "main.py").write_text("x = 1\n")
        repos[name] = tmp_path / name

    db = tmp_path / "t.db"
    init_db(db)
    ensure_session(db, "s-871")
    for i in range(40):
        # Every line names BOTH repos, so each observation's repo set has two
        # members tied at equal count — the shape whose iteration order decided
        # the dominant repo, and through it the merges.
        add_observation(
            db,
            "s-871",
            "Bash" if i % 3 else "Read",
            f"step{i}: ran {repos['alpha']}/main.py then {repos['beta']}/main.py",
        )

    out = []
    for seed in ("0", "1", "2", "3", "4", "5", "6", "7"):
        r = subprocess.run(
            [sys.executable, str(script), str(db)],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert r.returncode == 0, f"probe failed under seed {seed}: {r.stderr[-800:]}"
        out.append(r.stdout.strip())

    assert len(set(out)) == 1, (
        f"thread detection produced {len(set(out))} different segmentations across "
        "eight PYTHONHASHSEED values.\n"
        f"  one: {sorted(set(out))[0][:280]}\n  other: {sorted(set(out))[-1][:280]}\n"
        "Segmentation must not depend on set iteration order — content-addressed "
        "thread ids are derived from it, so unstable segments mean unstable ids."
    )
