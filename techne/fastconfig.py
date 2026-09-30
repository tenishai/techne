"""Stdlib-only fast path for hook config (techne#470).

WHY THIS EXISTS — measured, 2026-08-03, against the live 232MB store:

    bare interpreter                     40 ms
    + import techne.config              100 ms
    full pre_tool_use hook, end-to-end  145 ms

So ~100ms of every 145ms hook was startup before a single useful instruction, on
a hook that fires on EVERY matching tool call — 909 of them in one session, i.e.
roughly 90 seconds of pure interpreter boot doing nothing.

AND THE COST IS NOT WHERE YOU WOULD GUESS. The obvious hypothesis is "parsing
YAML on every hook is slow". It is not::

    import techne.config :  55.3 ms
    Config.load()        :   4.1 ms

The IMPORT is 13x the LOAD — ``yaml`` alone is 17ms, plus ``dataclasses``,
``tempfile``, ``shutil``, ``inspect``. Caching the parsed *values* would save
~4ms and would not be worth doing. The win is that a FLAT FILE IS STDLIB-
READABLE, so a hot hook can skip importing techne altogether::

    import techne.config + load()   100 ms
    stdlib-only flat read            40 ms
    ...plus a real sqlite query      40 ms

The last line is the point: once the import is gone, a hook does useful work in
the time the bare interpreter takes to boot. That is the Python floor.

THIS MODULE MUST STAY IMPORT-CHEAP. It may import ONLY from the standard
library, and only modules already paid for by interpreter startup (``os``,
``pathlib``). Adding a ``techne.*`` import here silently deletes the entire
benefit, because the import graph is the cost. There is a test pinning this.

STALENESS IS THE TRAP, and it is the failure class techne#468 exists to catch: a
cache that silently disagrees with its source is a FRESHNESS contract. Hand-edit
``config.yaml``, the cache does not regenerate, and hooks run on stale config
while ``techne status`` shows the new value — silent, and indistinguishable from
working. So the cache is self-healing BY CONSTRUCTION, not by discipline:

* the source's mtime+size are stamped into the cache when it is written;
* :func:`load_fast` ``stat()``s the source (one syscall, microseconds) and
  returns None when they disagree, so the caller falls back to the real
  ``Config`` and regenerates as a side effect;
* a missing, unreadable or unparseable cache is a MISS, never an error.

That makes this an optimisation that cannot be wrong, only occasionally slow.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Only these keys are cached. An ALLOWLIST, not a dump of the resolved config,
#: for two reasons: no secret (``llm_api_key``, tokens) can ever land in a
#: plaintext file next to the store, and the cache cannot quietly grow into a
#: second source of truth for values nobody meant to cache. Adding a key here is
#: a deliberate act — the fast path is for hot hooks, not for general config.
CACHED_KEYS = (
    "db_path",
    "hooks.timeout_s",
    "hooks.codex_grep_assist",
    "hooks.codex_grep_assist_limit",
    "hooks.per_turn_recall",
    #: Cached WITH per_turn_recall, never without it: user_prompt_submit gates on
    #: `per_turn_recall or per_turn_seed`, so a fast path reading only the first
    #: would silently kill the seed rail whenever recall is off. Added before any
    #: such fast path exists, so the next commit cannot inherit the trap (#471
    #: review, finding 3).
    "hooks.per_turn_seed",
    "hooks.post_tool_use",
    "hooks.capture_bash",
    "hooks.capture_read",
)

CACHE_NAME = ".config.env"
SOURCE_NAME = "config.yaml"


def _home() -> Path:
    """Duplicated from config.techne_home ON PURPOSE — importing it would pull
    the module this exists to avoid. Kept to three lines so the duplication is
    obvious and cheap to keep in sync; a test asserts the two agree."""
    env = os.environ.get("TECHNE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".techne"


#: The module that holds the shipped defaults. Part of the stamp (techne#1070).
_DEFAULTS_SOURCE = Path(__file__).with_name("config.py")


def _stamp(source: Path) -> str:
    """Source identity: mtime_ns + size. Cheaper and finer-grained than hashing,
    and size catches the (rare) same-mtime edit.

    THE DEFAULTS ARE A SECOND SOURCE (techne#1070). config.yaml is sparse: a key it
    does not set takes the default from `techne/config.py`, so an upgrade that
    changes a default changes a cached value without touching config.yaml. Stamped
    on the yaml alone, the cache would keep serving the old default until someone
    edited the file. `config.py`'s own mtime+size is folded in, one more stat.
    """
    st = source.stat()
    try:
        code = _DEFAULTS_SOURCE.stat()
        code_part = f"{code.st_mtime_ns}:{code.st_size}"
    except OSError:
        code_part = "-"
    return f"{st.st_mtime_ns}:{st.st_size}:{code_part}"


def stamp_source(source: Path) -> str:
    """Public alias for :func:`_stamp`, so ``Config.load`` can take the stamp
    BEFORE it reads the file. See :func:`write_cache`'s ``stamp`` parameter."""
    return _stamp(source)


def write_cache(
    values: dict[str, object], home: Path | None = None, stamp: str | None = None
) -> None:
    """Write the flat cache. Best-effort: never raises into a caller.

    Called as a side effect of the real ``Config.load()``, which already holds
    fully-resolved values, so the cache cannot drift from the parse that
    produced it.

    ``stamp`` MUST BE TAKEN BEFORE THE SOURCE IS READ, and ``Config.load`` does
    exactly that. Stamping here — after the parse — was a TOCTOU that poisoned
    the cache PERMANENTLY rather than transiently: an edit landing between the
    parse and the stat wrote v1's VALUES under v2's IDENTITY, so every later read
    verified as fresh and returned the old answer until the next edit happened to
    rewrite it (#471 review, finding 1; reproduced live). Taking the stamp first
    fails in the safe direction — a concurrent edit makes the cache look STALE, a
    miss falls through to the slow path, and the slow path regenerates it.
    """
    home = home or _home()
    source = home / SOURCE_NAME
    if stamp is None:
        # SELF-STAMPING IS THE TOCTOU, so it is safe only for a caller that has
        # NOT already read the source — direct `write_cache` users, i.e. tests.
        # `Config.load`, which has read it, never reaches here: it passes its
        # pre-read stamp and skips the write entirely when it could not take one
        # (#471 review round 2, nit).
        try:
            stamp = _stamp(source)
        except OSError:
            # No source file → nothing to be stale against, so caching would be
            # unverifiable. Skip rather than write an uncheckable cache.
            return
    lines = [f"# generated by techne — do not edit; regenerated from {SOURCE_NAME}", f"@={stamp}"]
    for key in CACHED_KEYS:
        if key in values and values[key] is not None:
            rendered = str(values[key])
            # THE FLAT FORMAT HAS NO ESCAPING, so a value containing a newline
            # injects extra lines and can forge another key. Today that is not
            # exploitable — keys are written in fixed CACHED_KEYS order, so a
            # smuggled key is always overridden by the real one later — but the
            # immunity rests on an ordering nobody wrote down, and a newline in
            # `db_path` would silently truncate the cached value. Refuse the
            # whole write instead: degrading to SLOW is this module's contract,
            # degrading to WRONG is not. (#471 review, nit 1.)
            if "\n" in rendered:
                return
            lines.append(f"{key}={rendered}")
    # PID-UNIQUE TMP. `replace()` is atomic for the RENAME, not for the CONTENT
    # being renamed: with a shared tmp name, two concurrent writers interleave
    # their writes into one file and a torn mix is renamed into place under a
    # fresh stamp. Thirteen modules call Config.load(), and every call writes
    # this file.
    #
    # THE TEAR NEEDS A PAYLOAD BIG ENOUGH TO SPLIT ACROSS write() SYSCALLS, and
    # today's cache is ~425 bytes, well under it. Measured against the pre-fix
    # code with 6 writer processes: 0 tears in 19,853 reads at production size,
    # first tears at 16KB (2 / 16,247), 48 / 3,909 at 200KB. So this is a latent
    # hazard rather than a live one — CACHED_KEYS is explicitly growable, and the
    # line costs nothing. The numbers live here and in the regression test's
    # docstring; keep them in agreement (#471 review finding 2, whose original
    # 217/2768 figure was retracted in round 2 after both of us re-measured).
    tmp = home / f"{CACHE_NAME}.{os.getpid()}.tmp"
    try:
        home.mkdir(parents=True, exist_ok=True)
        tmp.write_text("\n".join(lines) + "\n")
        # 0600: this discloses db_path, and everything else in ~/.techne is
        # owner-only. The default is umask-dependent and was landing 0644.
        os.chmod(tmp, 0o600)
        # Atomic: a hook must never read a half-written cache.
        tmp.replace(home / CACHE_NAME)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def load_fast(home: Path | None = None) -> dict[str, str] | None:
    """Read the cache, or None on any miss (absent / stale / malformed).

    None means "use the slow path" — it is never an error, and the caller must
    treat it as such. Returning stale values would be worse than being slow.
    """
    home = home or _home()
    cache = home / CACHE_NAME
    try:
        text = cache.read_text()
    except OSError:
        return None
    try:
        current = _stamp(home / SOURCE_NAME)
    except OSError:
        # Source vanished after the cache was written — cannot verify freshness.
        return None

    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if not _:
            continue
        out[key] = value
    if out.pop("@", None) != current:
        return None  # source changed since the cache was written
    return out


def get_bool(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_float(values: dict[str, str], key: str, default: float) -> float:
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError):
        return default


def get_int(values: dict[str, str], key: str, default: int) -> int:
    try:
        return int(values[key])
    except (KeyError, TypeError, ValueError):
        return default
