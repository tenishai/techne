---
card: arch-card:techne-utilization
updated: 2026-08-16
anchor: techne/core/utilization.py sha=402701a2b6d32e1dd0a71075bbb18a54ccdfcb4f
---
# utilization — is the read path earning its tokens? (techne#329)

WHAT: techne writes constantly and reads rarely. This seam MEASURES the read side — what we inject into an agent's context, and whether the agent then did anything that suggests the injection mattered. It exists because #329's audit found we were write-mostly and the auto-recall rail was **unmeasurable**: 306 memory writes to 116 reads, 43 code-graph queries against 3,296 hand edits, and `pattern_search` never called at all. The rule that came out of it — INSTRUMENT READS BEFORE ADDING RETRIEVAL — is why this module exists and why ranking work (#349, #442) is gated behind it.

SURFACE: `techne audit` (`cli.py`, text + `--json`). Four functions, all read-only over `context_injections` + `observations`, all accepting `since_days`:

- `injection_utilization` — per-source volume and token cost. What the rails SPENT.
- `read_write_ledger` — read-vs-write ratios per domain. #329's original audit, re-runnable.
- `edit_rail_attribution` — did a codex edit-rail injection precede an edit to a file/symbol it named?
- `recall_attribution` — did the agent go on to CITE an injected knowledge id (`techne#237` / `knowledge#956`)? This is #416, #329's deferred join, shipped in #560.

## THE THING TO UNDERSTAND BEFORE QUOTING ANY NUMBER FROM HERE

**Every attribution figure is a FLOOR, not a measurement of usefulness.** An explicit citation is strong evidence a fact was used; its absence is WEAK evidence it was not, because most use of a recalled fact never names it. A low `cited_pct` means "we cannot see whether these mattered" — NOT "these did not matter". The docstrings say so; this card says so because a number lifted out of context loses its caveat and someone will delete a rail with it.

Live figures as of 2026-08-16 (all-time): 1,601 injections analysed, 28 cited in the tight window (1.7%), 166 cited anywhere in-session (10.4%, a deliberate CEILING), 746 distinct items injected of which 639 were never cited (85.7%).

## INVARIANTS

- **TWO WINDOWS, AND BOTH ARE DELIBERATE.** `window` = within N following actions AND M minutes, so a citation plausibly follows FROM the injection. `session` = anywhere later, which OVER-credits (the agent may have reached the id via its own recall call, or been re-shown it) and exists only to bound how much the tight window could be missing. Report both or neither; a single number invites the wrong reading.
- **THE DENOMINATOR IS `attributable`, NOT `injections`.** Sessions that took an injection and then produced NO observations cannot be scored either way, and there are a lot of them — `unobserved_sessions` was 309 of 357 sessions' worth at last count. Dividing by all injections reports a capture gap as agent indifference. #562 is the cost side of that same population.
- **ECHO TOOLS CANNOT EARN CREDIT.** An observation that is the store quoting its own output back (a recall result containing the id it just returned) must not count as the agent citing it — it still consumes a window slot, but it scores nothing.
- **THE PER-ITEM TABLE IS WHERE THE ACTION IS.** `items` gives encounters vs citations per injected id. An item with many encounters and zero citations is EITHER load-bearing-and-silent OR pure token cost, and no automated signal can tell those apart — only an operator can.

## KNOWN GAPS

- The LLM-judge half of #416 is deliberately unbuilt. The proxy is free, deterministic and CI-runnable; the judge costs model calls and should be reached for only where the proxy is ambiguous (relevant injection, turn happened not to act on it).
- Nothing here scores the SESSION-START rail's prose blocks — only id-bearing hits.
- `recall_attribution` was invisible to `codex_search` when this card was written (#595), which is part of why it was nearly rebuilt from scratch. If you are reading this card INSTEAD of grepping, that is the intended path (techne#812).
