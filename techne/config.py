"""Configuration management for techne."""

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

from techne.security import config_trust
from techne.security.config_trust import TrustVerdict

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
    #: Permit a sync adapter that is NOT one of techne's built-ins (techne#389).
    #: Defaults False, i.e. DEFAULT-DENY. See `sync.adapter.load_adapter` for why
    #: this is the boundary that exists rather than the ALLOW_REMOTE flag #389
    #: originally sketched. Governed by the operator layer: `sync` is a whole-block
    #: entry in EGRESS_KEYS, so this sub-key is covered with it.
    allow_unlisted_adapter: bool = False
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
    # Default OFF on VOLUME, not maturity (techne#456): Read is far more frequent
    # than Bash, so capturing it inflates the observation ledger without a
    # matching gain. Shipped and symmetric with capture_bash — turn it on if you
    # want the coverage and accept the row count.
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
    #: RELEVANCE FLOOR (techne#414) — make "inject nothing" a legal outcome.
    #: DEFAULT None = NO FLOOR, and that default is the deliberate part. #414 says
    #: the threshold "should come from the replay harness (#415), not from a
    #: guess", and #415 is unbuilt. So this ships the MECHANISM and leaves the
    #: VALUE unset: behaviour is unchanged until someone sets it from evidence.
    #:
    #: PER-KIND, because the two scales are opposite-signed and #402's review
    #: rejected merging them. Reject a hit when:
    #:   vec_distance > per_turn_recall_floor_vec_distance   (higher = farther)
    #:   bm25         > per_turn_recall_floor_bm25           (more-negative = better)
    #:
    #: MEASURED on 6,457 live delivered hits, 2026-08-18, to anchor whoever sets it:
    #: vec_distance ranges 0.546 (nearest) to 1.351 (farthest), mean 0.99. The worst
    #: decile is 1.168-1.351. A floor of 1.168 would therefore reject roughly the
    #: worst 10% of what is delivered today. That is an ANCHOR, not a recommendation.
    #:
    #: THE BM25 FLOOR IS A WEAKER CLAIM and _injection_log's docstring says why:
    #: for FTS rows `rank` is an INPUT to placement, not placement itself — the
    #: real ordering value (`_combined`) is popped before the row is returned. Raw
    #: BM25 is at least stable across queries, which the normalized blend is not.
    #: Set it knowing that. Live traffic is ~99% vec_distance, so it rarely applies.
    per_turn_recall_floor_vec_distance: float | None = None
    per_turn_recall_floor_bm25: float | None = None
    # Per-turn SEED nudge (techne#277): on each turn, remind the in-session
    # agent to techne_store a durable fact/decision/correction if one emerged —
    # the interactive complement to end-of-session reflect. Best-effort, rides
    # the already-warm context; the session-end batch reconciles/dedups. The
    # model self-gates (stores only when something durable emerged). Default OFF.
    per_turn_seed: bool = False
    # FIRST-PROMPT STARTUP DIRECTIVE (techne#643). Rides UserPromptSubmit rather
    # than SessionStart for a platform reason, not a preference: `prompt` and
    # `agent` hooks are barred from SessionStart ("subagents aren't available at
    # session initialization" — code.claude.com/docs/en/hooks, verified
    # 2026-08-18), and SessionStart also fires before MCP servers finish
    # connecting. So the earliest point where a model AND techne's MCP tools
    # both exist is the first prompt. Gated once per session via
    # extension_state; see `_startup_block`. Default OFF.
    startup_directive: bool = False


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
    #: How the egress-selecting keys above were resolved (techne#393). Set by
    #: :meth:`load`, which is the only production path that reads a file; a
    #: directly-constructed Config has no file to evaluate and leaves it None.
    #: Never serialized by :meth:`save` — it describes a resolution, not a
    #: setting, and writing it back down into the agent-writable file would let
    #: the agent forge the appearance of a lock.
    egress_trust: TrustVerdict | None = None
    #: config.yaml's OWN egress values as read from disk, captured before the
    #: gate stripped them. Replayed verbatim by :meth:`save` so an enforcing
    #: verdict never destroys the file it overrules. Empty when nothing is
    #: enforcing (the file is already authoritative) — never serialized itself.
    preserved_egress: dict[str, Any] = field(default_factory=dict)
    #: The config.yaml this instance was READ FROM, or None when there was no
    #: file and every value is a built-in default. Set only by :meth:`load`;
    #: a directly-constructed Config has no file and leaves it None. Never
    #: serialized by :meth:`save` — like :attr:`egress_trust` it describes a
    #: resolution, not a setting.
    #:
    #: The distinction it exists for: WITHOUT a file, an attribute like
    #: ``posture.mode`` is a dataclass default rather than a choice anyone
    #: made, so advice that pins it is preserving nothing. Ask this rather
    #: than re-statting the filesystem — ``load(path=...)`` can read a config
    #: that is not ``techne_home()/config.yaml``, and a second, independent
    #: filesystem question is free to disagree with the one that actually
    #: produced these values.
    loaded_from: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        home = techne_home()
        path = path or (home / "config.yaml")
        if not path.exists():
            # No config.yaml, but an operator layer may still exist — and it is
            # authoritative for a default install exactly as for a customized
            # one. Going through the same gate keeps that from being a special
            # case someone has to remember.
            raw: dict[str, Any] = {}
            trust, preserved = cls._gate_egress(raw, home)
            cfg = cls._from_dict(raw, home=home)
            cfg.egress_trust = trust
            cfg.preserved_egress = preserved
            return cfg
        # STAMP BEFORE THE READ, not after. The stamp is the cache's claim about
        # WHICH revision of config.yaml produced its values; taken after the
        # parse, an edit landing in between gets v1's values labelled v2 —
        # permanently fresh-looking and permanently wrong. Taken here, the same
        # edit makes the cache look stale, which costs one slow load and
        # self-corrects (#471 review, finding 1).
        try:
            from techne.fastconfig import stamp_source

            pre_stamp: str | None = stamp_source(path)
        except (OSError, ImportError):
            pre_stamp = None
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        trust, preserved = cls._gate_egress(raw, home)
        cfg = cls._from_dict(raw, home=home)
        cfg.egress_trust = trust
        cfg.preserved_egress = preserved
        cfg.loaded_from = path
        cfg._refresh_fast_cache(home=home, source=path, stamp=pre_stamp)
        return cfg

    @staticmethod
    def _gate_egress(raw: dict[str, Any], home: Path) -> tuple[TrustVerdict, dict[str, Any]]:
        """Resolve the operator layer and rewrite ``raw``'s egress keys in place.

        THE SINGLE CHOKEPOINT for techne#393. It sits in ``load()`` rather than
        at the five places that consume an egress key (``cli.py`` sync push,
        both session hooks, the MCP federation fan-out, the LLM call in
        thread_orchestrator) because a gate a caller can forget is not a gate.
        Everything in production reads its config through ``load()``; only tests
        construct via ``_from_dict``.

        The three states are deliberately not symmetric:

        * ``locked``   — operator values win, config.yaml's egress keys are
          dropped rather than merged.
        * ``unlocked`` — a layer exists but this process could rewrite it, so
          NEITHER file is trusted and the BUILT-IN DEFAULT destinations stand
          (not silence — `model` is a live default). Failing closed here is the
          whole point: the alternative silently restores the
          authority of the file the layer was deployed to overrule.
        * ``absent``   — unchanged behavior. config.yaml keeps full authority,
          and ``check_config_trust`` reports the install as having an
          agent-writable egress selector instead of implying a boundary that
          was never deployed.
        """
        verdict = config_trust.evaluate_lock(config_trust.resolve_operator_config(home))
        if not verdict.enforcing:
            return verdict, {}
        # Snapshot BEFORE the strip. The gate must ignore these values, not
        # destroy them — see capture_egress for the data-loss this prevents.
        preserved = config_trust.capture_egress(raw)
        target = verdict.read_target
        if verdict.state == config_trust.LOCKED and target is not None:
            config_trust.apply_operator_layer(raw, config_trust.read_operator_layer(target))
        else:
            config_trust.strip_egress(raw)
        return verdict, preserved

    def _refresh_fast_cache(self, home: Path, source: Path, stamp: str | None = None) -> None:
        """Write the stdlib-readable hook cache (techne#470). Best-effort.

        Populated HERE, from the fully-resolved config, so the cache cannot
        disagree with the parse that produced it — the only way to keep a
        derived file honest is to derive it at the same moment.

        Only the default config path seeds the cache: `Config.load(path=...)`
        with an explicit non-default file (tests, one-off tooling) must not
        overwrite the cache the hooks read.
        """
        if source != home / "config.yaml":
            return
        if stamp is None:
            # Could not stamp before the read, so the only stamp available now
            # would be taken AFTER the parse — the exact TOCTOU this signature
            # exists to prevent. Skip the write; the next load regenerates it.
            # Unreachable today (a failed stat here means the open() failed and
            # propagated), but "unreachable" describes today's control flow, not
            # the invariant (#471 review round 2, nit).
            return
        try:
            from techne.fastconfig import write_cache

            write_cache(
                {
                    "db_path": self.db_path,
                    "hooks.timeout_s": self.hooks.timeout_s,
                    "hooks.codex_grep_assist": int(self.hooks.codex_grep_assist),
                    "hooks.codex_grep_assist_limit": self.hooks.codex_grep_assist_limit,
                    "hooks.per_turn_recall": int(self.hooks.per_turn_recall),
                    "hooks.per_turn_seed": int(self.hooks.per_turn_seed),
                    "hooks.post_tool_use": int(self.hooks.post_tool_use),
                    "hooks.capture_bash": int(self.hooks.capture_bash),
                    "hooks.capture_read": int(self.hooks.capture_read),
                },
                home=home,
                stamp=stamp,
            )
        except Exception:  # noqa: BLE001 — a cache write must never break a load
            pass

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
                allow_unlisted_adapter=bool(s.get("allow_unlisted_adapter", False)),
                extra={
                    k: v
                    for k, v in s.items()
                    if k
                    not in (
                        "adapter",
                        "url",
                        "auth",
                        "audience",
                        "path",
                        "allow_unlisted_adapter",
                    )
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
                per_turn_recall_floor_vec_distance=(
                    float(h["per_turn_recall_floor_vec_distance"])
                    if h.get("per_turn_recall_floor_vec_distance") is not None
                    else None
                ),
                per_turn_recall_floor_bm25=(
                    float(h["per_turn_recall_floor_bm25"])
                    if h.get("per_turn_recall_floor_bm25") is not None
                    else None
                ),
                per_turn_seed=h.get("per_turn_seed", False),
                startup_directive=h.get("startup_directive", False),
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

    def to_yaml_dict(self) -> dict[str, Any]:
        """The YAML surface — what `save` writes, before the egress gate touches it.

        EXTRACTED SO THE TRUST GATE'S GUARD CAN CHECK THE RIGHT SURFACE (techne#586).
        `EGRESS_KEYS` entries address THIS mapping: `config_trust._pop`/`_get`/`_set`
        walk the raw dict loaded from config.yaml. The orphan guard used to resolve
        them against the DATACLASS surface instead, and the two coincide only by
        convention, because `load` bridges them with hardcoded string literals
        (`c.get("model", ...)`). A rename could therefore leave the guard green
        while the gate pinned nothing — measured in #582's review. Returning the
        dict makes the real surface testable without writing a file.

        MANY KEYS ARE CONDITIONAL — `llm_api_base`, `llm_api_key`, `identity_dir`,
        `stores`, `connections`, `sources` and the `sync.*` extras are emitted only
        when set — so a DEFAULT Config does not contain every governed key. A caller
        checking coverage has to populate them first, or it will read "absent
        because unset" as "absent because renamed".
        """
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
                "per_turn_recall_floor_vec_distance": (
                    self.hooks.per_turn_recall_floor_vec_distance
                ),
                "per_turn_recall_floor_bm25": self.hooks.per_turn_recall_floor_bm25,
                "per_turn_seed": self.hooks.per_turn_seed,
                "startup_directive": self.hooks.startup_directive,
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
            # `conflict` was READ by _from_dict and written by nothing (techne#620),
            # so a hand-written block was silently dropped by any load->save cycle —
            # and `techne init` IS such a cycle. No error, no diff to notice. Emitted
            # unconditionally here, mirroring `reflect`, its same-shaped sibling.
            #
            # Not a gate defect and worth not conflating: `strip_egress` operates on
            # the RAW dict from config.yaml, so `conflict.model` was governed and
            # stripped correctly the whole time.
            "conflict": {
                "model": self.conflict.model,
                "max_comparison_candidates": self.conflict.max_comparison_candidates,
                "max_items_per_emit": self.conflict.max_items_per_emit,
                "log_raw_response": self.conflict.log_raw_response,
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
        # Emitted only when True: the default is deny, and writing `false` into
        # every config.yaml would read as a knob someone chose rather than the
        # built-in posture (techne#389).
        if self.sync.allow_unlisted_adapter:
            data["sync"]["allow_unlisted_adapter"] = True
        # Preserve adapter-specific extras that came in via _from_dict's
        # "kwargs" sweep, so load/save is a real round-trip.
        if self.sync.extra:
            for k, v in self.sync.extra.items():
                if k not in data["sync"]:
                    data["sync"][k] = v
        return data

    def save(self, path: Path | None = None):
        path = path or (techne_home() / "config.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_yaml_dict()
        # Never write the egress set back down into the agent-writable file while
        # an operator layer is in force (techne#393). `techne init` is a
        # load→save round-trip, so without this the operator's pinned values
        # would be copied into config.yaml — where they read as authoritative,
        # and where they would keep applying if the operator layer were later
        # removed. A privilege that outlives the thing that granted it is not a
        # boundary. Applies to `unlocked` too: those values are the built-in
        # defaults, and persisting them would still be config.yaml claiming
        # authorship of a decision it did not make.
        if self.egress_trust is not None and self.egress_trust.enforcing:
            config_trust.strip_egress(data)
            config_trust.restore_egress(data, self.preserved_egress)
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
