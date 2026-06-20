"""Configuration management for techne."""

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

DEFAULT_DIR = Path.home() / ".techne"
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
    """

    enabled: bool = True
    char_budget: int = 2000
    max_active_threads: int = 3
    max_recent_threads: int = 5
    thread_char_budget: int = 1500
    thread_per_item_chars: int = 200
    recent_reserve_frac: float = 0.4


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


@dataclass
class Config:
    db_path: Path = DEFAULT_DB
    data_dir: Path = DEFAULT_DIR
    identity_dir: Path | None = None
    sync: SyncConfig = field(default_factory=SyncConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    threads: ThreadConfig = field(default_factory=ThreadConfig)
    hooks: HookConfig = field(default_factory=HookConfig)
    heuristic: HeuristicConfig = field(default_factory=HeuristicConfig)
    reflect: ReflectConfig = field(default_factory=ReflectConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    platform: Platform = "claude-code"
    # Staleness alarm: if the prior last_session is older than this many hours,
    # the banner and collect_health emit a visible warning. Default 12h.
    staleness_hours: int = 12
    model: str = "claude-haiku-4-5-20251001"
    # Optional LiteLLM proxy targeting. When unset, litellm.completion() uses
    # its standard env-var-based provider routing (ANTHROPIC_API_KEY, etc.).
    # Set llm_api_base when you're running a LiteLLM proxy server (e.g., for
    # centralized budget enforcement, observability, or model routing) and
    # want techne's summarization calls to go through it.
    llm_api_base: str = ""
    llm_api_key: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or DEFAULT_CONFIG
        if not path.exists():
            return cls()
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, raw: dict) -> "Config":
        cfg = cls()
        if "db_path" in raw:
            cfg.db_path = Path(raw["db_path"]).expanduser()
        if "data_dir" in raw:
            cfg.data_dir = Path(raw["data_dir"]).expanduser()
        if "identity_dir" in raw:
            cfg.identity_dir = Path(raw["identity_dir"]).expanduser()
        if "platform" in raw:
            if raw["platform"] not in PLATFORMS:
                raise ValueError(
                    f"Invalid platform '{raw['platform']}'. Must be one of: {', '.join(PLATFORMS)}"
                )
            cfg.platform = raw["platform"]
        if "model" in raw:
            cfg.model = raw["model"]
        if "llm_api_base" in raw:
            cfg.llm_api_base = str(raw["llm_api_base"])
        if "llm_api_key" in raw:
            cfg.llm_api_key = str(raw["llm_api_key"])

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
            cfg.threads = ThreadConfig(
                min_observations=t.get("min_observations", 15),
                link_threshold=t.get("link_threshold", 0.85),
                stale_after_days=t.get("stale_after_days", 30),
                complete_after_hours=t.get("complete_after_hours", 48),
                detector=detector,
                summarizer=summarizer,
                tool_families={str(k): str(v) for k, v in tool_families.items()},
                entity_patterns=[str(p) for p in entity_patterns],
            )

        if "hooks" in raw and isinstance(raw["hooks"], dict):
            h = raw["hooks"]
            cfg.hooks = HookConfig(
                post_tool_use=h.get("post_tool_use", True),
                capture_bash=h.get("capture_bash", False),
                capture_read=h.get("capture_read", False),
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
            )

        if "search" in raw and isinstance(raw["search"], dict):
            s = raw["search"]
            cfg.search = SearchConfig(
                recency_weight=float(s.get("recency_weight", 0.3)),
                bm25_weight=float(s.get("bm25_weight", 0.7)),
                expansion_ratio=int(s.get("expansion_ratio", 2)),
                mmr_lambda=float(s.get("mmr_lambda", 0.3)),
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
            )

        if "staleness_hours" in raw:
            cfg.staleness_hours = int(raw["staleness_hours"])

        return cfg

    def save(self, path: Path | None = None):
        path = path or DEFAULT_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "db_path": str(self.db_path),
            "data_dir": str(self.data_dir),
            "platform": self.platform,
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
            },
            "search": {
                "recency_weight": self.search.recency_weight,
                "bm25_weight": self.search.bm25_weight,
                "expansion_ratio": self.search.expansion_ratio,
                "mmr_lambda": self.search.mmr_lambda,
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
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        if self.llm_api_key:
            path.chmod(0o600)
