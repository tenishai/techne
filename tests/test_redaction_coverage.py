"""Every free-text SQL write goes through ``redact_credentials`` (issue #1238).

Redaction is applied by each caller, as a convention, and the convention has
been missed three times (observations, the thread-summary outbox payload,
``context_injections.trigger_snippet``). This test enforces it statically:

1. Parse every ``.py`` under ``techne/`` with ``ast`` and collect string
   literals (and f-strings) holding ``INSERT INTO t (cols) VALUES (...)`` or
   ``UPDATE t SET col = ...``.
2. For each column that takes a bound value (``?``, ``:name``, ``{}``), look up
   its declared type in the real schema (``init_db`` plus the self-creating
   ``context_injections`` table). TEXT columns are the candidates.
3. A candidate passes if (a) the enclosing function, or one enclosing it,
   calls ``redact_credentials`` / ``redact_json_values`` or a helper in the
   same file that does; or (b) the column is SAFE (ids, hashes, enums, timestamps,
   paths: ``SAFE_COLUMNS`` / ``SAFE_NAME_RULES``, each with a reason); or
   (c) the write site is on ``SITE_ALLOW`` (data redacted upstream, upstream
   named; or a store redaction.py exempts by design).

Limits, stated plainly:

* The unit of proof is the enclosing function, not the bound value. A function
  that redacts ``content`` and also writes an unredacted ``summary`` passes
  on the ``summary`` write unless that column is flagged by hand. The test
  catches a write site that forgot redaction entirely (all three past misses
  were that shape); it does not trace data flow.
* Only SQL held in string literals is seen. SQL assembled at runtime (table or
  column names substituted, ``INSERT INTO`` built from a variable) is not
  analysed; ``DYNAMIC_SITES`` lists every such site found today, with its
  reason, and the test fails if a new one appears so a human looks at it.
* Writes by trigger (FTS shadow tables), by ``executescript`` DDL, and by code
  outside ``techne/`` are out of scope. Non-SQL sinks (files, HTTP) are too.
* Column types come from the schema built by ``init_db``; tables that live in
  other databases (architect, codex, librarian) are covered by ``EXEMPT_TABLES``.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path


from techne.core.database import init_db

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "techne"

#: The functions in techne/core/redaction.py that redact.
REDACT_CALLS = {"redact_credentials", "redact_json_values"}

# --------------------------------------------------------------------------
# Policy: what is allowed not to be redacted, and why.
# --------------------------------------------------------------------------

#: Stores redaction.py exempts by design (operator-curated or extracted from
#: source the operator controls) or that live outside techne.db.
EXEMPT_TABLES: dict[str, str] = {
    "patterns": "architect store: operator-curated notes (redaction.py docstring)",
    "investigations": "architect store: operator-curated notes (redaction.py docstring)",
    "pattern_applies": "architect store: operator-curated notes (redaction.py docstring)",
    "pattern_relations": "architect store: operator-curated notes (redaction.py docstring)",
    "pattern_outcomes": "architect store: operator-curated notes (redaction.py docstring)",
    "symbols": "codex store: repo symbols from operator-controlled source (redaction.py docstring)",
    "edges": "codex store: repo symbols from operator-controlled source (redaction.py docstring)",
    "edges_v1": "codex store: repo symbols from operator-controlled source (redaction.py docstring)",
    "codex_meta": "codex store: index bookkeeping (hashes, versions, paths)",
}

#: (table, column) -> reason it is not free text. Short structured fields are
#: outside the redaction threat model (redaction.py docstring).
SAFE_COLUMNS: dict[tuple[str, str], str] = {
    ("observations", "tool_name"): "tool identifier",
    ("observations", "tier"): "enum",
    ("observations", "kind"): "enum",
    ("observations", "tags"): "short caller-supplied tags (redaction.py: structured field)",
    ("knowledge", "type"): "enum",
    ("knowledge", "source"): "short provenance label",
    ("knowledge", "tags"): "short caller-supplied tags (redaction.py: structured field)",
    ("knowledge", "confidence"): "enum",
    ("knowledge", "anchor_path"): "file path",
    ("knowledge", "anchor_symbol"): "symbol name",
    ("knowledge", "anchor_git_sha"): "git sha",
    ("knowledge", "status"): "enum",
    ("knowledge", "origin_source"): "short provenance label",
    ("knowledge", "origin_session"): "session id",
    ("knowledge_candidates", "scope"): "enum",
    ("knowledge_candidates", "type"): "enum",
    ("knowledge_candidates", "tags"): "short caller-supplied tags (redaction.py: structured field)",
    ("knowledge_candidates", "confidence"): "enum",
    ("knowledge_candidates", "model_used"): "model name",
    ("knowledge_candidates", "status"): "enum",
    ("knowledge_candidates", "target_ids"): "JSON list of ids",
    ("knowledge_candidates", "origin"): "enum",
    ("knowledge_candidates", "decided_by"): "actor name",
    ("knowledge_deps", "kind"): "enum",
    ("lesson_meta", "agent_id"): "agent name",
    ("lesson_meta", "tier"): "enum",
    ("lesson_meta", "enforced_by"): "name of an enforcing check",
    ("sessions", "project_path"): "file path",
    ("sessions", "agent_identity"): "agent name",
    ("sessions", "platform"): "enum",
    ("sessions", "summary_llm_source"): "enum (which model wrote the summary)",
    ("threads", "status"): "enum",
    ("checkpoints", "trigger"): "enum",
    ("coord_messages", "from_name"): "session display name (short, caller-supplied)",
    ("coord_messages", "recipient"): "address (session id or alias)",
    ("outbox", "target"): "sink name",
    ("outbox", "status"): "enum",
    ("outbox", "error_message"): "our own sink error text, not captured content",
    ("reminders", "status"): "enum",
    ("curated_memory", "source_path"): "file path",
    ("curated_memory", "slug"): "file slug",
    ("curated_memory", "provenance"): "enum",
    ("curated_memory", "meta"): "JSON of file stats",
    ("documents", "source_type"): "enum",
    ("documents", "source_url"): "URL / path of the source",
    ("documents", "meta"): "JSON of source stats",
    ("documents", "owned_by"): "owner name",
    ("reflection_queue", "parked_reason"): "enum written as a literal",
    ("thread_boundaries", "family_a"): "enum",
    ("thread_boundaries", "family_b"): "enum",
    ("thread_boundaries", "signals_json"): "JSON of numeric scores",
    ("context_injections", "source"): "enum (hook name)",
    ("context_injections", "hit_ids"): "JSON list of ids",
    ("context_injections", "scores"): "JSON list of numbers",
    ("context_injections", "native_scores"): "JSON list of numbers",
    ("context_injections", "score_kinds"): "JSON list of enums",
    ("context_injections", "match_sources"): "JSON list of enums",
    ("extension_state", "name"): "extension name",
    ("extension_state", "payload_json"): "extension bookkeeping (cursors, counters)",
    ("identity", "key"): "settings key",
    ("identity", "value"): "operator identity setting (name, handle), set by the operator",
    ("identity", "source"): "enum",
    ("coord_reads", "reader_session"): "session id",
    ("coord_messages", "from_session"): "session id",
    ("lesson_meta", "evidence_session"): "session id",
    ("knowledge", "written_by"): "instance name",
    ("knowledge_candidates", "action"): "enum",
    ("observations", "source_uuid"): "transcript line uuid",
    ("sessions", "ended_at_source"): "enum",
}

#: Column-name rules for the long tail. (regex, reason)
SAFE_NAME_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^id$|_id$|_ids$"), "identifier"),
    (re.compile(r"_at$|^timestamp$"), "timestamp"),
    (re.compile(r"_hash$"), "hash"),
]

#: (file, table, column) -> reason, for write sites whose enclosing function
#: does not redact. Name the upstream site that does.
SITE_ALLOW: dict[tuple[str, str, str], str] = {
    (
        "techne/core/documents.py",
        "documents",
        "content",
    ): "library.index_document redacts title and content before calling store_document",
    (
        "techne/core/documents.py",
        "documents",
        "title",
    ): "library.index_document redacts title and content before calling store_document",
    (
        "techne/core/knowledge.py",
        "lesson_meta",
        "rule",
    ): "_carry_lesson_meta copies lesson_meta from the previous revision (INSERT ... SELECT); that row was redacted when first written",
}

#: Write sites whose SQL is assembled at runtime, so the scan cannot see which
#: columns they write. (file, start of the SQL text) -> why that is acceptable.
DYNAMIC_SITES: dict[tuple[str, str], str] = {
    ("techne/core/database.py", "UPDATE {} SET {} = strftime"): "migration: re-formats timestamp columns",
    ("techne/core/librarian.py", "INSERT OR IGNORE INTO documents ({})"): "one-time copy of documents rows that were redacted when first stored (migrate_documents_to_librarian)",
    ("techne/core/migrator.py", "INSERT INTO {}({}) SELECT"): "FTS rebuild from an existing table: copies stored text, adds none",
    ("techne/core/patterns.py", "UPDATE patterns SET {}"): "architect store (exempt); columns come from a fixed allowlist of enums and ids",
    ("techne/core/scrub.py", "UPDATE {} SET {} = ?"): "the scrub pass itself: writes redact_credentials output",
    ("techne/core/search.py", "INSERT INTO {}(rowid, embedding)"): "vector rows (rowid, blob): no text",
    ("techne/core/state.py", "UPDATE sessions SET {}"): "session end/summary writers: the summary value goes through redact_credentials in the same function",
    ("techne/platforms/claude_code/usage.py", "UPDATE sessions SET {}"): "numeric token-usage columns",
}


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------

_INSERT = re.compile(
    r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)\s*(VALUES\s*\(.*|SELECT\b.*)?",
    re.I | re.S,
)
_UPDATE = re.compile(r"\bUPDATE\s+(?:OR\s+\w+\s+)?(\w+)\s+SET\s+(.*?)(?:\bWHERE\b|$)", re.I | re.S)
_BOUND = re.compile(r"\?|:\w+|\{")


def _split_top(text: str) -> list[str]:
    """Split on commas that are not inside parentheses or quotes."""
    parts, cur, depth, quote = [], "", 0, ""
    for ch in text:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0 and not quote:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts]


def _values_items(tail: str | None, ncols: int) -> list[str]:
    """The VALUES expressions, or a bound placeholder per column if unparseable."""
    if tail and tail.upper().startswith("VALUES"):
        start = tail.index("(")
        depth = 0
        for i in range(start, len(tail)):
            depth += (tail[i] == "(") - (tail[i] == ")")
            if depth == 0:
                items = _split_top(tail[start + 1 : i])
                if len(items) == ncols:
                    return items
                break
    return ["?"] * ncols  # INSERT ... SELECT or odd shape: assume bound


class Site:
    def __init__(self, file: str, line: int, table: str, columns: list[str], redacts: bool, kind: str):
        self.file, self.line, self.table = file, line, table
        self.columns, self.redacts, self.kind = columns, redacts, kind

    def __repr__(self) -> str:
        return f"{self.file}:{self.line} {self.kind} {self.table}({', '.join(self.columns)})"


def _literals(tree: ast.AST):
    """Yield (node, text) for string constants and f-strings (``{}`` per hole)."""
    inside_fstring: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for v in n.values:
                inside_fstring.add(id(v))
    for n in ast.walk(tree):
        if id(n) in inside_fstring:
            continue
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n, n.value
        elif isinstance(n, ast.JoinedStr):
            yield n, "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)


def _call_names(fn: ast.AST) -> set[str]:
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            names.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
    return names


def _redactor_names(tree: ast.AST) -> set[str]:
    """``redact_credentials`` plus helpers in the SAME file that call it directly.

    One level, one file. A closure over bare function names across the package
    is useless: ``connect``, ``add`` and ``store`` collide between modules and
    pull in everything.
    """
    found = set(REDACT_CALLS)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _call_names(n) & REDACT_CALLS:
            found.add(n.name)
    return found


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}


def scan() -> tuple[list[Site], list[tuple[str, int, str]]]:
    """Return (static write sites, dynamic-SQL sites as (file, line, sql))."""
    sites: list[Site] = []
    dynamic: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text())
        parents = _parents(tree)
        redactors = _redactor_names(tree)
        for node, text in _literals(tree):
            hits = []
            for m in _INSERT.finditer(text):
                cols = [c.strip() for c in m.group(2).split(",") if c.strip()]
                if not all(re.fullmatch(r"\w+", c) for c in cols):
                    hits.append(("dyn", None, None))
                    continue
                items = _values_items(m.group(3), len(cols))
                bound = [c for c, v in zip(cols, items) if _BOUND.search(v)]
                hits.append(("INSERT", m.group(1), bound))
            for m in _UPDATE.finditer(text):
                bound = []
                dyn = False
                for assign in _split_top(m.group(2)):
                    mm = re.match(r"(\w+)\s*=\s*(.*)", assign, re.S)
                    if not mm:
                        dyn = dyn or "{" in assign
                        continue
                    if _BOUND.search(mm.group(2)):
                        bound.append(mm.group(1))
                if dyn or m.group(1) == "{":
                    hits.append(("dyn", None, None))
                else:
                    hits.append(("UPDATE", m.group(1), bound))
            if not hits and re.search(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s*\{", text, re.I):
                hits.append(("dyn", None, None))
            if not hits and re.search(r"\bUPDATE\s+\{", text):
                hits.append(("dyn", None, None))
            # enclosing functions
            chain, p = [], parents.get(id(node))
            while p is not None:
                if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    chain.append(p)
                p = parents.get(id(p))
            redacts = any(_call_names(f) & redactors for f in chain)
            for kind, table, cols in hits:
                if kind == "dyn":
                    dynamic.append((rel, node.lineno, re.sub(r"\s+", " ", text)[:80]))
                elif cols:
                    sites.append(Site(rel, node.lineno, table, cols, redacts, kind))
    return sites, dynamic


def _text_columns() -> dict[str, set[str]]:
    """TEXT-typed columns per table, from the real schemas."""
    import tempfile

    from techne.hooks import _injection_log

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        init_db(db).close()
        conn = sqlite3.connect(db)
        conn.executescript(_injection_log._DDL)
        out: dict[str, set[str]] = {}
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            for _cid, name, ctype, *_ in conn.execute(f"PRAGMA table_info({t})"):
                if (ctype or "").upper().startswith("TEXT"):
                    out.setdefault(t, set()).add(name)
        conn.close()
    return out


def _safe(table: str, col: str) -> str | None:
    if (table, col) in SAFE_COLUMNS:
        return SAFE_COLUMNS[(table, col)]
    for rx, why in SAFE_NAME_RULES:
        if rx.search(col):
            return why
    return None


def unredacted_free_text() -> list[str]:
    sites, _ = scan()
    text_cols = _text_columns()
    bad = []
    for s in sites:
        if s.table in EXEMPT_TABLES or s.redacts:
            continue
        for c in s.columns:
            if c not in text_cols.get(s.table, set()):
                continue  # not a TEXT column (or not in schema)
            if _safe(s.table, c) or (s.file, s.table, c) in SITE_ALLOW:
                continue
            bad.append(f"{s.file}:{s.line} {s.kind} {s.table}.{c}")
    return bad


def test_every_free_text_write_is_redacted_or_allowlisted():
    bad = unredacted_free_text()
    assert not bad, (
        "SQL writes a free-text column without redact_credentials in the writing "
        "function. Redact the value, or add the column to SAFE_COLUMNS / the site to "
        "SITE_ALLOW with a reason (issue #1238):\n  " + "\n  ".join(bad)
    )


def test_dynamic_sql_sites_are_known():
    _, dynamic = scan()
    seen = set()
    unknown = []
    for file, line, sql in dynamic:
        key = next((k for k in DYNAMIC_SITES if k[0] == file and sql.startswith(k[1])), None)
        if key is None:
            unknown.append(f"{file}:{line} {sql}")
        else:
            seen.add(key)
    assert not unknown, (
        "SQL built at runtime is invisible to the scan. Check what it writes, then "
        "list it in DYNAMIC_SITES with a reason:\n  " + "\n  ".join(unknown)
    )
    assert seen == set(DYNAMIC_SITES), f"stale DYNAMIC_SITES entries: {set(DYNAMIC_SITES) - seen}"


def test_site_allowlist_has_no_stale_entries():
    sites, _ = scan()
    live = {(s.file, s.table, c) for s in sites for c in s.columns if not s.redacts}
    assert set(SITE_ALLOW) <= live, f"SITE_ALLOW entries no longer needed: {set(SITE_ALLOW) - live}"


def test_scan_is_not_vacuous():
    """A broken scanner finds nothing and passes; pin that it sees the known writers."""
    sites, _ = scan()
    assert len(sites) >= 80
    seen = {(s.file, s.table, c) for s in sites for c in s.columns}
    for want in [
        ("techne/core/state.py", "observations", "content"),
        ("techne/core/coord.py", "coord_messages", "body"),
        ("techne/hooks/_injection_log.py", "context_injections", "trigger_snippet"),
        ("techne/sync/outbox.py", "outbox", "data_json"),
        ("techne/core/thread_orchestrator.py", "threads", "summary"),
    ]:
        assert want in seen, want
    text_cols = _text_columns()
    assert "content" in text_cols["observations"] and "trigger_snippet" in text_cols["context_injections"]


def test_outbox_payload_is_redacted_at_enqueue(tmp_path):
    """Regression for the second #1238 miss: thread-summary payloads went in raw."""
    import json

    from techne.sync.outbox import enqueue

    db = tmp_path / "t.db"
    init_db(db).close()
    secret = "sk-ant-api03-" + "a" * 30
    enqueue(
        db,
        "sync:test",
        {"thread_id": "t-1", "summary": f"used {secret}", "resume_points": [f"key={secret}"], "n": 3},
        deliver=False,
    )
    conn = sqlite3.connect(db)
    (blob,) = conn.execute("SELECT data_json FROM outbox").fetchone()
    conn.close()
    assert secret not in blob
    payload = json.loads(blob)
    assert "<redacted:sk-ant>" in payload["summary"]
    assert payload["n"] == 3 and payload["thread_id"] == "t-1"


if __name__ == "__main__":
    st, dyn = scan()
    print(len(st), "sites;", len(dyn), "dynamic")
    for b in unredacted_free_text():
        print("BAD", b)
