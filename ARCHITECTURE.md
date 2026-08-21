---
card: repo-card:techne
updated: 2026-08-19
anchor: pyproject.toml sha=bd17310592c734da6ae5057fd2eea40d02c9d4ec
---
# techne — repo card

WHAT: local-first, single-SQLite-file persistent memory for AI coding agents (mainly Claude Code). Captures sessions/threads/observations, curates durable knowledge, self-maintains (reflect/consolidate/dream) on the agent's OWN model — no API key.
STATE: main, v0.15.0, Alpha, active (the claims pass — 0.14.0 stopped the instruments lying about whether rails RAN; this one went after what they ASSERT when they do. search() reported its source TABLE as the row's kind; `status='uncertain'` ranked instead of filtering, so we injected what we are on record as not believing; the call graph asserted 19,345 edges it had resolved NONE of, and now marks each one exact vs name-inferred; `guarded_migrate` folded 'already there' together with 'ahead of the code', so architect.db skipped six days of migrations in total silence; fsck named faults it gave you no way to clear. Plus the half that makes any of it reach a user: `techne capabilities` — 8 of 8 off on a fresh install — and the self-care / techne-promote workflows now ship with the release).
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
- config-trust   techne/security/config_trust.py (operator-vs-agent gate over egress-selecting config) → docs/arch/config-trust.md
- utilization    techne/core/utilization.py   (is the READ path earning its tokens?)     → docs/arch/utilization.md
- archivist-query techne/core/archivist_query.py (guarded read-only SQL over team memory)  → docs/arch/archivist-query.md
- (uncarded) mcp_server.py (33-tool surface); core/federation.py (fan-out recall over local+remote); core/evidence.py (claim-vs-ledger check behind techne_check_evidence)

DEPS: imported/hosted by siblings cabal (orchestrator) + liminal (MCP gateway; liminal.handlers.memory imports techne.VALID_TYPES; renamed from `limenal` 2026-08-11). techne itself has NO cross-repo runtime deps (local-first).
HEALTH: green. 130 test files (`tests/**/test_*.py`); CI pytest on 3.12/3.13 every push/PR, run PARALLEL under pytest-xdist, worker count PINNED at `-n 4` not `auto` (#463 — the suite is syscall-heavy and was 97.5% of every job; note that is kernel CPU, not I/O wait, so the ceiling is core count: ~2x on the 2-core runner vs ~4x on a 6-core laptop. `ci-local.sh` uses `-n auto` on purpose — worker count provably does not change outcomes) (ruff runs too but is ADVISORY — `|| true`, so lint never fails the build), plus the deterministic eval gates (#68 recall, #385 retrieval-correctness) and the 3.14 `eval-vec` arm (#399), both left SERIAL on purpose (each selects a handful of tests; eval-vec also uses `-s`, which xdist does not support), the `arch-cards` freshness gate (#383), and the `crash-recovery` fault-injection gate (#388 — `-m crash`, SERIAL and `-p no:randomly`: it spawns subprocesses that SIGKILL themselves mid-write against a shared on-disk store, so a random order that flaked would teach people to re-run CI, which is how a real regression gets waved through). THE `crash` MARKER IS THE COVERED CASE, NOT THE UNCOVERED ONE — it is excluded from the default run like `eval`/`eval_gate`/`eval_vec` AND run by a dedicated job, which is exactly the distinction the DELIBERATELY LOCAL-ONLY block below turns on. CHANGELOG's most recent released section is [0.15.0]; [Unreleased] is empty at the cut.
DELIBERATELY LOCAL-ONLY — THREE THINGS CI DOES NOT COVER, BY DECISION (#453, #537; Richard, 2026-08-07). This is a standing policy, not a backlog item: each needs a toolchain CI is not going to carry, and each would SKIP rather than fail if wired up naively, so the cost of covering them is a green result that means less than it looks.
- `probe` (3 tests, #94 memory-attribution) — spawns a real agent session; needs the `claude` or `opencode` CLI on PATH.
- `eval_llm` (1 test, #118 Stage 2) — needs a reachable model.
  Both are excluded from the default run by `pyproject.toml:addopts` AND invoked by no workflow. Contrast `eval`/`eval_gate`/`eval_vec`, also excluded from the default run but each RUN by a dedicated CI job — exclusion alone does not tell you which case you are in.
- `RUN_LSP_INTEGRATION` (2 tests, in `test_codex_lsp_resolve.py` and `test_lsp_client.py`) — needs `pyright-langserver`, i.e. a Node toolchain for two tests. The env var appears in no workflow and no doc; setting it is a local opt-in. NOTE the files themselves are NOT skipped — they hold 11 tests and the other 9 run normally in CI; it is 2 tests inside them that are gated.
**A green build does not cover any of these.** Run them locally when touching #94 attribution, the LLM-judged eval, or LSP edge resolution. `tests/test_local_only_suites.py` fails if this list and the repo stop agreeing.
CI IS NOT ENFORCING — this repo's plan has no branch protection (`gh api repos/tenishai/techne/branches/main/protection` → 403), so no job is a required status check. CI reports; nothing blocks a merge.
GOTCHAS: Codex domain EXPERIMENTAL (tree-sitter+graphqlite deferred). Postgres StoreAdapter portability RETIRED (techne#135) → SQLite-only by decision.
RELATED DOCS (reconcile, don't dupe): docs/modules.md, memory-system-architecture.md, context-architecture.md, db-per-domain.md. Cards = seam-level, freshness-gated layer above these.
