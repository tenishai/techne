"""Configuration management for techne."""

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

Platform = Literal["claude-code", "codex", "standalone", "cabal", "opencode"]

# Single source of truth for the valid platform strings. The config validator
# and the CLI `--platform` choices both derive from this so they can't drift
# from the Literal (they did — the CLI list was missing entries). Add a new
# client here and it propagates everywhere.
PLATFORMS: tuple[str, ...] = get_args(Platform)

#: The platform whose hook mechanism is settings.json registration. Named rather
#: than spelled inline at each guard so a rename is caught by the type checker
#: (the annotation forces it to stay a member of ``Platform``) instead of by an
#: operator whose drift check silently stopped guarding. (#469 review 8.)
CLAUDE_CODE: Platform = "claude-code"


def techne_home() -> Path:
    """Root dir for techne state. TECHNE_HOME overrides the default ~/.techne so
    multiple agents on one OS user can each own a separate store — an agent's
    root is env/identity-defined, not equal to the user's home (techne#112)."""
    env = os.environ.get("TECHNE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".techne"


# Import-time snapshots, kept for back-compat with external importers. Code that
# must honor a TECHNE_HOME set after import — the MCP server, CLI, and hooks, all
# of which go through Config.load() — resolves via techne_home() at call time.
DEFAULT_DIR = techne_home()
DEFAULT_DB = DEFAULT_DIR / "techne.db"
DEFAULT_CONFIG = DEFAULT_DIR / "config.yaml"


@dataclass
class SyncConfig:
    adapter: str = "noop"
    url: str = ""
    auth: str = ""
    audience: str = ""
    path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoreSpec:
    """One domain store (db-per-domain, techne#88/#98). `adapter` selects the
    backend (sqlite day one; postgres/etc. later via techne#87). Empty `dsn`
    defaults to <data_dir>/<domain>.db."""

    adapter: str = "sqlite"
    dsn: str = ""


_FEDERATION_ROLES = frozenset({"team", "static"})


@dataclass
class FederationConnectionSpec:
    """One remote techne store to federate recall over (techne#222).

    role: 'team' — shared team store; 'static' — read-only reference store.
    db_path: path to the remote techne.db (opened read-only, never written). A
        relative path is resolved against techne_home() at open time (techne#332),
        so a committed config.yaml stays portable; an absolute path is used as-is.
    label: human-readable label shown in surfaced federation conflicts.
    """

    role: str  # "team" | "static"
    db_path: str  # path to remote techne.db, opened read-only
    label: str = ""


Detector = Literal["heuristic", "llm"]
Summarizer = Literal["heuristic", "llm"]


@dataclass
class ThreadConfig:
    min_observations: int = 15
    # 0.05 corresponds to |bm25| ≤ 19, which typical FTS5 matches produce.
    # The old default of 0.85 (|bm25| ≤ 0.18) was unreachable in production.
    link_threshold: float = 0.05
    stale_after_days: int = 30
    complete_after_hours: int = 48
    # Heuristic detector configuration. Default to heuristic + heuristic so
    # techne works out of the box without an LLM API key.
    detector: Detector = "heuristic"
    summarizer: Summarizer = "heuristic"
    # User extensions to the heuristic detector. tool_families overrides /
    # extends DEFAULT_FAMILIES; entity_patterns is a list of regex strings
    # added to DEFAULT_ENTITY_PATTERNS.
    tool_families: dict[str, str] = field(default_factory=dict)
    entity_patterns: list[str] = field(default_factory=list)


@dataclass
class HookConfig:
    post_tool_use: bool = True
    capture_bash: bool = False
    capture_read: bool = False
    # Hard wall-clock budget for any single hook process.  If the hook exceeds
    # this many seconds (SQLite contention, slow LLM call, stalled network) a
    # SIGALRM fires and the hook exits 0 silently.  Overrides the
    # TECHNE_HOOK_TIMEOUT env-var default (5.0 s) when set explicitly.
    timeout_s: float = 5.0
    # Experimental (techne#206): opt-in graph-assist on the agent's own Grep/Glob.
    # When ON, a PreToolUse hook runs a Codex symbol search for the search pattern
    # and injects matching symbols into the model's context via additionalContext.
    # Strictly best-effort — never blocks the tool. Default OFF.
    codex_grep_assist: bool = False
    # Hard cap on the number of symbols injected per Grep/Glob (config-over-hardcode).
    codex_grep_assist_limit: int = 8
    # Mid-session codex freshness (techne#301): when ON, a PostToolUse on
    # Write/Edit/MultiEdit incrementally reindexes just the edited file into its repo's
    # codex db, so codex queries don't go stale between SessionStart reindexes.
    # Best-effort, only touches already-indexed repos + supported langs. Off by
    # default (adds a small per-edit reindex).
    codex_reindex_on_edit: bool = False
    # Experimental (techne#335): opt-in codex "edit rail". When ON, a PostToolUse on
    # Edit/MultiEdit maps the edit to its enclosing symbol and injects that symbol's
    # blast-radius (transitive dependents — "what breaks if I change this") into
    # context, but only when the symbol has >= codex_edit_rail_min_deps dependents
    # (threshold-gated so leaf edits stay silent). Best-effort; never blocks the
    # tool; logs to context_injections (source=codex_edit_rail) so its utilization
    # is measurable against the other injection paths (#329/#684). Default OFF.
    codex_edit_rail: bool = False
    # Minimum transitive-dependent count for the edit rail to fire (skip low-risk edits).
    codex_edit_rail_min_deps: int = 3
    # Hard cap on dependents injected per edit (closest-first by depth).
    codex_edit_rail_limit: int = 10
    # Experimental (techne#109/#277): opt-in per-turn recall injection. When ON, a
    # UserPromptSubmit hook runs a knowledge search scoped to the submitted prompt
    # and injects the most relevant durable facts into context via additionalContext
    # — the DYNAMIC per-turn complement to the STABLE SessionStart block. Strictly
    # best-effort — never blocks the turn. Default OFF.
    per_turn_recall: bool = False
    # Hard cap on the number of knowledge rows injected per turn.
    per_turn_recall_limit: int = 5
    # techne#329 — log each per-turn recall attempt (trigger, hit ids, scores,
    # token cost) to the context_injections table so this READ path is auditable
    # (it left no trace before). The SessionStart tier-0 block is logged the same
    # way (source="session_start"). Observability of the feature, not a new
    # feature, so it's ON by default whenever per_turn_recall is on.
    per_turn_recall_log: bool = True
    # Per-turn SEED nudge (techne#277): on each turn, remind the in-session
    # agent to techne_store a durable fact/decision/correction if one emerged —
    # the interactive complement to end-of-session reflect. Best-effort, rides
    # the already-warm context; the session-end batch reconciles/dedups. The
    # model self-gates (stores only when something durable emerged). Default OFF.
    per_turn_seed: bool = False


ReflectWriteTo = Literal["candidates", "live"]


@dataclass
class TranscriptSourceConfig:
    """Config for one JsonlTranscriptSource instance."""

    paths: list[str] = field(default_factory=list)
    include_globs: list[str] = field(default_factory=lambda: ["*.jsonl"])
    exclude_globs: list[str] = field(default_factory=list)
    debounce_ms: int = 500
    adapter_name: str = "jsonl_transcript"


@dataclass
class SourcesConfig:
    """Config for all source adapters."""

    claude_code_transcripts: TranscriptSourceConfig | None = None


@dataclass
class HeuristicConfig:
    # Score threshold for calling a pair-boundary a real thread boundary.
    # Default matches BOUNDARY_THRESHOLD in heuristic_threads.py.
    boundary_threshold: float = 0.7
    # Additive score applied when the next observation is a UserPrompt.
    # Default matches USER_PROMPT_SCORE in heuristic_threads.py.
    user_prompt_score: float = 0.3


@dataclass
class SearchConfig:
    recency_weight: float = 0.3
    bm25_weight: float = 0.7
    expansion_ratio: int = 2
    mmr_lambda: float = 0.3
    # Embedding backend override (techne#601/#602). "" = auto-select (model2vec
    # on py3.14+/fastembed-absent, else fastembed) — see core/embeddings.py.
    # Explicit values: "model2vec" or "fastembed".
    embedding_backend: str = ""
    # Embedding model name override. "" = backend-specific default (see
    # embeddings.py: potion-base-8M for model2vec, MiniLM for fastembed).
    embedding_model: str = ""
    # Max librarian documents folded into a recall result (techne#340). Recall
    # returns memory + this many top library docs, each labelled origin=library.
    # 0 disables the doc fold-in.
    recall_documents_limit: int = 3


@dataclass
class InjectionConfig:
    """Config for Tier-0 knowledge injection at SessionStart (techne#93).

    enabled: when False the ## Key Knowledge block is omitted entirely.
    char_budget: hard ceiling on characters contributed by the knowledge
        block (includes the prefix/formatting overhead per line).
        Default 2000 keeps the injection well within a typical context
        preamble without crowding thread context or the health banner.
    max_active_threads: cap on active threads injected at session start.
    max_recent_threads: cap on recent-48h threads injected at session start.
    thread_char_budget: overall char ceiling for the threads block
        (active + recent combined). Mirrors the knowledge char_budget pattern.
    thread_per_item_chars: per-thread content truncation limit.
    recent_reserve_frac: fraction of char_budget reserved for the freshest
        decisions/evaluations/facts ("what changed since last session"), filled
        before the static feedback band so it can never be starved (techne#93
        egress-starvation fix). 0.0 disables the reservation (legacy ordering).
    correction_reserve_frac: fraction of char_budget reserved for belief-
        overturning facts (tagged memory-type:correction), filled right after
        identity so a correction is never evicted by merely-newer routine facts
        (the confident-wrong fix). 0.0 disables the reservation.
    """

    enabled: bool = True
    char_budget: int = 2000
    max_active_threads: int = 3
    max_recent_threads: int = 5
    thread_char_budget: int = 1500
    thread_per_item_chars: int = 200
    recent_reserve_frac: float = 0.4
    correction_reserve_frac: float = 0.3


@dataclass
class ReflectConfig:
    # End-of-session reflection: an LLM scans each detected thread and proposes
    # durable knowledge / identity entries worth keeping. Off by default; opt
    # in per host. While `write_to="candidates"` the live knowledge / identity
    # tables are never touched — proposals land in `knowledge_candidates` for
    # human review via `techne reflect review`.
    enabled: bool = False
    min_observations_per_thread: int = 8
    min_confidence: str = "medium"
    max_candidates_per_thread: int = 5
    model: str = ""  # falls back to Config.model when empty
    include_existing_knowledge: bool = True
    existing_knowledge_k: int = 10
    write_to: ReflectWriteTo = "candidates"
    log_raw_response: bool = True
    # Max threads a single techne_reflect_emit returns when the caller passes no
    # explicit limit. Bounds the payload so a large session can't produce a batch
    # that exceeds the MCP transport's token cap (see _techne_reflect_emit).
    emit_max_threads: int = 25
    # Serialized-size ceiling (chars) for a reflect emit batch — a conservative
    # proxy for the transport's token cap. The batch is trimmed to fit; a lone
    # thread exceeding this can't be delivered whole (see _techne_reflect_emit).
    emit_max_chars: int = 100_000


@dataclass
class ConflictConfig:
    """Configuration for contradiction detection → supersession (techne#213, D1).

    model: LLM model for the conflict prompt. Falls back to Config.model when empty.
    max_comparison_candidates: how many entity-overlapping rows to compare per entry.
    max_items_per_emit: cap on groups returned by conflict_check_emit per run.
    log_raw_response: whether to persist the agent's raw response in the candidate row.
    """

    model: str = ""  # falls back to Config.model
    max_comparison_candidates: int = 20
    max_items_per_emit: int = 20
    log_raw_response: bool = True


@dataclass
class ArchiveConfig:
    """Configuration for the soft-archive scan lifecycle (techne#216, D2).

    confidence_floor: entries whose effective_confidence() drops below this threshold
        qualify for archiving (along with status='uncertain' AND no recent reaffirmation).
        Default 0.1 is deliberately conservative — only deeply-decayed entries are
        archived, avoiding false positives. Bias conservative: aggressive archiving
        costs recall precision.
    reaffirmation_window_days: if last_confirmed_at is OLDER than this many days,
        the entry counts as "not recently reaffirmed" and qualifies (all other criteria
        also met). Default 180 days (6 months) is conservative.
    """

    confidence_floor: float = 0.1
    reaffirmation_window_days: int = 180


@dataclass
class PostureConfig:
    """Coexistence posture with Claude Code's curated memory (techne#107 PR4).

    mode='spool-primary': CC authors curated memory; techne silences its own
        UNATTENDED authoring — reflect's auto-promote (write_to='live') is forced to
        candidates, so no background pass can drift from CC's authoritative store.
        Scope is deliberately narrow: this gates the one unattended live writer.
        techne_store and human promote_candidate are INTENTIONAL acts and remain
        ungated (the operator chose to write them).
    mode='observe' (default): techne operates independently; reflect.write_to
        is honored as-is.
    cc_memory_detected: recorded at init time — True iff CC memory files were
        found in the project directory during `techne init`.
    """

    mode: Literal["observe", "spool-primary"] = "observe"
    cc_memory_detected: bool = False


@dataclass
class Config:
    # RESOLVED AT CONSTRUCTION, not at import. These used to bind the
    # import-time DEFAULT_DB/DEFAULT_DIR snapshot, with a comment saying that was
    # fine because "Config.load() and _from_dict() both overwrite db_path/data_dir
    # from techne_home() at call time; only direct Config() construction sees the
    # snapshot". Direct construction turned out to be common — and in tests it
    # meant a bare Config() pointed data_dir at the OPERATOR'S REAL store, so
    # anything deriving a domain db from it (``<data_dir>/librarian.db``) was
    # created in the live directory. Note the attr-pin trick used elsewhere in
    # the suite cannot fix this from outside: a dataclass default binds at CLASS
    # CREATION, so patching DEFAULT_DIR afterwards does nothing. A default_factory
    # is the only thing that makes TECHNE_HOME apply at the point of use.
    # (techne#477, found by Prudence on PR #478.)
    db_path: Path = field(default_factory=lambda: techne_home() / "techne.db")
    data_dir: Path = field(default_factory=techne_home)
    identity_dir: Path | None = None
    sync: SyncConfig = field(default_factory=SyncConfig)
    # Domain stores (db-per-domain, techne#88/#98): domain name → StoreSpec.
    # techne.db (db_path) is the implicit personal-memory store; these are the
    # separate domain dbs (librarian, architect, codex). Empty = no domains.
    stores: dict[str, StoreSpec] = field(default_factory=dict)
    # Federation connections (techne#222): connection name → FederationConnectionSpec.
    # Empty dict = local-only (unchanged behavior). opt-in: no migration needed.
    connections: dict[str, FederationConnectionSpec] = field(default_factory=dict)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    threads: ThreadConfig = field(default_factory=ThreadConfig)
    hooks: HookConfig = field(default_factory=HookConfig)
    heuristic: HeuristicConfig = field(default_factory=HeuristicConfig)
    reflect: ReflectConfig = field(default_factory=ReflectConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    posture: PostureConfig = field(default_factory=PostureConfig)
    conflict: ConflictConfig = field(default_factory=ConflictConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    platform: Platform = "claude-code"
    # Federation topology (techne#224): am I a lone agent that pushes memory
    # directly, or part of a team that routes through an Archivist? standalone=true
    # → the sync adapter pushes (the sync: outbox lane); standalone=false → route to
    # the Archivist sink (sink:archivist owns the push). techne behaviour is
    # otherwise identical. Default true = behaviour-preserving + the lone-agent case.
    standalone: bool = True
    # Base Archivist store (techne#346): the techne-schema SQLite db a non-standalone
    # instance's sink:archivist writes team memory into. "" → <data_dir>/archivist.db.
    archivist_db_path: str = ""
    # Staleness alarm: if the prior last_session is older than this many hours,
    # the banner and collect_health emit a visible warning. Default 12h.
    staleness_hours: int = 12
    # Minimum cosine similarity for infer() to attach a tag (techne#352). The
    # ceiling is backend-dependent: model2vec (potion-base-8M) tops out ~0.59, so
    # the old hardcoded 0.6 tagged 0% of rows. 0.42 gives ~65-70% coverage on
    # model2vec; raise it for a higher-cosine backend (e.g. fastembed/bge).
    tag_threshold: float = 0.42
    model: str = "claude-haiku-4-5-20251001"
    # Optional LiteLLM proxy targeting. When unset, litellm.completion() uses
    # its standard env-var-based provider routing (ANTHROPIC_API_KEY, etc.).
    # Set llm_api_base when you're running a LiteLLM proxy server (e.g., for
    # centralized budget enforcement, observability, or model routing) and
    # want techne's summarization calls to go through it.
    llm_api_base: str = ""
    llm_api_key: str = ""
    # Decay half-life for effective_confidence() (techne#214).  Entries
    # confirmed this many days ago are at half their stored confidence_numeric.
    # Generous default (≥30d per proposal D2) because decay is operational-only
    # — ZenBrain NoDecay ΔP@5=0.002; aggressive decay costs 4.4pp precision.
    # Set to 0 to disable decay entirely (returns raw confidence_numeric).
    knowledge_confidence_half_life_days: float = 30.0

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        home = techne_home()
        path = path or (home / "config.yaml")
        if not path.exists():
            cfg = cls()
            cfg.db_path = home / "techne.db"
            cfg.data_dir = home
            return cfg
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        return cls._from_dict(raw, home=home)

    @classmethod
    def _from_dict(cls, raw: dict, home: Path | None = None) -> "Config":
        home = home or techne_home()
        cfg = cls()
        # Absent db_path/data_dir default to the (possibly TECHNE_HOME-relocated)
        # root rather than the import-time snapshot, so the env knob takes effect.
        cfg.db_path = Path(raw["db_path"]).expanduser() if "db_path" in raw else home / "techne.db"
        cfg.data_dir = Path(raw["data_dir"]).expanduser() if "data_dir" in raw else home
        if "identity_dir" in raw:
            cfg.identity_dir = Path(raw["identity_dir"]).expanduser()
        if "platform" in raw:
            if raw["platform"] not in PLATFORMS:
                raise ValueError(
                    f"Invalid platform '{raw['platform']}'. Must be one of: {', '.join(PLATFORMS)}"
                )
            cfg.platform = raw["platform"]
        if "standalone" in raw:
            cfg.standalone = bool(raw["standalone"])
        if "archivist_db_path" in raw:
            cfg.archivist_db_path = str(raw["archivist_db_path"])
        if "model" in raw:
            cfg.model = raw["model"]
        if "llm_api_base" in raw:
            cfg.llm_api_base = str(raw["llm_api_base"])
        if "llm_api_key" in raw:
            cfg.llm_api_key = str(raw["llm_api_key"])

        if "stores" in raw and isinstance(raw["stores"], dict):
            for domain, spec in raw["stores"].items():
                if isinstance(spec, dict):
                    cfg.stores[str(domain)] = StoreSpec(
                        adapter=str(spec.get("adapter", "sqlite")),
                        dsn=str(spec.get("dsn", "")),
                    )

        if "connections" in raw and isinstance(raw["connections"], dict):
            for conn_name, spec in raw["connections"].items():
                if isinstance(spec, dict):
                    role = str(spec.get("role", "team"))
                    if role not in _FEDERATION_ROLES:
                        raise ValueError(
                            f"Invalid connections['{conn_name}'].role '{role}'. "
                            f"Must be one of: {', '.join(sorted(_FEDERATION_ROLES))}"
                        )
                    cfg.connections[str(conn_name)] = FederationConnectionSpec(
                        role=role,
                        db_path=str(spec.get("db_path", "")),
                        label=str(spec.get("label", "")),
                    )

        if "sources" in raw and isinstance(raw["sources"], dict):
            s = raw["sources"]
            sc = SourcesConfig()
            if "claude_code_transcripts" in s and isinstance(s["claude_code_transcripts"], dict):
                t = s["claude_code_transcripts"]
                sc.claude_code_transcripts = TranscriptSourceConfig(
                    paths=list(t.get("paths", [])),
                    include_globs=list(t.get("include_globs", ["*.jsonl"])),
                    exclude_globs=list(t.get("exclude_globs", [])),
                    debounce_ms=int(t.get("debounce_ms", 500)),
                    adapter_name=str(t.get("adapter_name", "jsonl_transcript")),
                )
            cfg.sources = sc

        if "sync" in raw and isinstance(raw["sync"], dict):
            s = raw["sync"]
            cfg.sync = SyncConfig(
                adapter=s.get("adapter", "noop"),
                url=s.get("url", ""),
                auth=s.get("auth", ""),
                audience=s.get("audience", ""),
                path=s.get("path", ""),
                extra={
                    k: v
                    for k, v in s.items()
                    if k not in ("adapter", "url", "auth", "audience", "path")
                },
            )

        if "threads" in raw and isinstance(raw["threads"], dict):
            t = raw["threads"]
            detector = t.get("detector", "heuristic")
            if detector not in ("heuristic", "llm"):
                raise ValueError(
                    f"Invalid threads.detector '{detector}'. Must be 'heuristic' or 'llm'."
                )
            summarizer = t.get("summarizer", "heuristic")
            if summarizer not in ("heuristic", "llm"):
                raise ValueError(
                    f"Invalid threads.summarizer '{summarizer}'. Must be 'heuristic' or 'llm'."
                )
            tool_families = t.get("tool_families") or {}
            if not isinstance(tool_families, dict):
                raise ValueError("threads.tool_families must be a mapping")
            entity_patterns = t.get("entity_patterns") or []
            if not isinstance(entity_patterns, list):
                raise ValueError("threads.entity_patterns must be a list")
            # Fallbacks read from the dataclass defaults so a YAML block that
            # omits a field can never drift from the in-code default (#82 — the
            # old literal 0.85 fallback silently overrode the 0.05 default).
            _td = ThreadConfig()
            cfg.threads = ThreadConfig(
                min_observations=t.get("min_observations", _td.min_observations),
                link_threshold=t.get("link_threshold", _td.link_threshold),
                stale_after_days=t.get("stale_after_days", _td.stale_after_days),
                complete_after_hours=t.get("complete_after_hours", _td.complete_after_hours),
                detector=detector,
                summarizer=summarizer,
                tool_families={str(k): str(v) for k, v in tool_families.items()},
                entity_patterns=[str(p) for p in entity_patterns],
            )

        if "hooks" in raw and isinstance(raw["hooks"], dict):
            h = raw["hooks"]
            _hd = HookConfig()
            cfg.hooks = HookConfig(
                post_tool_use=h.get("post_tool_use", True),
                capture_bash=h.get("capture_bash", False),
                capture_read=h.get("capture_read", False),
                timeout_s=float(h.get("timeout_s", _hd.timeout_s)),
                codex_grep_assist=h.get("codex_grep_assist", False),
                codex_grep_assist_limit=int(h.get("codex_grep_assist_limit", 8)),
                codex_reindex_on_edit=h.get("codex_reindex_on_edit", False),
                codex_edit_rail=h.get("codex_edit_rail", False),
                codex_edit_rail_min_deps=int(h.get("codex_edit_rail_min_deps", 3)),
                codex_edit_rail_limit=int(h.get("codex_edit_rail_limit", 10)),
                per_turn_recall=h.get("per_turn_recall", False),
                per_turn_recall_limit=int(h.get("per_turn_recall_limit", 5)),
                per_turn_recall_log=h.get("per_turn_recall_log", True),
                per_turn_seed=h.get("per_turn_seed", False),
            )

        if "heuristic" in raw and isinstance(raw["heuristic"], dict):
            h = raw["heuristic"]
            cfg.heuristic = HeuristicConfig(
                boundary_threshold=float(h.get("boundary_threshold", 0.7)),
                user_prompt_score=float(h.get("user_prompt_score", 0.3)),
            )

        if "reflect" in raw and isinstance(raw["reflect"], dict):
            r = raw["reflect"]
            write_to = r.get("write_to", "candidates")
            if write_to not in ("candidates", "live"):
                raise ValueError(
                    f"Invalid reflect.write_to '{write_to}'. Must be 'candidates' or 'live'."
                )
            min_conf = r.get("min_confidence", "medium")
            if min_conf not in ("low", "medium", "high"):
                raise ValueError(
                    f"Invalid reflect.min_confidence '{min_conf}'. "
                    "Must be 'low', 'medium', or 'high'."
                )
            cfg.reflect = ReflectConfig(
                enabled=bool(r.get("enabled", False)),
                min_observations_per_thread=int(r.get("min_observations_per_thread", 8)),
                min_confidence=min_conf,
                max_candidates_per_thread=int(r.get("max_candidates_per_thread", 5)),
                model=str(r.get("model", "")),
                include_existing_knowledge=bool(r.get("include_existing_knowledge", True)),
                existing_knowledge_k=int(r.get("existing_knowledge_k", 10)),
                write_to=write_to,
                log_raw_response=bool(r.get("log_raw_response", True)),
                emit_max_threads=int(r.get("emit_max_threads", 25)),
                emit_max_chars=int(r.get("emit_max_chars", 100_000)),
            )

        if "search" in raw and isinstance(raw["search"], dict):
            s = raw["search"]
            cfg.search = SearchConfig(
                recency_weight=float(s.get("recency_weight", 0.3)),
                bm25_weight=float(s.get("bm25_weight", 0.7)),
                expansion_ratio=int(s.get("expansion_ratio", 2)),
                mmr_lambda=float(s.get("mmr_lambda", 0.3)),
                embedding_backend=str(s.get("embedding_backend", "")),
                embedding_model=str(s.get("embedding_model", "")),
                recall_documents_limit=int(s.get("recall_documents_limit", 3)),
            )

        if "injection" in raw and isinstance(raw["injection"], dict):
            i = raw["injection"]
            cfg.injection = InjectionConfig(
                enabled=bool(i.get("enabled", True)),
                char_budget=int(i.get("char_budget", 2000)),
                max_active_threads=int(i.get("max_active_threads", 3)),
                max_recent_threads=int(i.get("max_recent_threads", 5)),
                thread_char_budget=int(i.get("thread_char_budget", 1500)),
                thread_per_item_chars=int(i.get("thread_per_item_chars", 200)),
                recent_reserve_frac=float(i.get("recent_reserve_frac", 0.4)),
                correction_reserve_frac=float(i.get("correction_reserve_frac", 0.3)),
            )

        if "staleness_hours" in raw:
            cfg.staleness_hours = int(raw["staleness_hours"])

        if "tag_threshold" in raw:
            cfg.tag_threshold = float(raw["tag_threshold"])

        if "knowledge_confidence_half_life_days" in raw:
            cfg.knowledge_confidence_half_life_days = float(
                raw["knowledge_confidence_half_life_days"]
            )

        if "posture" in raw and isinstance(raw["posture"], dict):
            p = raw["posture"]
            mode = p.get("mode", "observe")
            # Raise on an invalid mode rather than silently clamping to 'observe'
            # — a typo'd posture would otherwise un-gate the unattended writer
            # without telling anyone. Consistent with every other enum validator
            # here (platform, detector, write_to). (techne#167)
            if mode not in ("observe", "spool-primary"):
                raise ValueError(
                    f"Invalid posture.mode '{mode}'. Must be one of: observe, spool-primary"
                )
            cfg.posture = PostureConfig(
                mode=mode,
                cc_memory_detected=bool(p.get("cc_memory_detected", False)),
            )

        if "conflict" in raw and isinstance(raw["conflict"], dict):
            c = raw["conflict"]
            _cd = ConflictConfig()
            cfg.conflict = ConflictConfig(
                model=str(c.get("model", _cd.model)),
                max_comparison_candidates=int(
                    c.get("max_comparison_candidates", _cd.max_comparison_candidates)
                ),
                max_items_per_emit=int(c.get("max_items_per_emit", _cd.max_items_per_emit)),
                log_raw_response=bool(c.get("log_raw_response", _cd.log_raw_response)),
            )

        if "archive" in raw and isinstance(raw["archive"], dict):
            a = raw["archive"]
            _ad = ArchiveConfig()
            cfg.archive = ArchiveConfig(
                confidence_floor=float(a.get("confidence_floor", _ad.confidence_floor)),
                reaffirmation_window_days=int(
                    a.get("reaffirmation_window_days", _ad.reaffirmation_window_days)
                ),
            )

        return cfg

    def save(self, path: Path | None = None):
        path = path or (techne_home() / "config.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "db_path": str(self.db_path),
            "data_dir": str(self.data_dir),
            "platform": self.platform,
            "standalone": self.standalone,
            "archivist_db_path": self.archivist_db_path,
            "model": self.model,
            "sync": {
                "adapter": self.sync.adapter,
            },
            "threads": {
                "min_observations": self.threads.min_observations,
                "link_threshold": self.threads.link_threshold,
                "stale_after_days": self.threads.stale_after_days,
                "complete_after_hours": self.threads.complete_after_hours,
                "detector": self.threads.detector,
                "summarizer": self.threads.summarizer,
            },
            "hooks": {
                "post_tool_use": self.hooks.post_tool_use,
                "capture_bash": self.hooks.capture_bash,
                "capture_read": self.hooks.capture_read,
                "timeout_s": self.hooks.timeout_s,
                "codex_grep_assist": self.hooks.codex_grep_assist,
                "codex_grep_assist_limit": self.hooks.codex_grep_assist_limit,
                "codex_reindex_on_edit": self.hooks.codex_reindex_on_edit,
                "codex_edit_rail": self.hooks.codex_edit_rail,
                "codex_edit_rail_min_deps": self.hooks.codex_edit_rail_min_deps,
                "codex_edit_rail_limit": self.hooks.codex_edit_rail_limit,
                "per_turn_recall": self.hooks.per_turn_recall,
                "per_turn_recall_limit": self.hooks.per_turn_recall_limit,
                "per_turn_recall_log": self.hooks.per_turn_recall_log,
                "per_turn_seed": self.hooks.per_turn_seed,
            },
            "heuristic": {
                "boundary_threshold": self.heuristic.boundary_threshold,
                "user_prompt_score": self.heuristic.user_prompt_score,
            },
            "reflect": {
                "enabled": self.reflect.enabled,
                "min_observations_per_thread": self.reflect.min_observations_per_thread,
                "min_confidence": self.reflect.min_confidence,
                "max_candidates_per_thread": self.reflect.max_candidates_per_thread,
                "model": self.reflect.model,
                "include_existing_knowledge": self.reflect.include_existing_knowledge,
                "existing_knowledge_k": self.reflect.existing_knowledge_k,
                "write_to": self.reflect.write_to,
                "log_raw_response": self.reflect.log_raw_response,
                "emit_max_threads": self.reflect.emit_max_threads,
                "emit_max_chars": self.reflect.emit_max_chars,
            },
            "search": {
                "recency_weight": self.search.recency_weight,
                "bm25_weight": self.search.bm25_weight,
                "expansion_ratio": self.search.expansion_ratio,
                "mmr_lambda": self.search.mmr_lambda,
                # embedding_backend/embedding_model are parsed by _from_dict but
                # must also be serialized here, else `techne init`'s load→save
                # round-trip silently drops an operator's backend override — the
                # same staleness bug the injection note below records (techne#601).
                "embedding_backend": self.search.embedding_backend,
                "embedding_model": self.search.embedding_model,
                "recall_documents_limit": self.search.recall_documents_limit,
            },
            "posture": {
                "mode": self.posture.mode,
                "cc_memory_detected": self.posture.cc_memory_detected,
            },
            # injection + staleness_hours are parsed by _from_dict but were
            # previously NOT serialized here, so re-running `techne init` (the only
            # save() caller) silently reset any customization. (techne#167)
            "injection": {
                "enabled": self.injection.enabled,
                "char_budget": self.injection.char_budget,
                "max_active_threads": self.injection.max_active_threads,
                "max_recent_threads": self.injection.max_recent_threads,
                "thread_char_budget": self.injection.thread_char_budget,
                "thread_per_item_chars": self.injection.thread_per_item_chars,
                "recent_reserve_frac": self.injection.recent_reserve_frac,
                "correction_reserve_frac": self.injection.correction_reserve_frac,
            },
            "staleness_hours": self.staleness_hours,
            "tag_threshold": self.tag_threshold,
            "knowledge_confidence_half_life_days": self.knowledge_confidence_half_life_days,
            "archive": {
                "confidence_floor": self.archive.confidence_floor,
                "reaffirmation_window_days": self.archive.reaffirmation_window_days,
            },
        }
        # sources: only emit the sub-keys that are configured to avoid
        # polluting a minimal config with empty stubs.
        sources_data: dict[str, Any] = {}
        if self.sources.claude_code_transcripts is not None:
            t = self.sources.claude_code_transcripts
            sources_data["claude_code_transcripts"] = {
                "paths": list(t.paths),
                "include_globs": list(t.include_globs),
                "exclude_globs": list(t.exclude_globs),
                "debounce_ms": t.debounce_ms,
                "adapter_name": t.adapter_name,
            }
        if sources_data:
            data["sources"] = sources_data
        if self.identity_dir:
            data["identity_dir"] = str(self.identity_dir)
        if self.llm_api_base:
            data["llm_api_base"] = self.llm_api_base
        if self.llm_api_key:
            data["llm_api_key"] = self.llm_api_key
        if self.threads.tool_families:
            data["threads"]["tool_families"] = dict(self.threads.tool_families)
        if self.threads.entity_patterns:
            data["threads"]["entity_patterns"] = list(self.threads.entity_patterns)
        if self.stores:
            data["stores"] = {
                domain: {"adapter": spec.adapter, "dsn": spec.dsn}
                for domain, spec in self.stores.items()
            }
        # connections: only emit when non-empty, mirrors the stores block (techne#222).
        if self.connections:
            data["connections"] = {
                conn_name: {
                    "role": spec.role,
                    "db_path": spec.db_path,
                    **({"label": spec.label} if spec.label else {}),
                }
                for conn_name, spec in self.connections.items()
            }
        if self.sync.url:
            data["sync"]["url"] = self.sync.url
        if self.sync.auth:
            data["sync"]["auth"] = self.sync.auth
        if self.sync.audience:
            data["sync"]["audience"] = self.sync.audience
        if self.sync.path:
            data["sync"]["path"] = self.sync.path
        # Preserve adapter-specific extras that came in via _from_dict's
        # "kwargs" sweep, so load/save is a real round-trip.
        if self.sync.extra:
            for k, v in self.sync.extra.items():
                if k not in data["sync"]:
                    data["sync"][k] = v
        # Write atomically with owner-only perms FROM CREATION. The config can hold
        # secrets (llm_api_key, sync.auth); the old open()+chmod() created the file
        # world-readable (≈0644) and only narrowed it afterward — a TOCTOU window in
        # which another local user could read the key. tempfile.mkstemp creates the
        # temp 0600, and os.replace preserves that mode on the final file, so the key
        # is never world-readable for any instant. Always 0600 (not just when a key
        # is present) so sync.auth and future secrets are covered too. (techne#167)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
