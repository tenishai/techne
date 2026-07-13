"""LSP edge resolution for codex — the resolver pass the schema was built for (techne#321).

codex's tree-sitter indexer records every call edge with `to_symbol = NULL` /
`resolution_status = 'unresolved'`: it knows the callee *name* but not which symbol
that name binds to, and name-matching conflates same-named symbols across modules
(the bug the epic #319 documents). This pass fills `to_symbol` *semantically*: for
each function/method symbol it asks a language server for that symbol's OUTGOING
calls — each carrying the callee's real definition location — maps each back to a
codex symbol id, and UPDATEs the matching edge to `resolution_status = 'lsp'`.

Graceful and additive: it resolves only what the LSP can, and leaves everything else
untouched (queries fall back to name-matching, exactly as before — strictly an
improvement, never a regression). Python/pyright is wired here; Go/Rust ride the same
`LspClient` (#324). Perf note: this is a per-symbol batch pass — correctness first;
incremental/batched resolution is a follow-up.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from techne.core.lsp_client import LspClient
from techne.core.stores import plain_connect

_SERVERS = {"python": ["pyright-langserver", "--stdio"]}
_CALLABLE_KINDS = ("function", "method")


def default_server(language: str = "python") -> list[str] | None:
    """The default language-server command for *language*, if it's on PATH — else None."""
    cmd = _SERVERS.get(language)
    return cmd if (cmd and shutil.which(cmd[0])) else None


def _span_start(span: str | None) -> int | None:
    """Parse codex's `"start:end"` span → 1-based start line."""
    try:
        return int(str(span).split(":")[0])
    except (ValueError, AttributeError):
        return None


def _name_position(repo_root: Path, file: str, start_line_1based: int, name: str):
    """0-based (line, character) of *name* on its definition line, or None if not found.
    codex stores no character offset, so we recover it from the source line."""
    try:
        src = (repo_root / file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    idx = start_line_1based - 1
    if 0 <= idx < len(src):
        col = src[idx].find(name)
        if col >= 0:
            return idx, col
    return None


def _map_symbol(conn, file: str, name: str, lsp_line0: int) -> str | None:
    """Map an LSP callee (repo-relative *file*, *name*, 0-based def *line*) to a codex
    symbol id. Match file+name, then prefer the symbol whose 1-based span start equals
    the LSP line; fall back to the sole match, else the nearest span start."""
    rows = conn.execute("SELECT id, span FROM symbols WHERE file = ? AND name = ?", (file, name)).fetchall()
    if not rows:
        return None
    target = lsp_line0 + 1  # codex spans are 1-based
    exact = [r["id"] for r in rows if _span_start(r["span"]) == target]
    if exact:
        return exact[0]
    if len(rows) == 1:
        return rows[0]["id"]
    return min(rows, key=lambda r: abs((_span_start(r["span"]) or 0) - target))["id"]


def resolve_edges_lsp(
    db_path: Path,
    repo_root: Path | str,
    *,
    language: str = "python",
    server_cmd: list[str] | None = None,
    client=None,
    settle=(1, 0.0),
) -> dict:
    """Fill `edges.to_symbol` for call edges the LSP can resolve. Returns a report
    ``{resolved, attempted, symbols, skipped?}``.

    *client* is injectable (tests pass a fake with `.supports`/`.open_file`/
    `.outgoing_calls`); otherwise an `LspClient` is built from *server_cmd* or the
    language default and torn down. No-ops gracefully when no server is available or
    the server lacks call-hierarchy.
    """
    repo_root = Path(repo_root).resolve()
    conn = plain_connect(db_path)
    try:
        syms = conn.execute(
            f"SELECT id, file, name, span FROM symbols WHERE kind IN ({','.join('?' * len(_CALLABLE_KINDS))})",
            _CALLABLE_KINDS,
        ).fetchall()

        own_client = client is None
        if own_client:
            cmd = server_cmd or default_server(language)
            if cmd is None:
                return {"resolved": 0, "attempted": 0, "symbols": len(syms), "skipped": "no language server"}
            client = LspClient(cmd, repo_root).start()
        try:
            if not client.supports("callHierarchyProvider"):
                return {"resolved": 0, "attempted": 0, "symbols": len(syms), "skipped": "no callHierarchy"}

            resolved = attempted = 0
            for s in syms:
                pos = _name_position(repo_root, s["file"], _span_start(s["span"]) or 0, s["name"])
                if pos is None:
                    continue
                line0, char = pos
                abs_path = repo_root / s["file"]
                client.open_file(abs_path, language_id=language)
                for callee in client.outgoing_calls(abs_path, line0, char, settle=settle) or []:
                    try:
                        cfile = str(Path(callee.path).resolve().relative_to(repo_root))
                    except ValueError:
                        continue  # callee outside the repo (stdlib/deps) — not a codex symbol
                    to_id = _map_symbol(conn, cfile, callee.name, callee.line)
                    if to_id is None:
                        continue
                    attempted += 1
                    cur = conn.execute(
                        "UPDATE edges SET to_symbol = ?, resolution_status = 'lsp' "
                        "WHERE from_symbol = ? AND to_name = ? AND kind = 'call'",
                        (to_id, s["id"], callee.name),
                    )
                    resolved += cur.rowcount
            conn.commit()
            return {"resolved": resolved, "attempted": attempted, "symbols": len(syms)}
        finally:
            if own_client:
                client.stop()
    finally:
        conn.close()
