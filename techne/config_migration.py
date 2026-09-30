"""Sparse config.yaml, and the one-time migration of a legacy full one (techne#1070).

THE DEFECT. Up to 0.17.0 `Config.save` wrote every field into config.yaml, and
`techne init` is a load->save round-trip, so the first init pinned that day's
defaults. A later release that changed a default never reached the install: the
loader read the pinned value. #993 lowered `reflect.emit_max_chars` 100000 ->
45000 and #1020 turned five hooks on, and neither took effect on any install that
ran `techne init` before them.

THE FIX HAS TWO HALVES.

1. SPARSE SAVE (`Config.save`). The file holds only keys the operator set: a key
   is written when it was present in the file this Config was loaded from, or
   when its value differs from the shipped default. Keys the file carried that
   this version does not know are written back unchanged. Everything else
   follows the defaults of whatever version is running. Tracking what the FILE
   held, rather than only "differs from default", is what keeps a deliberate
   pin that happens to equal today's default pinned: `hooks.capture_read: false`
   written by hand stays in the file and keeps meaning false if the default ever
   flips.

2. THIS MIGRATION, for a file written by the old full save. Such a file cannot
   say which values the operator chose, so the rule is: a key whose value equals
   a default that key has EVER shipped with is un-pinned (removed, so it follows
   future defaults); a key whose value matches no known default is kept, since
   no release could have written it; a key this module does not know is kept.
   The cost, stated plainly: an operator who deliberately set a value that was
   also a default at some point (say `per_turn_recall: false` before #1020
   flipped it) loses that pin. The backup and the one-line report exist so that
   case is recoverable and visible.

WHEN IT RUNS. `migrate_raw` is pure and runs on every `Config.load` of a legacy
file, so every reader (hooks, MCP server, CLI) sees the migrated values at once,
without a hook process ever writing config.yaml. The FILE is rewritten by the
next `Config.save` of that Config, i.e. `techne init`, which first copies the
original to `config.yaml.bak-<timestamp>`. The sparse file carries
`config_version: 2`, and a file with that key is never migrated again.

WHAT COUNTS AS LEGACY. No `config_version` key AND the shape every old save
produced (see `_looks_like_full_save`). A hand-written partial config is not
touched: every key in it is a choice somebody typed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Written at the top of every sparse config.yaml. A file carrying it is never
#: migrated. 1 was never written: the legacy full format simply had no key.
CONFIG_VERSION = 2
VERSION_KEY = "config_version"

#: EVERY EARLIER SHIPPED DEFAULT, for each key whose default has changed. Today's
#: default is not listed: the migration reads it from the running code. Built from
#: `git log -p -- techne/config.py` (dataclass field defaults at every revision on
#: main, cross-checked at tags v0.9.0..v0.17.0). The `_from_dict` fallback literals
#: were checked too and never held a value outside these sets.
#:
#: ADD A LINE HERE WHENEVER A DEFAULT CHANGES. Otherwise a legacy file pinned at
#: the old value keeps it, which is the #1070 defect again.
PAST_DEFAULTS: dict[str, tuple[Any, ...]] = {
    # 0.85 until 999fd54 (2026-06-11); unreachable in production (see ThreadConfig).
    "threads.link_threshold": (0.85,),
    # The five hooks #1020 turned on (2026-09-23). False in every tag v0.9.0..v0.17.0.
    "hooks.capture_bash": (False,),
    "hooks.codex_grep_assist": (False,),
    "hooks.codex_reindex_on_edit": (False,),
    "hooks.codex_edit_rail": (False,),
    "hooks.per_turn_recall": (False,),
    # 200 (v0.16.1, #826) -> 450 (v0.17.0, #852) -> 1000 (#1020).
    "hooks.per_turn_recall_chars": (200, 450),
    # 100000 until #993 (2026-09-25), above the MCP result cap.
    "reflect.emit_max_chars": (100_000,),
}

#: Keys techne once read (or claimed to) and no longer does. Dropped from a
#: migrated file and never written back by `save`. Only keys with no possible
#: reader belong here; a key a custom adapter might want is not retired.
RETIRED_KEYS = frozenset(
    {
        # Removed in 0.13.0 (#420): parsed and saved, passed to no search function.
        "search.mmr_lambda",
        # #1074: same defect. search_fts has its own default of 2 and search()
        # hard-codes limit * 2, so the knob changed nothing.
        "search.expansion_ratio",
    }
)


@dataclass
class ConfigMigration:
    """What migrating one legacy file did. Empty lists mean nothing to report."""

    #: Keys removed because they held a known default. Sorted dotted paths.
    unpinned: list[str] = field(default_factory=list)
    #: The subset of `unpinned` whose EFFECTIVE value changes, as
    #: (key, old pinned value, new default). The ones worth telling the operator.
    changed: list[tuple[str, Any, Any]] = field(default_factory=list)
    #: RETIRED_KEYS entries dropped.
    retired: list[str] = field(default_factory=list)

    def pending_line(self) -> str | None:
        """While the file is still legacy: the keys whose value IN THE FILE is not the
        value in effect, with the remedy. None when there are none.

        An operator who hand-edits a legacy file before `techne init` gets the edit
        un-pinned again on every load if the value is a past default (for example
        `per_turn_recall: false`), so the file and the running value disagree. This
        is the line that says so.
        """
        if not self.changed:
            return None
        keys = ", ".join(
            f"{k} (file: {_render(a)}, in effect: {_render(b)})" for k, a, b in self.changed
        )
        return (
            f"config.yaml sets {len(self.changed)} key(s) that are not in effect until it is "
            f"rewritten: {keys}. Run `techne init` to rewrite config.yaml, then re-set any "
            "key you want pinned."
        )

    def summary(self, backup: Path | None = None) -> str:
        """The one stdout line `techne init` and `techne doctor` print."""
        parts = [f"un-pinned {len(self.unpinned)} key(s) that held a shipped default"]
        if self.changed:
            moved = ", ".join(f"{k} {_render(a)}->{_render(b)}" for k, a, b in self.changed)
            parts.append(f"{len(self.changed)} now take a newer default: {moved}")
        if self.retired:
            parts.append(f"dropped retired {', '.join(self.retired)}")
        line = "config.yaml migrated to sparse form: " + "; ".join(parts)
        if backup is not None:
            line += f" (original kept at {backup})"
        return line


MISSING = object()


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def same_value(a: Any, b: Any) -> bool:
    """Equality as YAML means it. Python's `True == 1` is not: `per_turn_recall_limit: 1`
    must not read as the boolean default of some other key, and `capture_bash: 1` is
    not the literal `true` any release wrote."""
    if a is MISSING or b is MISSING:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if a is None or b is None:
        return a is None and b is None
    return a == b


def is_section(defaults: dict[str, Any], key: str) -> bool:
    """A top-level key whose sub-keys are separate settings (`hooks`, `reflect`...).

    Decided from the DEFAULT surface: a non-empty mapping there is a section. An
    empty one (`stores`, `connections`) or an absent one is a single value
    compared whole, since its sub-keys are names the operator chose, not settings.
    """
    d = defaults.get(key)
    return isinstance(d, dict) and bool(d)


def _looks_like_full_save(raw: dict[str, Any]) -> bool:
    """True when `raw` has the shape every pre-#1070 `Config.save` wrote.

    Every save since v0.5.0 wrote these unconditionally (checked against v0.5.0's
    save and the v0.16.1 / v0.17.0 `techne init` output). A hand-written config
    almost never carries all of them, and one that does is treated as legacy,
    which errs toward following defaults, with a backup.
    """
    if not all(k in raw for k in ("platform", "model", "sync", "threads", "hooks")):
        return False
    threads, hooks = raw.get("threads"), raw.get("hooks")
    if not isinstance(threads, dict) or not isinstance(hooks, dict):
        return False
    return {"min_observations", "link_threshold", "detector", "summarizer"} <= set(threads) and {
        "post_tool_use",
        "capture_bash",
        "capture_read",
    } <= set(hooks)


def needs_migration(raw: Any) -> bool:
    return isinstance(raw, dict) and VERSION_KEY not in raw and _looks_like_full_save(raw)


def _is_known_default(path: str, value: Any, current: Any) -> bool:
    if same_value(value, current):
        return True
    return any(same_value(value, past) for past in PAST_DEFAULTS.get(path, ()))


def migrate_raw(
    raw: dict[str, Any], defaults: dict[str, Any], home: Path
) -> tuple[dict[str, Any], ConfigMigration | None]:
    """Return (migrated copy of `raw`, report), or (`raw`, None) when not legacy.

    Pure: reads nothing and writes nothing. `defaults` is `Config().to_yaml_dict()`
    from the running code, which is today's default for every key the old save
    wrote. Idempotent: every removable key is gone from the output, so running it
    again removes nothing, and a run that removes nothing reports None.
    """
    if not needs_migration(raw):
        return raw, None
    out: dict[str, Any] = {}
    report = ConfigMigration()

    def unpin(path: str, old: Any, new: Any) -> None:
        report.unpinned.append(path)
        if not same_value(old, new):
            report.changed.append((path, old, new))

    for key, value in raw.items():
        if key in ("db_path", "data_dir"):
            # Not in `defaults` (to_yaml_dict omits them when they are the home
            # default, #947), so compared against the home they would resolve to.
            default_path = home / "techne.db" if key == "db_path" else home
            try:
                is_default = Path(str(value)).expanduser() == default_path
            except (TypeError, ValueError):
                is_default = False
            if is_default:
                unpin(key, value, value)
            else:
                out[key] = value
            continue
        if key in RETIRED_KEYS:
            report.retired.append(key)
            continue
        if is_section(defaults, key) and isinstance(value, dict):
            dsec = defaults[key]
            kept: dict[str, Any] = {}
            for sub, sval in value.items():
                path = f"{key}.{sub}"
                if path in RETIRED_KEYS:
                    report.retired.append(path)
                elif sub in dsec and _is_known_default(path, sval, dsec[sub]):
                    unpin(path, sval, dsec[sub])
                else:
                    kept[sub] = sval
            if kept:
                out[key] = kept
            continue
        if key in defaults and _is_known_default(key, value, defaults[key]):
            unpin(key, value, defaults[key])
            continue
        out[key] = value

    if not (report.unpinned or report.retired):
        return raw, None
    report.unpinned.sort()
    report.changed.sort(key=lambda t: t[0])
    report.retired.sort()
    log.debug(
        "config migration (techne#1070): unpinned=%s changed=%s retired=%s",
        report.unpinned,
        report.changed,
        report.retired,
    )
    return out, report


def backup_config(path: Path) -> Path:
    """Copy `path` to `<name>.bak-<YYYYmmdd-HHMMSS>` beside it, 0600, never overwriting.

    O_EXCL, so two saves in the same second cannot clobber each other's backup:
    the second takes a `-1`, `-2`... suffix. 0600 because config.yaml can hold
    `llm_api_key`.
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    data = path.read_bytes()
    for n in range(100):
        suffix = f".bak-{ts}" + (f"-{n}" if n else "")
        target = path.with_name(path.name + suffix)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        log.debug("config migration: backed up %s to %s", path, target)
        return target
    raise FileExistsError(f"could not pick a free backup name for {path}")
