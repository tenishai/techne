"""Utilization audit over the observation ledger + injection log (techne#329).

Read-only panels, no writes:
  - ``injection_utilization`` — per-source aggregate over ``context_injections``
    (the memory read-path that #331 made visible).
  - ``read_write_ledger`` — read-vs-write counts per memory domain over
    ``observations`` (refreshes #329's original findings table on live data).
  - ``edit_rail_attribution`` — proximity-attribution for ``codex_edit_rail``
    (techne#374): did a SUBSEQUENT edit touch an injected dependent?
  - ``recall_attribution`` — citation-attribution for the KNOWLEDGE rails
    (``per_turn_recall`` / ``session_start``): did the agent go on to cite an
    injected item's id? This is #329 step 1's deferred "was the injected item
    actually used" join, for the two sources that had no attribution at all.

The first two are DESCRIPTIVE only. The two attribution panels are NOT causation,
and they are biased in OPPOSITE directions — read them accordingly:

  - ``edit_rail_attribution`` is PROXIMITY ("an injected dependent was edited
    within the next few edits"), which OVER-credits: the edit may well have
    happened anyway. Its tight window and trigger-file exclusion exist to damp
    exactly that.
  - ``recall_attribution`` is CITATION ("the agent wrote techne#<id> after being
    shown it"), which UNDER-credits: memory can obviously be used — and usually
    is — without the agent ever typing the id. It is a floor, not a rate.

A true counterfactual ("would it have happened WITHOUT the injection") remains
UNMEASURED for both, and is tracked by techne#682. It was cited here as #105
(the evidence gate, which asks whether an agent's CLAIM has ledger backing — a
different question) and as #416 in _injection_log (which shipped the PROXY and
deferred the judge).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from techne.core.stores import plain_connect

# Tool-name groupings — domain semantics, not user config. Full MCP tool names
# as stored in observations.tool_name. Centralised here so the audit's notion of
# "a memory read" has one home as the tool surface grows.
_MEMORY_WRITE = ("mcp__techne__techne_store",)
_MEMORY_READ = ("mcp__techne__techne_recall", "mcp__techne__techne_search")
_CODEX_READ = ("mcp__techne__codex_search", "mcp__techne__codex_blast_radius")
# The archivist's own read tools. `techne_archivist_query` is the SQL surface;
# `archivist_search` is the domain-named one added in #724. Counted by nothing at all
# until #727 r4 — so three of the four domain tools landed in the ledger and the
# archivist's vanished. THE BITE DOES NOT NEED AN ARCHIVIST ROW: when a read moves from
# `techne_recall` to `archivist_search`, which the docs tell operators to do,
# `memory.read` falls and the panel reports a read-path DECLINE that is actually a
# read-path MIGRATION. (Prudence, #725 r4.)
_ARCHIVIST_READ = (
    "mcp__techne__archivist_search",
    "mcp__techne__techne_archivist_query",
)
_ARCHITECT_READ = (
    "mcp__techne__pattern_search",
    "mcp__techne__pattern_related",
    "mcp__techne__architect_search",
)
# NOTE (techne#329 review): techne exposed NO librarian MCP read tool — the store
# was reachable only via the `techne library` CLI, which is not a tool-use
# observation — so this ledger structurally COULD NOT measure librarian
# utilization, and 0 had to be reported as "invisible", never as "never queried"
# (which implies a tool agents ignore rather than a store the ledger cannot see).
#
# THAT CHANGED IN #724: `librarian_search` exists, and it matches this prefix, so
# the counting auto-lit-up exactly as the comment promised. `mcp_read_tool` did
# NOT, because it was a HARDCODED False beside a prefix matcher that had already
# started counting — the ledger had the number and declined to print it, telling
# an operator "no MCP read tool" for a tool the tutorial tells them to call.
# Prudence ranked that the one finding not to let reach the release notes (#727).
#
# So it is DERIVED now. A hardcoded boolean beside a derived count is two answers
# to one question, and they drift the moment the surface grows — which is the
# whole defect the domain vocabulary exists to remove.
_LIBRARIAN_READ_PREFIX = "mcp__techne__librarian_"


#: Domain -> the read tools its counter actually sums. ONE FACT, TWO CONSUMERS: the
#: ledger counts through this and `_has_mcp_read_tool` asks through it, so the count
#: and the "does a tool exist" boolean CANNOT disagree.
#:
#: They were two facts. The predicate asked "is a tool named `<domain>_*` registered?"
#: — a naming convention — while the counters name tools that do not match their own
#: domain prefix at all: `techne_archivist_query`, `pattern_search`, `pattern_related`.
#: They agreed on the boolean only by luck of which tools happened to exist. Prudence
#: demonstrated the divergence: seed 7 `techne_archivist_query` reads, drop
#: `archivist_search`, and the panel prints "no MCP read tool — not visible to this
#: ledger" while the dict holds `read: 7`. The count discarded and the operator told
#: the opposite of the truth — the same inversion #727 fixed, through a different door.
#: (#728 review r5.)
_DOMAIN_READ_TOOLS: dict[str, tuple[str, ...] | str] = {
    "archivist": _ARCHIVIST_READ,
    "architect": _ARCHITECT_READ,
    "codex": _CODEX_READ,
    "librarian": _LIBRARIAN_READ_PREFIX,  # a PREFIX, so a new librarian_* tool auto-counts
}


def _has_mcp_read_tool(domain: str) -> bool:
    """Does a domain have an MCP read tool at all?

    ASKS THE REGISTRY THROUGH THE SAME TUPLE THE COUNTER SUMS, so the boolean and the
    count are one fact rather than two that agree by luck. This distinguishes "0 because
    agents ignore an existing tool" (a real behavioural signal) from "0 because there
    is nothing to call" (ledger-invisible, not neglect) — and getting it wrong inverts
    the meaning of every zero in the report.

    A PREVIOUS VERSION WAS `domain in DOMAIN_NAMES`, WHICH IS A CONSTANT WEARING A
    DERIVATION'S CLOTHES. Both call sites pass literals that are in `DOMAIN_NAMES`
    forever, so it never consulted anything — I had replaced a hardcoded `False` with a
    hardcoded `True`. Prudence proved it by removing `librarian_search` from the
    registry: the tool was gone, the function still said True, and both tests stayed
    green. (#727, caught in the #725 r4 review.)

    Import is deliberately lazy: `mcp_server` imports from `core`, not the reverse, and
    a module-level import here would invert that.
    """
    from techne.mcp_server import TOOL_HANDLERS

    spec = _DOMAIN_READ_TOOLS.get(domain)
    if spec is None:
        return False
    registered = {f"mcp__techne__{name}" for name in TOOL_HANDLERS}
    if isinstance(spec, str):  # a prefix
        return any(name.startswith(spec) for name in registered)
    return any(name in registered for name in spec)


# Code-write baseline for the codex "reads per edit" ratio. Edit only, to stay
# directly comparable to #329's original finding (Edit dominates by ~100x).
_CODE_EDIT = ("Edit",)


def _window(since_days: int | None) -> tuple[str, list]:
    """Optional recency filter on a ``timestamp`` column. Returns (where, params).

    ``timestamp`` is stored UTC ('YYYY-MM-DD HH:MM:SS'), so a lexical compare
    against ``datetime('now', '-N days')`` (also UTC) windows correctly.
    """
    if since_days is None:
        return "", []
    return "WHERE timestamp >= datetime('now', ?)", [f"-{int(since_days)} days"]


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def injection_utilization(db_path: Path, *, since_days: int | None = None) -> list[dict]:
    """Per-source aggregate over ``context_injections``.

    One row per ``source`` (session_start / per_turn_recall / codex_edit_rail /
    …): event count, total+avg hits, total+avg approx_tokens, distinct sessions,
    empty-fire count (``hit_count=0`` — fired but selected nothing, itself a
    signal), and the first/last timestamp. Returns [] if nothing has been logged
    (the table is created lazily by the injection writer, so it may not exist).
    """
    where, params = _window(since_days)
    conn = plain_connect(db_path)
    try:
        if not _table_exists(conn, "context_injections"):
            return []
        # total_hits (pre-cap radius, techne#343) is a later column — a table
        # written before it exists lacks it, so only AVG it when present.
        cols = {c[1] for c in conn.execute("PRAGMA table_info(context_injections)").fetchall()}
        radius_expr = "AVG(total_hits)" if "total_hits" in cols else "NULL"
        rows = conn.execute(
            f"""SELECT source,
                       COUNT(*)                                    AS events,
                       COALESCE(SUM(hit_count), 0)                 AS sum_hits,
                       COALESCE(SUM(approx_tokens), 0)             AS total_tokens,
                       COUNT(DISTINCT session_id)                  AS sessions,
                       SUM(CASE WHEN hit_count = 0 THEN 1 ELSE 0 END) AS empty_events,
                       {radius_expr}                              AS avg_radius,
                       MIN(timestamp)                             AS first_at,
                       MAX(timestamp)                             AS last_at
                FROM context_injections
                {where}
                GROUP BY source
                ORDER BY events DESC""",
            params,
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        events = r["events"]
        avg_radius = r["avg_radius"]
        out.append(
            {
                "source": r["source"],
                "events": events,
                # total_hits = injected items summed across events (the shown/cost
                # basis); avg_radius = mean pre-cap blast-radius size, only for
                # sources that cap their injection (codex_edit_rail). (techne#343)
                "total_hits": r["sum_hits"],
                "avg_hits": round(r["sum_hits"] / events, 1) if events else 0.0,
                "total_tokens": r["total_tokens"],
                "avg_tokens": round(r["total_tokens"] / events) if events else 0,
                "sessions": r["sessions"],
                "empty_events": r["empty_events"],
                "avg_radius": round(avg_radius, 1) if avg_radius is not None else None,
                "first_at": r["first_at"],
                "last_at": r["last_at"],
            }
        )
    return out


def read_write_ledger(db_path: Path, *, since_days: int | None = None) -> dict:
    """Read-vs-write counts per memory domain over ``observations``.

    Refreshes #329's findings table: memory store-vs-recall/search, codex
    reads-vs-edits, and the two write-only ghosts (architect patterns, librarian
    — 0 reads at the time of the audit). Ratios are ``None`` when the denominator
    is zero (never read) rather than raising.
    """
    where, params = _window(since_days)
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT tool_name, COUNT(*) AS n FROM observations {where} GROUP BY tool_name",
            params,
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM observations {where}", params).fetchone()[0]
    finally:
        conn.close()

    counts = {r["tool_name"]: r["n"] for r in rows}

    def sum_tools(names: tuple[str, ...]) -> int:
        return sum(counts.get(n, 0) for n in names)

    def sum_prefix(prefix: str) -> int:
        return sum(n for t, n in counts.items() if t.startswith(prefix))

    mem_write = sum_tools(_MEMORY_WRITE)
    mem_read = sum_tools(_MEMORY_READ)
    codex_read = sum_tools(_CODEX_READ)
    code_edit = sum_tools(_CODE_EDIT)
    architect_read = sum_tools(_ARCHITECT_READ)
    archivist_read = sum_tools(_ARCHIVIST_READ)
    librarian_read = sum_prefix(_LIBRARIAN_READ_PREFIX)

    return {
        "total_observations": total,
        "memory": {
            "write": mem_write,
            "read": mem_read,
            "writes_per_read": round(mem_write / mem_read, 2) if mem_read else None,
        },
        "codex": {
            "read": codex_read,
            "edit": code_edit,
            "edits_per_read": round(code_edit / codex_read, 1) if codex_read else None,
        },
        # mcp_read_tool distinguishes "0 because agents don't call an existing
        # MCP tool" (a real behavioural signal) from "0 because there is no MCP
        # tool to call" (ledger-invisible, not neglect). DERIVED, not hardcoded —
        # see `_has_mcp_read_tool`.
        "architect": {"read": architect_read, "mcp_read_tool": _has_mcp_read_tool("architect")},
        "librarian": {"read": librarian_read, "mcp_read_tool": _has_mcp_read_tool("librarian")},
        "archivist": {"read": archivist_read, "mcp_read_tool": _has_mcp_read_tool("archivist")},
    }


# ---- edit-rail attribution (techne#374) ------------------------------------

# Edit-family observations record their target path in a tool-specific shape
# (post_tool_use.summarize): Edit → "Edited: <path>\nChange: <old> → <new>",
# Write → "Wrote: <path>\nContent: <body>…", MultiEdit → the generic fallback
# "Tool: MultiEdit\nInput: {json …\"file_path\": <path>…}". All three FIRE the
# rail and all three are plausible follow-up edits to a dependent, so all three
# must parse — an Edit-only regex silently drops Write/MultiEdit and biases
# attribution downward (techne#374 review). Paths are absolute + machine-specific,
# but injected dependent ids are repo-RELATIVE ("techne/cli.py::name::kind::line"),
# so we match by path suffix rather than reconstructing the repo root.
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit")
_EDIT_LABEL_RE = re.compile(r"^(?:Edited|Wrote):\s*(\S+)")
_MULTIEDIT_PATH_RE = re.compile(r'"file_path":\s*"([^"]+)"')
# A synthetic module-scope symbol — the from_symbol codex assigns to calls/imports
# outside any def (source.py). As an injected "dependent" it's near-useless noise.
_MODULE_MARKER = "::<module>::module::"


def _sym_file(symbol_id: str) -> str:
    """Repo-relative file of a codex symbol id ('file::name::kind::line')."""
    return symbol_id.split("::", 1)[0]


def _sym_name(symbol_id: str) -> str | None:
    """The symbol's name segment, or None for a synthetic module node."""
    parts = symbol_id.split("::")
    if len(parts) < 2 or parts[1] == "<module>":
        return None
    return parts[1]


def _is_test_path(path: str) -> bool:
    """Heuristic: a test file (tests/… dir or a test_*.py basename)."""
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    return "tests/" in p or "/test/" in p or base.startswith("test_")


def _parse_edit(content: str | None) -> tuple[str, str] | None:
    """Extract (abspath, change_body) from an edit-family observation, or None.

    change_body is the CHANGE text only, never the path line — so a dependent
    whose name also appears in its own filename (symbol ``foo`` in ``foo.py``)
    doesn't self-credit the symbol match (techne#374 review nit). Handles Edit,
    Write, and MultiEdit (see _EDIT_TOOLS commentary above)."""
    if not content:
        return None
    first, _, rest = content.partition("\n")
    m = _EDIT_LABEL_RE.match(first)
    if m:
        # rest is "Change: <old> → <new>" or "Content: <body>…" — drop the label.
        body = rest.split(":", 1)[1] if ":" in rest else rest
        return m.group(1), body
    if first.startswith("Tool: MultiEdit"):
        m = _MULTIEDIT_PATH_RE.search(content)
        if m:
            path = m.group(1)
            # Body = the whole observation minus the path value, so the path
            # can't leak into the symbol scan.
            return path, content.replace(path, "")
    return None


def _edit_hits_file(edit_abspath: str, rel_files: set[str]) -> bool:
    """True if the edited absolute path ends with any injected relative file.

    Suffix match (the "/" guard prevents ``barfoo.py``↔``foo.py``); a bare
    top-level ``util.py`` could still match ``sub/util.py``, but that collision is
    rare and dissolves once edges carry resolved paths (techne#374 review nit)."""
    p = edit_abspath.replace("\\", "/")
    return any(p == f or p.endswith("/" + f) for f in rel_files)


def edit_rail_attribution(
    db_path: Path,
    *,
    since_days: int | None = None,
    window_minutes: float = 2.0,
    next_edits: int = 3,
) -> dict:
    """Proximity-attribution for ``codex_edit_rail`` injections (techne#374).

    For each injection, look at the SAME session's edits that follow it — capped
    to the next ``next_edits`` edits within ``window_minutes`` — and ask whether
    any touched an injected dependent, at two granularities:

      - **file** — an injected dependent's file was edited. The trigger's OWN file
        (``trigger_snippet``'s file — the symbol you're already changing) is
        excluded from credit; editing it is not "used a dependent".
      - **symbol** — a following edit's captured text MENTIONS an injected
        dependent's name. Stronger (the edit plausibly touched the dependent
        itself), but name-based, so short/common names can false-positive.

    Also reports **payload composition** — what the rail actually injected: the
    share of dependents that are synthetic module nodes / test files / 'real'
    symbols — because #374's telemetry showed the payload is polluted with both,
    which caps real attribution regardless of thresholds.

    Window default (next 3 edits / 2 min) is deliberately tight: the naive
    whole-session number is confounded (see module docstring). Returns a dict with
    ``injections`` == 0 when nothing has been logged.
    """
    where, params = _window(since_days)
    clause = (where + " AND " if where else "WHERE ") + "source = 'codex_edit_rail'"
    conn = plain_connect(db_path)
    try:
        if not _table_exists(conn, "context_injections"):
            return _empty_attribution(window_minutes, next_edits)
        injs = conn.execute(
            f"""SELECT session_id, timestamp, trigger_snippet, hit_ids
                FROM context_injections
                {clause}
                ORDER BY session_id, timestamp""",
            params,
        ).fetchall()
        # Edits only for the sessions that fired the rail (bounds the scan). Store
        # (dt, abspath, body): dt = parsed naive-UTC time (NOT a raw string — live
        # and transcript-imported observations use different timestamp formats, so
        # lexical compare is unsound, techne#374 review). Keep only rows whose time
        # AND target path both parse; sort by real time in Python.
        sessions = {r["session_id"] for r in injs if r["session_id"]}
        edits: dict[str, list[tuple[datetime, str, str]]] = {s: [] for s in sessions}
        if sessions:
            placeholders = ",".join("?" * len(sessions))
            tool_ph = ",".join("?" * len(_EDIT_TOOLS))
            for r in conn.execute(
                f"""SELECT session_id, timestamp, content FROM observations
                    WHERE session_id IN ({placeholders})
                      AND tool_name IN ({tool_ph})""",
                [*sessions, *_EDIT_TOOLS],
            ):
                dt = _parse_ts(r["timestamp"])
                parsed = _parse_edit(r["content"])
                if dt is not None and parsed is not None:
                    edits[r["session_id"]].append((dt, parsed[0], parsed[1]))
    finally:
        conn.close()
    for rows in edits.values():
        rows.sort(key=lambda e: e[0])

    total = file_hit = symbol_hit = empty_window = noise_only = 0
    dep_total = dep_module = dep_test = dep_real = 0

    for inj in injs:
        total += 1
        try:
            ids = [i for i in json.loads(inj["hit_ids"] or "[]") if i]
        except (ValueError, TypeError):
            ids = []
        trigger_file = _sym_file(inj["trigger_snippet"]) if inj["trigger_snippet"] else None

        # Payload composition (over every injected dependent).
        real_ids = []
        for sid in ids:
            dep_total += 1
            if _MODULE_MARKER in sid:
                dep_module += 1
            elif _is_test_path(_sym_file(sid)):
                dep_test += 1
            else:
                dep_real += 1
                real_ids.append(sid)
        if not real_ids:
            noise_only += 1

        # Credited targets: dependents minus anything in the trigger's own file.
        credited_files = {_sym_file(s) for s in ids if _sym_file(s) != trigger_file}
        credited_names = {
            n for s in ids if _sym_file(s) != trigger_file and (n := _sym_name(s)) is not None
        }

        # The next `next_edits` edits within `window_minutes` after the injection.
        # Both sides are parsed naive-UTC datetimes (no lexical/format mixing).
        inj_dt = _parse_ts(inj["timestamp"])
        following: list[tuple[str, str]] = []  # (abspath, body)
        if inj_dt is not None:
            for edt, abspath, body in edits.get(inj["session_id"], []):
                if edt <= inj_dt:
                    continue
                if (edt - inj_dt).total_seconds() > window_minutes * 60:
                    continue
                following.append((abspath, body))
                if len(following) >= next_edits:
                    break

        if not following:
            empty_window += 1
            continue
        if any(_edit_hits_file(abspath, credited_files) for abspath, _ in following):
            file_hit += 1
        if any(any(name in body for name in credited_names) for _, body in following):
            symbol_hit += 1

    def pct(n: int) -> float | None:
        return round(100 * n / total, 1) if total else None

    # Attributable denominator = injections that HAD a follow-up edit in window.
    # Rates over this base separate "the rail rarely precedes an edit" from "edits
    # happen but miss the dependents" (techne#374 review suggestion).
    attributable = total - empty_window

    def apct(n: int) -> float | None:
        return round(100 * n / attributable, 1) if attributable else None

    return {
        "window_minutes": window_minutes,
        "next_edits": next_edits,
        "injections": total,
        "file_attributed": file_hit,
        "file_attributed_pct": pct(file_hit),
        "file_attributed_pct_attributable": apct(file_hit),
        "symbol_attributed": symbol_hit,
        "symbol_attributed_pct": pct(symbol_hit),
        "symbol_attributed_pct_attributable": apct(symbol_hit),
        "attributable": attributable,
        "no_edits_in_window": empty_window,
        "noise_only_injections": noise_only,  # 100% module/test payload
        "noise_only_pct": pct(noise_only),
        "payload": {
            "dependents": dep_total,
            "module_pct": round(100 * dep_module / dep_total, 1) if dep_total else None,
            "test_pct": round(100 * dep_test / dep_total, 1) if dep_total else None,
            "real_pct": round(100 * dep_real / dep_total, 1) if dep_total else None,
        },
    }


def _parse_ts(s: str | None) -> datetime | None:
    """Parse a stored timestamp to a NAIVE-UTC datetime; None if unparseable.

    Two formats coexist in ``observations.timestamp``: live captures write naive
    ``YYYY-MM-DD HH:MM:SS`` (SQLite CURRENT_TIMESTAMP, UTC), while transcript-
    imported rows write the raw ISO transcript time with a ``Z``/offset suffix
    (tz-AWARE). Mixing aware and naive datetimes in a subtraction raises TypeError
    — which used to take down ``techne audit`` (techne#374 review). Normalising
    everything to naive-UTC here makes all comparisons safe."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _empty_attribution(window_minutes: float, next_edits: int) -> dict:
    return {
        "window_minutes": window_minutes,
        "next_edits": next_edits,
        "injections": 0,
        "file_attributed": 0,
        "file_attributed_pct": None,
        "file_attributed_pct_attributable": None,
        "symbol_attributed": 0,
        "symbol_attributed_pct": None,
        "symbol_attributed_pct_attributable": None,
        "attributable": 0,
        "no_edits_in_window": 0,
        "noise_only_injections": 0,
        "noise_only_pct": None,
        "payload": {"dependents": 0, "module_pct": None, "test_pct": None, "real_pct": None},
    }


# ---- recall attribution (techne#329 step 1's deferred join) -----------------

# The knowledge rails: sources whose hit_ids are `knowledge` row ids (ints), as
# opposed to codex_edit_rail's symbol-id strings. Only these can be citation-
# attributed, because only these inject items that HAVE a citable id.
_KNOWLEDGE_RAILS = ("per_turn_recall", "session_start")

# How injected knowledge gets referenced in prose. Four spellings are live in the
# corpus: "techne#237" (the original), "knowledge#956" (maintenance-rail cross-
# references), "🧠2862" (the interactive sigil since 2026-08-31, knowledge #2862),
# and "knowledge #2862" (the machine-readable form used in PR and issue bodies).
# Before techne#995 only the first two matched, so every citation written in the
# current convention went uncounted. The ⟦…⟧ brackets the injection banner uses
# sit OUTSIDE the match, so ⟦techne#237⟧ is caught by the same pattern.
# Exactly those four: "techne #N" (with a space) is NOT one, and it is also how a
# GitHub issue gets written in prose, so it stays unmatched (PR #996 r1).
_CITATION_RE = re.compile(r"(?:knowledge ?#|techne#|🧠)(\d+)")

# Which rail wrote a knowledge row, from knowledge.source: reflect-promoted rows
# carry "reflect-promote:<session>:<n>" (or "reflect:<...>"), consolidate merges
# "consolidate-merge:<candidate>". Everything else is bucketed as "hand". That
# includes rows stored directly ("agent", "session", "manual", "team"), whatever
# free-text source a caller passes to techne_store, document_card rows sourced by
# URL or path, the probe canary, and legacy imports ("cc-memory:*").
#
# An injected id not found in `knowledge` is "unknown", never "hand": hand is the
# baseline reflect is compared against, so it must not absorb rows of any origin
# (PR #996 r1). Both knowledge rails inject knowledge rows only (per_turn_recall
# calls search(table="knowledge")), so today "unknown" means a row deleted after
# it was injected.
_ORIGINS = ("reflect", "consolidate", "hand", "unknown")


def knowledge_origin(source: str | None) -> str:
    s = source or ""
    if s.startswith("reflect"):
        return "reflect"
    if s.startswith("consolidate"):
        return "consolidate"
    return "hand"


# Tools whose captured Output is GENERATED BY THE STORE, not written by the agent.
# post_tool_use.summarize records both Input and Output, so a techne_recall
# observation literally contains the ids of the rows it returned — crediting those
# would be the store citing itself and would inflate attribution toward 100% for
# any session that called recall. This is the direct analogue of excluding the
# trigger's OWN file in edit_rail_attribution. These still CONSUME a window slot
# (they are real actions); they just cannot earn credit.
_ECHO_TOOLS = frozenset(
    {
        "mcp__techne__techne_recall",
        "mcp__techne__techne_search",
        "mcp__techne__techne_resume",
        "mcp__techne__techne_status",
        "mcp__techne__techne_reminder_list",
        "mcp__techne__techne_reflect_emit",
        "mcp__techne__techne_consolidate_emit",
        "mcp__techne__techne_dream_emit",
        "mcp__techne__techne_memory_claims_emit",
        "mcp__techne__techne_faithfulness_emit",
        "mcp__techne__techne_conflict_emit",
        "mcp__techne__techne_check_evidence",
        "mcp__techne__techne_inbox",
        "mcp__techne__techne_coord_threads",
        "mcp__techne__techne_threads",
    }
)


# Per-origin counter shape (techne#995). Counted per injected ROW, not per
# injection: one injection mixes rows from several rails. `unobserved` pairs sit in
# ledger-blind sessions and are excluded from the rate for the same reason as the
# per-source `unobserved_session` count.
_EMPTY_ORIGIN = {
    "pairs": 0,
    "unobserved": 0,
    "cited_in_window": 0,
    "cited_in_session": 0,
}

# Per-source counter shape, shared by the live path and the empty-db path so the
# two can't drift.
_EMPTY_SRC = {
    "injections": 0,
    "cited_in_window": 0,
    "cited_in_session": 0,
    "no_actions_in_window": 0,
    "unobserved_session": 0,
}


def _int_ids(raw: str | None) -> list[int]:
    """Knowledge ids from a stored hit_ids JSON array, ignoring non-numeric ones.

    codex_edit_rail writes symbol-id STRINGS into the same column, so a row from
    an unexpected source degrades to "no citable ids" instead of raising.
    """
    try:
        vals = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    out = []
    for v in vals:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, str) and v.isdigit():
            out.append(int(v))
    return out


def recall_attribution(
    db_path: Path,
    *,
    since_days: int | None = None,
    window_minutes: float = 10.0,
    next_actions: int = 10,
    sources: tuple[str, ...] = _KNOWLEDGE_RAILS,
) -> dict:
    """Citation-attribution for the knowledge rails (techne#329's deferred join).

    For each injection from ``sources``, look at the SAME session's observations
    that follow it and ask whether the agent went on to CITE an injected item by
    id (any form ``_CITATION_RE`` matches: "techne#237", "knowledge#956",
    "🧠2862", "knowledge #2862"), at two window widths:

      - **window** — within the next ``next_actions`` actions AND
        ``window_minutes`` minutes. Tight, so a citation plausibly follows FROM
        the injection rather than merely co-occurring with it in a long session.
      - **session** — anywhere later in the session. A deliberate CEILING: it
        over-credits (the agent may have reached the same id via its own recall
        call, or been shown it again by a later injection), and is reported only
        to bound how much the tight window could be missing.

    Both are a FLOOR on real use, for the reason in the module docstring: an
    explicit id is strong evidence of use, but its absence is weak evidence of
    non-use — most use of a recalled fact never names it. Do not read
    ``cited_pct`` as "the share of injections that mattered"; read a LOW number as
    "we cannot see whether these mattered", which is the honest state.

    ``_ECHO_TOOLS`` observations cannot earn credit (the store quoting its own
    output back at us), though they still consume a window slot.

    Also returns a per-item table (``items``) — encounters vs citations for each
    injected knowledge id — which is where the actionable signal lives: an item
    with many encounters and zero citations is either load-bearing-and-silent or
    pure token cost, and only the operator can tell which.
    """
    where, params = _window(since_days)
    src_ph = ",".join("?" * len(sources))
    clause = (where + " AND " if where else "WHERE ") + f"source IN ({src_ph})"
    conn = plain_connect(db_path)
    try:
        if not _table_exists(conn, "context_injections"):
            return _empty_recall_attribution(window_minutes, next_actions, sources)
        injs = conn.execute(
            f"""SELECT source, session_id, timestamp, hit_ids
                FROM context_injections
                {clause}
                ORDER BY session_id, timestamp""",
            [*params, *sources],
        ).fetchall()
        # Observations for the injected-into sessions only. Keep (dt, cited_ids)
        # — never the raw content — so a 24k-row ledger stays cheap to hold.
        sessions = {r["session_id"] for r in injs if r["session_id"]}
        acts: dict[str, list[tuple[datetime, frozenset[int]]]] = {s: [] for s in sessions}
        if sessions:
            placeholders = ",".join("?" * len(sessions))
            for r in conn.execute(
                f"""SELECT session_id, timestamp, tool_name, content FROM observations
                    WHERE session_id IN ({placeholders})""",
                list(sessions),
            ):
                dt = _parse_ts(r["timestamp"])
                if dt is None:
                    continue
                if r["tool_name"] in _ECHO_TOOLS:
                    cited: frozenset[int] = frozenset()
                else:
                    cited = frozenset(int(m) for m in _CITATION_RE.findall(r["content"] or ""))
                acts[r["session_id"]].append((dt, cited))
        origin_of: dict[int, str] = {}
        if _table_exists(conn, "knowledge"):
            origin_of = {
                r["id"]: knowledge_origin(r["source"])
                for r in conn.execute("SELECT id, source FROM knowledge")
            }
    finally:
        conn.close()

    for rows in acts.values():
        rows.sort(key=lambda a: a[0])

    total = win_hit = sess_hit = empty_window = unobserved = 0
    per_source: dict[str, dict] = {s: dict(_EMPTY_SRC) for s in sources}
    per_origin: dict[str, dict] = {o: dict(_EMPTY_ORIGIN) for o in _ORIGINS}
    # item id -> counters
    items: dict[int, dict] = {}

    for inj in injs:
        total += 1
        src = per_source.setdefault(inj["source"], dict(_EMPTY_SRC))
        src["injections"] += 1

        ids = _int_ids(inj["hit_ids"])
        inj_dt = _parse_ts(inj["timestamp"])
        following = acts.get(inj["session_id"], [])

        # LEDGER-BLIND vs IGNORED — the distinction the librarian row in
        # read_write_ledger exists to make, and it dominates here: on the live
        # corpus 278 of 353 session_start-injected sessions have NO observations
        # at all (session opened, nothing captured; cf. techne#16's remote/ssh
        # capture blindness). Counting those as "injected and never used" would
        # report a measurement hole as agent behaviour, so they are excluded from
        # the attributable denominator rather than scored as a miss.
        if not following:
            unobserved += 1
            src["unobserved_session"] += 1

        later: list[tuple[datetime, frozenset[int]]] = []
        windowed: list[frozenset[int]] = []
        if inj_dt is not None:
            later = [(dt, cited) for dt, cited in following if dt > inj_dt]
            for dt, cited in later:
                if (dt - inj_dt).total_seconds() > window_minutes * 60:
                    break
                windowed.append(cited)
                if len(windowed) >= next_actions:
                    break

        cited_window = frozenset().union(*windowed) if windowed else frozenset()
        cited_session = frozenset().union(*[c for _, c in later]) if later else frozenset()

        idset = set(ids)
        hit_w = bool(idset & cited_window)
        hit_s = bool(idset & cited_session)
        if hit_w:
            win_hit += 1
            src["cited_in_window"] += 1
        if hit_s:
            sess_hit += 1
            src["cited_in_session"] += 1
        if following and not windowed:
            # Session IS in the ledger, but nothing landed inside the window —
            # a real miss, unlike the unobserved case counted above.
            empty_window += 1
            src["no_actions_in_window"] += 1

        for i in idset:
            o = per_origin[origin_of.get(i, "unknown")]
            o["pairs"] += 1
            o["unobserved"] += not following
            o["cited_in_window"] += i in cited_window
            o["cited_in_session"] += i in cited_session
            it = items.setdefault(
                i,
                {
                    "id": i,
                    "encounters": 0,
                    "sessions": set(),
                    "cited_in_window": 0,
                    "cited_in_session": 0,
                },
            )
            it["encounters"] += 1
            if inj["session_id"]:
                it["sessions"].add(inj["session_id"])
            if i in cited_window:
                it["cited_in_window"] += 1
            if i in cited_session:
                it["cited_in_session"] += 1

    def pct(n: int) -> float | None:
        return round(100 * n / total, 1) if total else None

    # Attributable denominator = injections we could actually have SEEN a citation
    # for: drop both the ledger-blind sessions (no observations at all) and the
    # ones where nothing landed in the window. Mirrors edit_rail_attribution's
    # split of "nothing followed" from "things followed but missed the item",
    # with the extra ledger-blind term this rail needs.
    attributable = total - empty_window - unobserved

    def apct(n: int) -> float | None:
        return round(100 * n / attributable, 1) if attributable else None

    item_rows = sorted(
        (
            {
                "id": v["id"],
                "encounters": v["encounters"],
                "sessions": len(v["sessions"]),
                "cited_in_window": v["cited_in_window"],
                "cited_in_session": v["cited_in_session"],
                "origin": origin_of.get(v["id"], "unknown"),
            }
            for v in items.values()
        ),
        key=lambda r: (-r["encounters"], r["id"]),
    )
    never_cited = sum(1 for r in item_rows if r["cited_in_session"] == 0)
    for name, o in per_origin.items():
        rows_o = [r for r in item_rows if r["origin"] == name]
        observed = o["pairs"] - o["unobserved"]
        o["cited_in_window_pct"] = (
            round(100 * o["cited_in_window"] / observed, 1) if observed else None
        )
        o["items"] = len(rows_o)
        o["items_never_cited"] = sum(1 for r in rows_o if r["cited_in_session"] == 0)

    return {
        "sources": list(sources),
        "window_minutes": window_minutes,
        "next_actions": next_actions,
        "injections": total,
        "cited_in_window": win_hit,
        "cited_in_window_pct": pct(win_hit),
        "cited_in_window_pct_attributable": apct(win_hit),
        "cited_in_session": sess_hit,
        "cited_in_session_pct": pct(sess_hit),
        "attributable": attributable,
        "no_actions_in_window": empty_window,
        "unobserved_sessions": unobserved,
        "per_source": per_source,
        "per_origin": per_origin,
        "items": item_rows,
        "items_injected": len(item_rows),
        "items_never_cited": never_cited,
        "items_never_cited_pct": (
            round(100 * never_cited / len(item_rows), 1) if item_rows else None
        ),
    }


def _empty_recall_attribution(
    window_minutes: float, next_actions: int, sources: tuple[str, ...]
) -> dict:
    return {
        "sources": list(sources),
        "window_minutes": window_minutes,
        "next_actions": next_actions,
        "injections": 0,
        "cited_in_window": 0,
        "cited_in_window_pct": None,
        "cited_in_window_pct_attributable": None,
        "cited_in_session": 0,
        "cited_in_session_pct": None,
        "attributable": 0,
        "no_actions_in_window": 0,
        "unobserved_sessions": 0,
        "per_source": {s: dict(_EMPTY_SRC) for s in sources},
        "per_origin": {
            o: {**_EMPTY_ORIGIN, "cited_in_window_pct": None, "items": 0, "items_never_cited": 0}
            for o in _ORIGINS
        },
        "items": [],
        "items_injected": 0,
        "items_never_cited": 0,
        "items_never_cited_pct": None,
    }
