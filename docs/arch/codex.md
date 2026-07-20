---
card: arch-card:techne-codex
updated: 2026-07-20
anchor: techne/core/codex.py sha=4f27c264078f870d3748d8a85c7f98c74f10e3a8
---
# codex — code-graph store (EXPERIMENTAL)

STATUS: experimental, P1 only; tree-sitter+graphqlite deferred until the Codex persona is scoped (docs/roadmap.md).
SEAM: persona-agnostic code-graph substrate, db-per-repo (codex/<slug>-<hash>.db, PRAGMA user_version, CODEX_VERSION=2). files→symbols→call/import/def-use edges stored RELATIONALLY on SQLite — NOT a graph DB (keeps the single-file/portable substrate). Reachability/blast-radius = hand-rolled recursive CTEs, not a graph engine. graphqlite (Cypher + PageRank/Louvain UDF) = possible future techne[codex] upgrade, NOT built.
PARSING: tree-sitter SourceAdapter (Python+TypeScript) lives SEPARATELY in techne/codex/source.py, behind the `techne[codex]` extra (tree-sitter, -python, -typescript, watchdog). LSP-assist: techne/codex/lsp_resolve.py + core/lsp_client.py.
OPS: schema symbols(id,file,name,kind,…)+edges; codex_meta KV (v2) persists repo_root; migrations via _apply_codex_migration() before store.migrate().
API: `techne codex index <repo>`; MCP codex_search, codex_blast_radius.
CONSUMERS: none cross-repo yet (experimental). CI installs the `codex` extra so extraction tests run, not skip.
