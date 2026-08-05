---
card: repo-card:techne
updated: 2026-08-03
anchor: pyproject.toml sha=804e472ae2d47848557e139a96b732e215fd054d
---
# techne — repo card

WHAT: local-first, single-SQLite-file persistent memory for AI coding agents (mainly Claude Code). Captures sessions/threads/observations, curates durable knowledge, self-maintains (reflect/consolidate/dream) on the agent's OWN model — no API key.
STATE: main, v0.12.0, Alpha, active (evidence gate + retrieval-correctness eval gates landed; coord mailbox live).
STACK: Python 3.11+ (CI 3.12/3.13 matrix + a separate 3.14 `eval-vec` arm), Click CLI, stdlib sqlite3 (WAL) = sole store. Optional extras: sqlite-vec+fastembed/model2vec (vec search), tree-sitter (codegraph), litellm (headless). pip/pipx pkg `techne` + stdio MCP server. Dockerfile present.
ENTRY:
- CLI techne → techne/cli.py:main (init/status/threads/search/reflect/consolidate/dream/architect/codex/library/coord…)
- MCP → techne/mcp_server.py (stdio JSON-RPC, 33 tools; `techne init --platform claude-code`)
- hooks → techne/hooks/*.py (session_start/end, pre/post_tool_use, pre/post_compact, user_prompt_submit)

SEAMS → card:
- domain-stores  techne/core/stores.py       (StoreAdapter registry, SQLite-only)      → docs/arch/domain-stores.md
- sync-adapters  techne/sync/adapter.py       (team egress + external ingest)           → docs/arch/sync-adapters.md
- codex          techne/core/codex.py         (code-graph, blast-radius; EXPERIMENTAL)  → docs/arch/codex.md
- coord          techne/core/coord.py         (inter-session mailbox)                   → docs/arch/coord.md
- egress         techne/core/egress.py        (Tier-0 SessionStart injection)           → docs/arch/egress.md
- reflect        techne/core/reflect.py + sleepdream.py (self-maintenance)              → docs/arch/reflect-pipeline.md
- search/embed   techne/core/embeddings.py + search.py (hybrid FTS+vector; model2vec on py3.14) → docs/arch/search-embeddings.md
- liveness       techne/core/checks.py        (did each rail actually RUN?)             → docs/arch/liveness.md
- (uncarded) mcp_server.py (33-tool surface); core/federation.py (fan-out recall over local+remote); core/evidence.py (claim-vs-ledger check behind techne_check_evidence)

DEPS: imported/hosted by siblings cabal (orchestrator) + limenal (MCP gateway; limenal.handlers.memory imports techne.VALID_TYPES). techne itself has NO cross-repo runtime deps (local-first).
HEALTH: green. 109 test files (`tests/**/test_*.py`); CI pytest on 3.12/3.13 every push/PR, run PARALLEL under pytest-xdist, worker count PINNED at `-n 4` not `auto` (#463 — the suite is syscall-heavy and was 97.5% of every job; note that is kernel CPU, not I/O wait, so the ceiling is core count: ~2x on the 2-core runner vs ~4x on a 6-core laptop. `ci-local.sh` uses `-n auto` on purpose — worker count provably does not change outcomes) (ruff runs too but is ADVISORY — `|| true`, so lint never fails the build), plus the deterministic eval gates (#68 recall, #385 retrieval-correctness) and the 3.14 `eval-vec` arm (#399), both left SERIAL on purpose (each selects a handful of tests; eval-vec also uses `-s`, which xdist does not support), and the `arch-cards` freshness gate (#383). CHANGELOG carries an [Unreleased] section beyond 0.12.0.
TESTING GOTCHA — two suites run NOWHERE: the default run excludes `eval`/`eval_gate`/`eval_vec` (all three ARE run by dedicated CI jobs) but also `probe` and `eval_llm`, which NO workflow invokes — and both would additionally skip on a missing CLI/model even if invoked. So the #94 memory-attribution probe and the #118 LLM-judged eval have never executed in CI. Do not read a green build as covering them.
CI IS NOT ENFORCING — this repo's plan has no branch protection (`gh api repos/tenishai/techne/branches/main/protection` → 403), so no job is a required status check. CI reports; nothing blocks a merge.
GOTCHAS: Codex domain EXPERIMENTAL (tree-sitter+graphqlite deferred). Postgres StoreAdapter portability RETIRED (techne#135) → SQLite-only by decision.
RELATED DOCS (reconcile, don't dupe): docs/modules.md, memory-system-architecture.md, context-architecture.md, db-per-domain.md. Cards = seam-level, freshness-gated layer above these.
