"""Tests for techne.core.heuristic_threads."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from techne.core import heuristic_threads as ht
from techne.core.database import connect


# --- Helpers ---------------------------------------------------------------


def obs(
    tool_name: str,
    *,
    content: str = "",
    ts: datetime | None = None,
    minutes_from_zero: float | None = None,
) -> dict:
    """Build an observation dict. Provide either `ts` or `minutes_from_zero`."""
    base = datetime(2026, 1, 1, 12, 0, 0)
    if ts is None:
        ts = base + timedelta(minutes=minutes_from_zero or 0)
    return {"tool_name": tool_name, "content": content, "timestamp": ts.isoformat()}


def fs_with(paths_to_markers: dict[str, list[str]]):
    """Build a fs_check stub. ``paths_to_markers`` maps a directory path string
    to the list of marker filenames present in it."""
    table = {str(k): set(v) for k, v in paths_to_markers.items()}

    def check(directory, marker):
        return marker in table.get(str(directory), set())

    return check


# --- resolve_tool_family ---------------------------------------------------


class TestResolveToolFamily:
    def test_builtin(self):
        assert ht.resolve_tool_family("Edit") == "filesystem"
        assert ht.resolve_tool_family("WebFetch") == "web"
        assert ht.resolve_tool_family("Agent") == "meta"

    def test_bash_git_promotes_to_git_family(self):
        assert ht.resolve_tool_family("Bash", "git status") == "git"
        assert ht.resolve_tool_family("Bash", "git push origin main") == "git"
        # 'git' in content but not as a command — still bash
        assert ht.resolve_tool_family("Bash", "ls /opt/digital") == "bash"

    def test_user_override(self):
        families = {"Edit": "writing", "MyCustom": "research"}
        assert ht.resolve_tool_family("Edit", user_families=families) == "writing"
        assert ht.resolve_tool_family("MyCustom", user_families=families) == "research"

    def test_mcp_convention(self):
        assert ht.resolve_tool_family("mcp__slack__send_message") == "mcp:slack"
        assert ht.resolve_tool_family("mcp__claude_ai_Datadog__search_logs") == "mcp:claude_ai_datadog"

    def test_unknown_falls_to_other(self):
        assert ht.resolve_tool_family("WhoKnows") == "other"


# --- extract_file_paths + find_repo + extract_entities ---------------------


class TestExtractFilePaths:
    def test_simple_path(self):
        basenames, full = ht.extract_file_paths("edited /Users/me/proj/src/auth.py")
        assert basenames == {"auth.py"}
        assert "/Users/me/proj/src/auth.py" in full

    def test_multiple_paths(self):
        bn, _ = ht.extract_file_paths(
            "diff /a/b/foo.go and /c/d/bar.go are different"
        )
        assert bn == {"foo.go", "bar.go"}

    def test_no_paths(self):
        bn, full = ht.extract_file_paths("just some text")
        assert bn == set()
        assert full == []


class TestFindRepo:
    def test_walks_up_to_git(self):
        fs = fs_with({"/Users/me/proj": [".git"]})
        assert ht.find_repo("/Users/me/proj/src/deep/file.py", fs_check=fs) == "proj"

    def test_pyproject_marker(self):
        fs = fs_with({"/work/svc-a": ["pyproject.toml"]})
        assert ht.find_repo("/work/svc-a/app/x.py", fs_check=fs) == "svc-a"

    def test_no_marker(self):
        fs = fs_with({})
        assert ht.find_repo("/somewhere/file.py", fs_check=fs) is None

    def test_rejects_relative(self):
        assert ht.find_repo("relative/path.py") is None


class TestExtractEntities:
    def test_url_hostnames(self):
        ents = ht.extract_entities("see https://example.com/foo and http://api.dev")
        assert "example.com" in ents
        assert "api.dev" in ents

    def test_extra_patterns(self):
        slack_channel = re.compile(r"\b(C[A-Z0-9]{8,12})\b")
        ents = ht.extract_entities("posted in C270YFVHS", extra_patterns=[slack_channel])
        assert "C270YFVHS" in ents


# --- extract_features ------------------------------------------------------


class TestParseTimestamp:
    def test_naive_passthrough(self):
        # Naive ISO string returns naive datetime
        dt = ht._parse_timestamp("2026-06-09T16:00:00")
        assert dt.tzinfo is None
        assert dt.year == 2026

    def test_z_suffix_normalized_to_utc_naive(self):
        # Regression: JSONL timestamps are tz-aware (Z); _humanize_relative
        # subtracts against datetime.now() which is naive. They must match.
        dt = ht._parse_timestamp("2026-06-09T16:00:00Z")
        assert dt.tzinfo is None
        assert dt.hour == 16

    def test_offset_normalized_to_utc_naive(self):
        dt = ht._parse_timestamp("2026-06-09T12:00:00-04:00")
        assert dt.tzinfo is None
        # 12:00 -04:00 → 16:00 UTC
        assert dt.hour == 16

    def test_empty_returns_min(self):
        assert ht._parse_timestamp("") == datetime.min

    def test_invalid_returns_min(self):
        assert ht._parse_timestamp("not a timestamp") == datetime.min


class TestExtractFeatures:
    def test_composes(self):
        fs = fs_with({"/work/proj": [".git"]})
        f = ht.extract_features(
            0,
            obs("Edit", content="modified /work/proj/src/x.py", minutes_from_zero=0),
            fs_check=fs,
        )
        assert f.index == 0
        assert f.tool_family == "filesystem"
        assert "x.py" in f.file_paths
        assert "proj" in f.repos
        assert f.is_meta is False

    def test_meta_flag(self):
        f = ht.extract_features(5, obs("ToolSearch", content="search:foo"))
        assert f.is_meta is True
        assert f.tool_family == "meta"


# --- score_boundaries ------------------------------------------------------


class TestScoreBoundaries:
    def test_no_signals_short_gap_same_family(self):
        feats = [
            ht.extract_features(0, obs("Edit", content="a", minutes_from_zero=0)),
            ht.extract_features(1, obs("Edit", content="b", minutes_from_zero=1)),
        ]
        scores = ht.score_boundaries(feats)
        assert len(scores) == 1
        assert scores[0].total == 0

    def test_hard_gap_alone_crosses_threshold(self):
        feats = [
            ht.extract_features(0, obs("Edit", content="a", minutes_from_zero=0)),
            ht.extract_features(1, obs("Edit", content="b", minutes_from_zero=70)),
        ]
        score = ht.score_boundaries(feats)[0]
        assert "gap_hard" in score.signals
        assert score.total >= 1.0

    def test_family_change_alone_below_threshold(self):
        feats = [
            ht.extract_features(0, obs("Edit", content="a", minutes_from_zero=0)),
            ht.extract_features(1, obs("WebFetch", content="b", minutes_from_zero=1)),
        ]
        score = ht.score_boundaries(feats)[0]
        assert "family_change" in score.signals
        assert score.total == pytest.approx(0.4)

    def test_meta_sandwich_does_not_fire_family_change(self):
        # Edit → ToolSearch → Edit: the meta in the middle shouldn't make
        # either pair register a family change.
        feats = [
            ht.extract_features(0, obs("Edit", content="a", minutes_from_zero=0)),
            ht.extract_features(1, obs("ToolSearch", content="b", minutes_from_zero=1)),
            ht.extract_features(2, obs("Edit", content="c", minutes_from_zero=2)),
        ]
        scores = ht.score_boundaries(feats)
        for s in scores:
            assert "family_change" not in s.signals

    def test_repo_change_signal(self):
        fs = fs_with({"/work/svc-a": [".git"], "/work/svc-b": [".git"]})
        feats = [
            ht.extract_features(
                0,
                obs("Edit", content="edit /work/svc-a/x.py", minutes_from_zero=0),
                fs_check=fs,
            ),
            ht.extract_features(
                1,
                obs("Edit", content="edit /work/svc-b/y.py", minutes_from_zero=1),
                fs_check=fs,
            ),
        ]
        score = ht.score_boundaries(feats)[0]
        assert "repo_change" in score.signals


class TestUserPromptSignal:
    """A UserPrompt is a candidate thread anchor: it amplifies real signals
    but never crosses the threshold on its own."""

    def test_prompt_after_tool_short_gap_below_threshold(self):
        # Mid-flow prompt — same minute, no other signals. Should NOT break.
        feats = [
            ht.extract_features(0, obs("Edit", content="a", minutes_from_zero=0)),
            ht.extract_features(1, obs("UserPrompt", content="check disk", minutes_from_zero=0)),
        ]
        score = ht.score_boundaries(feats)[0]
        assert score.signals.get("user_prompt") == ht.USER_PROMPT_SCORE
        # family_change must be suppressed — otherwise filesystem→user_prompt
        # would fire it and push the total to threshold.
        assert "family_change" not in score.signals
        assert score.total < ht.BOUNDARY_THRESHOLD

    def test_prompt_with_medium_gap_crosses_threshold(self):
        # 30-min pause then prompt: clean break.
        feats = [
            ht.extract_features(0, obs("Edit", content="a", minutes_from_zero=0)),
            ht.extract_features(1, obs("UserPrompt", content="new topic", minutes_from_zero=35)),
        ]
        score = ht.score_boundaries(feats)[0]
        assert "user_prompt" in score.signals
        assert "gap_medium" in score.signals
        assert score.total >= ht.BOUNDARY_THRESHOLD

    def test_prompt_with_soft_gap_below_threshold(self):
        # 12-min pause: still amplifying, but not enough.
        feats = [
            ht.extract_features(0, obs("Edit", content="a", minutes_from_zero=0)),
            ht.extract_features(1, obs("UserPrompt", content="another thought", minutes_from_zero=12)),
        ]
        score = ht.score_boundaries(feats)[0]
        assert score.total == pytest.approx(0.6)
        assert score.total < ht.BOUNDARY_THRESHOLD

    def test_prompt_with_repo_change_crosses_threshold(self):
        fs = fs_with({"/work/svc-a": [".git"], "/work/svc-b": [".git"]})
        feats = [
            ht.extract_features(
                0,
                obs("Edit", content="edit /work/svc-a/x.py", minutes_from_zero=0),
                fs_check=fs,
            ),
            ht.extract_features(
                1,
                obs("UserPrompt", content="now work on /work/svc-b/y.py", minutes_from_zero=1),
                fs_check=fs,
            ),
        ]
        score = ht.score_boundaries(feats)[0]
        assert "user_prompt" in score.signals
        assert "repo_change" in score.signals
        assert score.total >= ht.BOUNDARY_THRESHOLD

    def test_two_prompts_in_a_row_no_family_change(self):
        # Back-to-back user prompts — same family, no family_change either way;
        # user_prompt only fires for the right-hand obs of each pair.
        feats = [
            ht.extract_features(0, obs("UserPrompt", content="one", minutes_from_zero=0)),
            ht.extract_features(1, obs("UserPrompt", content="two", minutes_from_zero=1)),
        ]
        score = ht.score_boundaries(feats)[0]
        assert "family_change" not in score.signals
        assert score.signals.get("user_prompt") == ht.USER_PROMPT_SCORE

    def test_prompt_then_tool_does_fire_family_change(self):
        # Suppression is direction-aware: prompt → tool DOES change family,
        # and there's no user_prompt signal on the right-hand side.
        feats = [
            ht.extract_features(0, obs("UserPrompt", content="check it", minutes_from_zero=0)),
            ht.extract_features(1, obs("Edit", content="a", minutes_from_zero=1)),
        ]
        score = ht.score_boundaries(feats)[0]
        assert "family_change" in score.signals
        assert "user_prompt" not in score.signals


# --- split + merge ---------------------------------------------------------


class TestSplitAtBoundaries:
    def test_no_observations(self):
        assert ht.split_at_boundaries(0, []) == []

    def test_single_observation(self):
        assert ht.split_at_boundaries(1, []) == [[0]]

    def test_one_boundary(self):
        scores = [
            ht.BoundaryScore(
                a_index=1, b_index=2, gap_seconds=4000, family_a="x", family_b="y",
                signals={"gap_hard": 1.0},
            ),
        ]
        assert ht.split_at_boundaries(4, scores) == [[0, 1], [2, 3]]


class TestMergePass:
    def _f(self, idx, family="filesystem", repo=None, entities=None, minutes=0):
        feat = ht.ObsFeatures(
            index=idx,
            timestamp=datetime(2026, 1, 1, 12, 0) + timedelta(minutes=minutes),
            tool_family=family,
            is_meta=(family == "meta"),
        )
        if repo:
            feat.repos = {repo}
        if entities:
            feat.entities = set(entities)
        return feat

    def test_no_op_when_one_segment(self):
        feats = [self._f(0), self._f(1)]
        assert ht.merge_pass([[0, 1]], feats) == [[0, 1]]

    def test_tiny_segment_merges_forward_by_default(self):
        # Tiny middle segment, no entity overlap with either neighbor → tie,
        # merges forward.
        feats = [self._f(i) for i in range(10)]
        segments = [[0, 1, 2], [3], [4, 5, 6, 7, 8, 9]]
        result = ht.merge_pass(segments, feats)
        assert result == [[0, 1, 2], [3, 4, 5, 6, 7, 8, 9]]

    def test_tiny_segment_picks_higher_overlap_neighbor(self):
        # Tiny segment shares an entity with the previous segment but not
        # with the next — should merge backward.
        feats = [
            self._f(0, entities={"alpha"}),
            self._f(1, entities={"alpha"}),
            self._f(2, entities={"alpha"}),  # tiny segment, shares "alpha" with [0..2]
            self._f(3, entities={"beta"}),
            self._f(4, entities={"beta"}),
            self._f(5, entities={"beta"}),
        ]
        segments = [[0, 1], [2], [3, 4, 5]]
        result = ht.merge_pass(segments, feats)
        assert result == [[0, 1, 2], [3, 4, 5]]

    def test_meta_only_segment_merges_forward(self):
        feats = [
            self._f(0, "filesystem"),
            self._f(1, "filesystem"),
            self._f(2, "filesystem"),
            self._f(3, "meta"),
            self._f(4, "meta"),
            self._f(5, "datadog"),
            self._f(6, "datadog"),
            self._f(7, "datadog"),
        ]
        segments = [[0, 1, 2], [3, 4], [5, 6, 7]]
        result = ht.merge_pass(segments, feats)
        assert [3, 4] not in result
        # Meta segment landed with the datadog segment, not the filesystem one.
        assert [3, 4, 5, 6, 7] in result or any(3 in s and 5 in s for s in result)

    def test_same_repo_segments_merge_within_hard_gap(self):
        feats = [
            self._f(0, repo="alpha", minutes=0),
            self._f(1, repo="alpha", minutes=1),
            self._f(2, repo="alpha", minutes=2),
            self._f(3, repo="alpha", minutes=3),
            self._f(4, repo="alpha", minutes=4),
            self._f(5, repo="alpha", minutes=5),
        ]
        segments = [[0, 1, 2], [3, 4, 5]]
        result = ht.merge_pass(segments, feats)
        assert result == [[0, 1, 2, 3, 4, 5]]

    def test_continuing_investigation_merges_same_family_with_entity_overlap(self):
        # Rule 4: same dominant family + shared entities + gap < 3h → merge.
        # This is the long-investigation pattern the same-repo rule can't
        # catch when work is over services/dashboards instead of code.
        feats = [
            self._f(0, "mcp:datadog", entities={"papi"}, minutes=0),
            self._f(1, "mcp:datadog", entities={"papi", "capig"}, minutes=1),
            self._f(2, "mcp:datadog", entities={"capig"}, minutes=2),
            # 2h50m gap — would normally split via gap_hard
            self._f(3, "mcp:datadog", entities={"papi", "capig"}, minutes=172),
            self._f(4, "mcp:datadog", entities={"papi"}, minutes=173),
            self._f(5, "mcp:datadog", entities={"capig"}, minutes=174),
        ]
        segments = [[0, 1, 2], [3, 4, 5]]
        result = ht.merge_pass(segments, feats)
        assert result == [[0, 1, 2, 3, 4, 5]]

    def test_continuing_investigation_does_not_merge_past_cutoff(self):
        # 4 hours apart — beyond CONTINUING_INVESTIGATION_GAP, no merge
        # even with shared entities and same family.
        feats = [
            self._f(0, "mcp:datadog", entities={"papi"}, minutes=0),
            self._f(1, "mcp:datadog", entities={"papi"}, minutes=1),
            self._f(2, "mcp:datadog", entities={"papi"}, minutes=2),
            self._f(3, "mcp:datadog", entities={"papi"}, minutes=240),
            self._f(4, "mcp:datadog", entities={"papi"}, minutes=241),
            self._f(5, "mcp:datadog", entities={"papi"}, minutes=242),
        ]
        segments = [[0, 1, 2], [3, 4, 5]]
        result = ht.merge_pass(segments, feats)
        assert result == [[0, 1, 2], [3, 4, 5]]

    def test_same_repo_does_not_merge_across_huge_gap(self):
        feats = [
            self._f(0, repo="alpha", minutes=0),
            self._f(1, repo="alpha", minutes=1),
            self._f(2, repo="alpha", minutes=2),
            self._f(3, repo="alpha", minutes=120),  # 2-hour gap
            self._f(4, repo="alpha", minutes=121),
            self._f(5, repo="alpha", minutes=122),
        ]
        segments = [[0, 1, 2], [3, 4, 5]]
        result = ht.merge_pass(segments, feats)
        assert result == [[0, 1, 2], [3, 4, 5]]


# --- detect_threads end-to-end --------------------------------------------


class TestDetectThreadsEndToEnd:
    """Smaller-than-the-worked-example scenarios exercising the full pipeline."""

    def test_empty(self):
        segments, scores = ht.detect_threads([])
        assert segments == []
        assert scores == []

    def test_single_topic_session_collapses_to_one_thread(self):
        # All Edit calls within a few minutes, same repo.
        fs = fs_with({"/proj": [".git"]})
        observations = [
            obs("Edit", content=f"line {i} of /proj/src/file.py", minutes_from_zero=i)
            for i in range(8)
        ]
        segments, _ = ht.detect_threads(observations, fs_check=fs)
        assert len(segments) == 1
        assert segments[0] == list(range(8))

    def test_long_gap_splits(self):
        observations = [
            obs("Edit", content="x", minutes_from_zero=0),
            obs("Edit", content="x", minutes_from_zero=1),
            obs("Edit", content="x", minutes_from_zero=2),
            obs("Edit", content="x", minutes_from_zero=3),
            # 2-hour gap
            obs("Edit", content="x", minutes_from_zero=120),
            obs("Edit", content="x", minutes_from_zero=121),
            obs("Edit", content="x", minutes_from_zero=122),
            obs("Edit", content="x", minutes_from_zero=123),
        ]
        segments, _ = ht.detect_threads(observations)
        assert len(segments) == 2
        assert segments[0] == [0, 1, 2, 3]
        assert segments[1] == [4, 5, 6, 7]

    def test_worked_example_three_threads(self):
        # Compressed version of the proposal's example session b6c2d821.
        # Topic 1: slack (with meta interleaving). Topic 2: datadog after
        # 1h15m gap, shared entity "service:papi"/"service:capig" preventing
        # internal splits across a 2h50m sub-gap (Rule 4: continuing
        # investigation). Topic 3: filesystem after a 4h49m gap.
        zero = datetime(2026, 1, 1, 15, 31)

        def at(minute, tool, content=""):
            return {
                "tool_name": tool,
                "content": content,
                "timestamp": (zero + timedelta(minutes=minute)).isoformat(),
            }

        # User-supplied entity pattern that catches Datadog service references.
        # In real use this would live in config.threads.entity_patterns.
        service_pattern = re.compile(r"service:([a-z0-9-]+)")

        observations = [
            # --- Thread 1: Slack incident response ---
            at(0, "ToolSearch", "slack tools"),
            at(0.1, "mcp__claude_ai_Slack__search", "incident-response"),
            at(1, "ToolSearch", "slack_read_channel"),
            at(1.1, "mcp__claude_ai_Slack__read", "C270YFVHS"),
            at(2, "ToolSearch", "datadog auth"),
            at(2.1, "mcp__claude_ai_Datadog__auth", "auth flow"),
            # --- 1h15m gap → Thread 2: Datadog investigation ---
            at(80, "ToolSearch", "datadog skills"),
            at(90, "mcp__claude_ai_Datadog__search_services", "service:papi"),
            at(90.1, "mcp__claude_ai_Datadog__search_services", "service:capig"),
            at(90.2, "mcp__claude_ai_Datadog__search_logs", "service:papi errors"),
            # --- 2h50m gap but same entities → Rule 4 should re-merge ---
            at(260, "mcp__claude_ai_Datadog__search_logs", "service:papi service:capig errors"),
            at(260.1, "mcp__claude_ai_Datadog__search_metrics", "service:papi"),
            at(261, "mcp__claude_ai_Datadog__get_metric", "service:capig hits"),
            # --- 4h49m gap → Thread 3: RCA writeup ---
            at(550, "mcp__claude_ai_Datadog__get_metric", "service:presentation-api"),
            at(551, "Write", "/Users/me/rca-akamai-stampede.md content"),
            at(552, "Edit", "/Users/me/rca-akamai-stampede.md"),
            at(553, "Edit", "/Users/me/rca-akamai-stampede.md"),
        ]

        segments, _ = ht.detect_threads(
            observations, extra_entity_patterns=[service_pattern]
        )
        assert len(segments) == 3, f"expected 3 threads, got {len(segments)}: {segments}"

        # Thread 1 should hold the slack observations (~indices 0-5)
        assert 0 in segments[0]
        assert 5 in segments[0]
        # Thread 2 should hold the datadog work (~indices 6-12)
        assert 6 in segments[1]
        assert 12 in segments[1]
        # Thread 3 should hold the RCA (~indices 13-16)
        assert 13 in segments[2]
        assert 16 in segments[2]


# --- Label generation ------------------------------------------------------


class TestGenerateLabel:
    def _f(self, idx, family="filesystem", repo=None, entities=None):
        feat = ht.ObsFeatures(
            index=idx,
            timestamp=datetime(2026, 1, 1),
            tool_family=family,
            is_meta=(family == "meta"),
        )
        if repo:
            feat.repos = {repo}
        if entities:
            feat.entities = set(entities)
        return feat

    def test_empty_segment(self):
        assert ht.generate_label([], []) == "Session work"

    def test_specific_family_label(self):
        feats = [self._f(0, "mcp:datadog"), self._f(1, "mcp:datadog")]
        label = ht.generate_label([0, 1], feats)
        assert "Datadog" in label
        assert "investigation" in label

    def test_specific_family_with_entity(self):
        feats = [
            self._f(0, "mcp:datadog", entities={"papi"}),
            self._f(1, "mcp:datadog", entities={"papi", "capig"}),
        ]
        label = ht.generate_label([0, 1], feats)
        assert "Datadog investigation" in label
        # One of the entities should be appended after the colon
        assert ": " in label

    def test_repo_label_for_generic_family(self):
        feats = [
            self._f(0, "filesystem", repo="techne"),
            self._f(1, "filesystem", repo="techne"),
        ]
        label = ht.generate_label([0, 1], feats)
        assert label == "Work on techne"

    def test_repo_with_entity(self):
        feats = [
            self._f(0, "filesystem", repo="techne", entities={"threads.py"}),
            self._f(1, "filesystem", repo="techne", entities={"threads.py"}),
        ]
        label = ht.generate_label([0, 1], feats)
        assert label.startswith("Work on techne")
        assert "threads.py" in label

    def test_fallback_lists_top_families(self):
        feats = [
            self._f(0, "filesystem"),
            self._f(1, "filesystem"),
            self._f(2, "bash"),
        ]
        label = ht.generate_label([0, 1, 2], feats)
        assert "Session work" in label
        assert "Filesystem" in label
        assert "Bash" in label


# --- Fallback summary ------------------------------------------------------


class TestFallbackSummary:
    def test_empty(self):
        assert ht.fallback_summary([]) == ""

    def test_observation_count(self):
        observations = [obs("Edit") for _ in range(7)]
        s = ht.fallback_summary(observations)
        assert "7 observations" in s

    def test_tool_breakdown_in_order(self):
        observations = (
            [obs("Edit") for _ in range(5)]
            + [obs("Bash") for _ in range(2)]
        )
        s = ht.fallback_summary(observations)
        # filesystem appears before bash in the tools list
        i_fs = s.find("filesystem")
        i_bash = s.find("bash")
        assert 0 < i_fs < i_bash

    def test_includes_files_section(self):
        observations = [
            obs("Edit", content="modified /a/foo.py"),
            obs("Edit", content="modified /a/bar.py"),
        ]
        s = ht.fallback_summary(observations)
        assert "files:" in s
        assert "foo.py" in s
        assert "bar.py" in s

    def test_files_overflow_indicator(self):
        observations = [
            obs("Edit", content=f"modified /a/file_{i}.py") for i in range(10)
        ]
        s = ht.fallback_summary(observations, max_files=3)
        assert "+7 more" in s

    def test_entities_separate_from_files(self):
        observations = [
            obs("Edit", content="see /a/foo.py"),
            obs("WebFetch", content="see https://example.com/x"),
        ]
        s = ht.fallback_summary(observations)
        # foo.py shows in files: section, not duplicated in entities
        assert s.count("foo.py") == 1
        assert "example.com" in s


# --- Resume points ---------------------------------------------------------


class TestBuildResumePoint:
    def test_empty(self):
        assert ht.build_resume_point([]) == ""

    def test_basic_fields(self):
        now = datetime(2026, 1, 1, 12, 30)
        observations = [
            obs("Edit", content="edited /a/b/foo.py line 12", ts=datetime(2026, 1, 1, 12, 0)),
            obs("Bash", content="pytest tests/", ts=datetime(2026, 1, 1, 12, 10)),
            obs("Edit", content="edited /a/b/foo.py line 14", ts=datetime(2026, 1, 1, 12, 26)),
        ]
        result = ht.build_resume_point(observations, now=now)
        assert "Last activity: 4m ago" in result
        assert "Last action: Edit" in result
        assert "Open files: foo.py" in result
        assert "Recent:" in result
        # Tail shows the two before the last
        assert "Edit: edited /a/b/foo.py line 12" in result
        assert "Bash: pytest tests/" in result

    def test_error_captured(self):
        observations = [
            obs("Bash", content="pytest tests/test_x.py"),
            obs(
                "Bash",
                content="FAILED tests/test_x.py::test_thing\n"
                "E   AssertionError: expected 3, got 2",
            ),
        ]
        result = ht.build_resume_point(observations, now=datetime(2026, 1, 1, 12, 30))
        assert "Last error:" in result
        assert "FAILED" in result or "AssertionError" in result

    def test_no_error_no_error_line(self):
        observations = [
            obs("Edit", content="just a happy edit", ts=datetime(2026, 1, 1, 12, 0))
        ]
        result = ht.build_resume_point(observations, now=datetime(2026, 1, 1, 12, 1))
        assert "Last error:" not in result

    def test_open_files_deduplicates_and_orders_recent_first(self):
        observations = [
            obs("Read", content="read /a/foo.py", ts=datetime(2026, 1, 1, 11, 0)),
            obs("Edit", content="edit /a/bar.py", ts=datetime(2026, 1, 1, 11, 5)),
            obs("Edit", content="edit /a/foo.py", ts=datetime(2026, 1, 1, 11, 10)),
        ]
        result = ht.build_resume_point(observations, now=datetime(2026, 1, 1, 11, 11))
        # foo.py touched most recently, then bar.py
        line = next(l for l in result.splitlines() if l.startswith("Open files:"))
        files = line.removeprefix("Open files:").strip().split(", ")
        assert files[0] == "foo.py"
        assert "bar.py" in files

    def test_long_content_truncates(self):
        big = "x" * 500
        observations = [obs("Edit", content=big, ts=datetime(2026, 1, 1, 12, 0))]
        result = ht.build_resume_point(observations, now=datetime(2026, 1, 1, 12, 1))
        # No line in the result should exceed a reasonable width.
        for line in result.splitlines():
            assert len(line) < 200


# --- Persistence (save_boundaries / load_boundaries) ----------------------


class TestBoundaryPersistence:
    @pytest.fixture
    def conn(self, tmp_path):
        c = connect(tmp_path / "techne.db")
        # Need a sessions row for the FK
        c.execute(
            "INSERT INTO sessions (id, project_path) VALUES (?, ?)",
            ("sess-1", "/tmp"),
        )
        c.commit()
        return c

    def test_boundary_indices_from_segments(self):
        # 3 segments → 2 boundaries (last index of each non-final segment)
        segments = [[0, 1, 2], [3, 4, 5, 6], [7, 8, 9]]
        assert ht.boundary_indices_from_segments(segments) == {2, 6}

    def test_boundary_indices_empty(self):
        assert ht.boundary_indices_from_segments([]) == set()
        assert ht.boundary_indices_from_segments([[0, 1, 2]]) == set()

    def test_save_and_load_roundtrip(self, conn):
        scores = [
            ht.BoundaryScore(
                a_index=0, b_index=1, gap_seconds=120.0,
                family_a="filesystem", family_b="filesystem",
                signals={},
            ),
            ht.BoundaryScore(
                a_index=1, b_index=2, gap_seconds=4500.0,
                family_a="filesystem", family_b="bash",
                signals={"gap_hard": 1.0, "family_change": 0.4},
            ),
        ]
        ht.save_boundaries(conn, "sess-1", scores, boundary_a_indices={1})

        rows = ht.load_boundaries(conn, "sess-1")
        assert len(rows) == 2

        # First pair: no signals, not a boundary
        assert rows[0]["obs_index_a"] == 0
        assert rows[0]["signals"] == {}
        assert rows[0]["total_score"] == pytest.approx(0.0)
        assert rows[0]["is_boundary"] is False

        # Second pair: gap_hard + family_change fired, became a boundary
        assert rows[1]["obs_index_a"] == 1
        assert rows[1]["signals"]["gap_hard"] == 1.0
        assert rows[1]["signals"]["family_change"] == 0.4
        assert rows[1]["total_score"] == pytest.approx(1.4)
        assert rows[1]["is_boundary"] is True

    def test_save_empty_scores_is_no_op(self, conn):
        ht.save_boundaries(conn, "sess-1", [], boundary_a_indices=set())
        assert ht.load_boundaries(conn, "sess-1") == []

    def test_load_isolates_by_session(self, conn):
        # Add a second session
        conn.execute("INSERT INTO sessions (id, project_path) VALUES (?, ?)", ("sess-2", "/tmp"))
        conn.commit()
        scores_1 = [
            ht.BoundaryScore(
                a_index=0, b_index=1, gap_seconds=1.0,
                family_a="x", family_b="x", signals={},
            )
        ]
        scores_2 = [
            ht.BoundaryScore(
                a_index=0, b_index=1, gap_seconds=2.0,
                family_a="y", family_b="y", signals={},
            )
        ]
        ht.save_boundaries(conn, "sess-1", scores_1, set())
        ht.save_boundaries(conn, "sess-2", scores_2, set())

        rows_1 = ht.load_boundaries(conn, "sess-1")
        rows_2 = ht.load_boundaries(conn, "sess-2")
        assert len(rows_1) == 1 and rows_1[0]["family_a"] == "x"
        assert len(rows_2) == 1 and rows_2[0]["family_a"] == "y"
