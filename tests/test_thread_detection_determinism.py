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


def test_fallback_summary_does_not_depend_on_set_order():
    """`fallback_summary` slices `files` and `entities`, so hash order decided
    WHICH FIVE were shown, not merely their order (Prudence, #873 r1) — 12
    distinct summaries across 12 seeds. It lands in `threads.summary`, which is
    FTS-indexed and which `link_threads` searches to pick a `parent_thread_id`.

    THIS TEST EXISTS BECAUSE ITS PREDECESSOR NAMED `fallback_summary` AND NEVER
    CALLED IT (Prudence, #873 r2): deleting both `sorted()` calls left 4044
    tests passing, so the gap was CONCEALED by a docstring rather than merely
    absent — worse than no test, and exactly the standard this file applies to
    the guard three functions down.

    Sorting is per-feature, not global, and that is deliberate: the outer loop
    runs over `features` in observation order, so the accumulation is
    first-touched-first. Measured by her at 1 distinct output across 12 seeds
    with the sort and 12 without.
    """
    from techne.core.threads.features import ObsFeatures
    from techne.core.threads.summary import fallback_summary

    def _feat(i, files, ents):
        return ObsFeatures(
            index=i,
            timestamp=None,
            tool_family="bash",
            repos=set(),
            file_paths=set(files),
            entities=set(ents),
            is_meta=False,
        )

    # More than max_files/max_entities, so WHICH survive the slice is decided.
    feats = [
        _feat(0, {f"f{i:02d}.py" for i in range(9)}, {f"e{i:02d}" for i in range(9)}),
        _feat(1, {f"g{i:02d}.py" for i in range(9)}, {f"h{i:02d}" for i in range(9)}),
    ]
    obs = [{"tool_name": "Bash", "content": "x", "timestamp": "2026-09-01T12:00:00"}] * 2

    got = fallback_summary(obs, features=feats, max_files=5, max_entities=5)

    # REPEATING THE CALL IN ONE PROCESS PROVES NOTHING, and an earlier version of
    # this test did exactly that: set iteration order is FIXED for the life of an
    # interpreter, so six calls always agree no matter how the sort behaves. The
    # only in-process signal is that the SHOWN slice is ordered.
    def _section(text, name):
        seg = text.split(f"{name}: ", 1)[1]
        seg = seg.split(" (+", 1)[0].split("; ", 1)[0]
        return [tok.strip().rstrip(",") for tok in seg.split(", ") if tok.strip()]

    files = _section(got, "files")
    ents = _section(got, "entities")
    assert len(files) == 5 and len(ents) == 5, (
        f"fixture did not exercise the slice: files={files} entities={ents} — both "
        "must be truncated or the ordering cannot matter"
    )
    assert files == sorted(files), (
        f"shown files are {files} — a truncated list built from a set must be "
        "ordered within each feature, or WHICH FIVE appear is decided by hash seed"
    )
    assert ents == sorted(ents), f"shown entities are {ents}, not ordered"


def test_recent_files_does_not_depend_on_set_order():
    """The sibling site: `_recent_files` returns early at `limit`, so hash order
    decided which basenames reached `resume_points` — also FTS-indexed."""
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



# DELETED HERE: `test_detection_is_identical_under_different_hash_seeds`.
#
# It never had teeth and two review rounds went into finding out why. Both halves
# of the diagnosis are Prudence's (#873 r1 and r2), not mine:
#
#   1. `python /tmp/probe.py` puts the SCRIPT's directory on `sys.path[0]`, not
#      the cwd — so `import techne` resolved to the editable install, a DIFFERENT
#      checkout from the worktree under review. It coincided only when the run
#      happened to be inside the dev tree, which is why it looked fine locally
#      and why I misdiagnosed the cause as the fixture.
#   2. The fixture yields ONE segment, and `merge_pass` returns immediately at
#      `len(segments) <= 1`, so the dominant repo never decided anything.
#
# Fixing (1) did not fix (2): with PYTHONPATH corrected it was still green under
# a reverted `_segment_dominant_repo`. A cross-process guard is worth having —
# it is the only shape that could catch a NEW hash-order site elsewhere in this
# pipeline — but a version that cannot fail is not that guard, it is eight
# subprocesses of decoration. `test_a_repo_tie_must_not_decide_a_merge` covers
# the property in-process with proven teeth.
#
# If someone builds the real cross-process canary: it needs observations that
# actually SEGMENT (several distinct tool families and real time gaps) AND a
# genuine two-way repo tie inside one of those segments.
