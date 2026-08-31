# Proposal: extend the evidence gate from deeds to assertions

- **Filed:** 2026-08-31 · by Poe, from a day of Prudence rounds on #852
- **Status:** proposed. **This is not a new proposal** — [#584] made it on
  2026-08-16. What follows is a second body of evidence that sharpens its claim
  taxonomy, falsifies one of its statements, and answers its implicit question
  about what fires the check.
- **Parent:** #584 · **Related:** #517, #828, #848, #851

## What #584 already established

`techne/core/evidence.py` verifies **deeds** — `completion`/`did_work`,
`commit`, `tests_green`, `ran_action`, `wrote_file` — against the observation
ledger, returning `backed` / `unbacked` / `unverifiable`. Its docstring scopes
free-text claim extraction out, deliberately, for that slice.

#584's finding: that leaves **assertions about state** unguarded, and its
evidence was four conversational claims in one session of which **three were
caught by the operator**. Its diagnosis — every failure was a state claim that
was cheaply queryable at the moment it was made — still holds and is not
restated here.

## What 2026-08-31 adds

Five of my own claims were falsified in one day, four by a Prudence review of
#852 and one by re-reading a file an hour after writing it.

| # | the claim | what was actually true | class |
|---|---|---|---|
| 1 | `codex_grep_assist` is off | `~/.techne/config.yaml:23` sets it true; I read the dataclass default | capability |
| 2 | `body[450]` is mid-word, "verified empirically" | `body[449]` was a space, so the guard passed with the mechanism deleted | **self-referential** |
| 3 | the misattribution is fixed | fixed in two of three places; the third contradicted the others 170 lines away | **distributional** |
| 4 | 47% below the cap, 48% arrive whole | 1163/2468 = 47.1; "48" was `100 − 52`, double-truncating a complementary pair | **arithmetic** |
| 5 | ~312 tokens/turn is a ceiling | `floor = int(limit * 0.6)` applies at the old cap too, so the true worst case is 412 | capability |

Claims 1 and 5 are #584's existing classes. **Claims 2, 3 and 4 are not, and
each needs a different check.**

### A sixth, found by checking this note

The first draft of this document asserted "2,640 knowledge rows, 30 evaluation
docs". Both were wrong — 2,643 and 28 — and the second one is not a typo. On
2026-08-31 a sweep script globbed the evaluations directory and printed 28; two
new docs written earlier that day were already in that count, and a comment
posted to #506 afterwards said "the count is now 30 docs", adding them a second
time. The mtimes settle it: both files predate the sweep.

It is the arithmetic class again, in a comment whose subject was measurement
discipline, and it was caught only because this note's claims were checked
before it was committed. That is the argument for the mechanism, made against
the document proposing it — and a demonstration that the failure does not stop
once you are writing about the failure.

### Self-referential (claim 2)

A claim about the artifact currently being written. Not answerable by querying
project state, because the subject does not exist anywhere else yet — only by
*running* the thing. Prudence settled it by deleting boundary-seeking and
watching the guard stay green; I would have re-read my own comment and agreed
with it.

This is the class where reading cannot substitute for executing, which is
already the recorded finding of the 2026-08-29 Fable run: *the intelligence was
entirely in the brief, not the execution* — a brief that forbids reading and
demands execution.

### Distributional (claim 3)

Not "is X true" but "**where else does X live**". A claim can be corrected at
the point of correction and left standing everywhere else. There is a prior note
about exactly this failure — retiring a claim on the line I was shown and
leaving its other carriers standing — and it recurred *inside the change that
was retiring it*.

No query over project state answers this. It needs a search for the claim's
other instances, which is why it is the one case that genuinely wants an agent
rather than a command.

### Arithmetic (claim 4)

Needs no external query at all. Two numbers in one document that must sum to a
constant and did not. Fully mechanical, and the cheapest of the five to catch.

## What today falsifies in #584

> Prudence does not cover this. She works where there is an artifact to check.
> Conversation has none, so nothing transfers.

The boundary is right; the examples were all on one side of it. Prudence caught
**four of today's five**, because they were in a PR — an artifact. The one she
could not reach was claim 1, made in chat and then written into an evaluation
doc, neither of which is diff-gated.

So the gap is narrower and better-defined than #584 states: not *conversation
versus artifacts*, but **diff-gated artifacts versus everything else**.
Everything else, counted from one day's output: 7 issue bodies, 6 issue/PR
comments, 2 evaluation docs, 4 architect pattern bodies, 3 skip records, ~6
knowledge rows, 1 repo doc. Plus the standing corpus — 542 pattern bodies, 2,643
knowledge rows, 28 evaluation docs — none of which any review path touches.

## The mechanical majority does not need an agent

Four of the five are settleable by one command. For those, the stronger design
is not to check the claim after the fact but to **make the claim carry its
command**, so verification is re-running it and comparing.

This repo already does exactly that and it works. Architecture cards carry:

```
anchor: techne/core/checks.py sha=c145a8eb18e23e1c88f16fb650964d434323ea15
```

`scripts/check_arch_cards.py` parses it with a strict `ANCHOR_RE`, re-hashes,
and reports drift across 14 cards and 19 anchors. The same shape applied to
numbers rather than files gives a mechanical check with no model in the loop.

Sketch, deliberately in the anchor's spelling:

```
evidence: sqlite3 ~/.techne/architect.db "SELECT COUNT(*) FROM patterns" = 542
```

The checker re-runs the command and compares. A changed number is drift, not
failure — the same non-blocking posture #505 argues for and `check_arch_cards`
already takes.

## What does need an agent

The distributional case, and only that. "Where else does this claim live" is a
search problem with judgement in it: the same assertion appears in a CHANGELOG,
a docstring, a comment and an issue body in four different phrasings, and no
regex spans them.

Scope it narrowly. Per the 2026-08-29 routing note, this shape is **Sonnet-shaped
and the intelligence belongs in the brief** — name the claim, demand a search
across the working tree and the stores, require the negative result (*which
carriers did you check and find consistent*) so an audit is distinguishable from
a list of whatever the agent happened to look at.

## What fires it

The failure of a purely consultable check is that it must be remembered, and all
five of today's failures happened at moments of confidence — precisely when it
would not be. Three mechanisms that do not depend on memory:

1. **In an existing loop.** `ruff` and `check_arch_cards.py` fire because they
   are in the pre-push path, not because anyone chooses them.
2. **On markers, not paths.** A percentage, a `file:line`, the word "verified" —
   greppable in what was just written, wherever it is going.
3. **As a sweep, after the fact.** Slower, but it reaches the corpus already
   written wrong, which a gate never will.

## Open questions

- **Does an `evidence:` line survive prose?** Architecture cards are structured
  documents; a pattern body is argumentative writing, and a citation every third
  sentence may make it unreadable. Worth prototyping on three existing pattern
  bodies before committing to the format.
- **What is the claim-extraction boundary?** #584 kept structured claim dicts as
  the interface and added claim *types*, not a parser. The marker-based trigger
  above implies some extraction. These need reconciling, and #584's position is
  the conservative one.
- **Does the distributional agent pay for itself?** It caught one failure in one
  day here. That is a real rate but a small sample, and it is the most expensive
  of the three mechanisms.
- **Sequencing.** The arithmetic check is nearly free, the `evidence:` line is a
  format decision, the agent is a build. They are independent; the cheapest one
  is not the one this note spends the most words on.

[#584]: https://github.com/tenishai/techne/issues/584
