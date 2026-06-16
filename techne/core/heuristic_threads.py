"""Backwards-compatibility shim.

``techne.core.heuristic_threads`` has been refactored into the
``techne.core.threads`` subpackage. All public names are still available
here so existing callers keep working without modification.

New code should import directly from the submodules:
    from techne.core.threads.features import ObsFeatures, extract_features
    from techne.core.threads.scoring import BoundaryScore, score_boundaries
    ...
or from the package root:
    from techne.core.threads import detect_threads, ObsFeatures
"""

from techne.core.threads import (  # noqa: F401
    BOUNDARY_THRESHOLD,
    CONTINUING_INVESTIGATION_GAP,
    DEFAULT_ENTITY_PATTERNS,
    DEFAULT_FAMILIES,
    ERROR_LINE_RE,
    FAMILY_CHANGE_SCORE,
    FILE_OP_TOOLS,
    FILE_PATH_RE,
    GAP_HARD,
    GAP_MEDIUM,
    GAP_SOFT,
    GENERIC_FAMILIES,
    META_FAMILY,
    MIN_SEGMENT_SIZE,
    REPO_CHANGE_SCORE,
    REPO_MARKERS,
    USER_PROMPT_FAMILY,
    USER_PROMPT_SCORE,
    ZERO_ENTITY_OVERLAP_SCORE,
    ZERO_FILE_OVERLAP_SCORE,
    BoundaryScore,
    ObsFeatures,
    _entity_overlap,
    _humanize_family,
    _humanize_relative,
    _inter_segment_gap,
    _last_error_snippet,
    _nearest_non_meta,
    _parse_timestamp,
    _recent_files,
    _score_signals,
    _segment_dominant_entity,
    _segment_dominant_family,
    _segment_dominant_repo,
    _short,
    boundary_indices_from_segments,
    build_resume_point,
    detect_threads,
    extract_entities,
    extract_features,
    extract_file_paths,
    fallback_summary,
    find_repo,
    generate_label,
    load_boundaries,
    merge_pass,
    resolve_tool_family,
    save_boundaries,
    score_boundaries,
    split_at_boundaries,
)
