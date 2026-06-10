"""Configuration management for techne."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

Platform = Literal["claude-code", "codex", "standalone"]

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
    link_threshold: float = 0.85
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


@dataclass
class Config:
    db_path: Path = DEFAULT_DB
    data_dir: Path = DEFAULT_DIR
    identity_dir: Path | None = None
    sync: SyncConfig = field(default_factory=SyncConfig)
    threads: ThreadConfig = field(default_factory=ThreadConfig)
    hooks: HookConfig = field(default_factory=HookConfig)
    platform: Platform = "claude-code"
    model: str = "claude-haiku-4-5-20251001"

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
            if raw["platform"] not in ("claude-code", "codex", "standalone"):
                raise ValueError(
                    f"Invalid platform '{raw['platform']}'. "
                    f"Must be one of: claude-code, codex, standalone"
                )
            cfg.platform = raw["platform"]
        if "model" in raw:
            cfg.model = raw["model"]

        if "sync" in raw and isinstance(raw["sync"], dict):
            s = raw["sync"]
            cfg.sync = SyncConfig(
                adapter=s.get("adapter", "noop"),
                url=s.get("url", ""),
                auth=s.get("auth", ""),
                audience=s.get("audience", ""),
                path=s.get("path", ""),
                extra={k: v for k, v in s.items() if k not in (
                    "adapter", "url", "auth", "audience", "path"
                )},
            )

        if "threads" in raw and isinstance(raw["threads"], dict):
            t = raw["threads"]
            detector = t.get("detector", "heuristic")
            if detector not in ("heuristic", "llm"):
                raise ValueError(
                    f"Invalid threads.detector '{detector}'. "
                    "Must be 'heuristic' or 'llm'."
                )
            summarizer = t.get("summarizer", "heuristic")
            if summarizer not in ("heuristic", "llm"):
                raise ValueError(
                    f"Invalid threads.summarizer '{summarizer}'. "
                    "Must be 'heuristic' or 'llm'."
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

        return cfg

    def save(self, path: Path | None = None):
        path = path or DEFAULT_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
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
        }
        if self.identity_dir:
            data["identity_dir"] = str(self.identity_dir)
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
