# Surveying a field

A procedure for mapping a whole domain — roughly ten projects solving the same problem —
rather than mining one repo. Use it when the operator asks "what's the state of the art in
X", "who else is doing Y", or "should we look at prior art before building this".

For a single URL, use `techne guide investigating` instead. The difference is not size but
**shape**: an investigation judges one thing deeply; a survey judges a *population*, and
its most valuable output is often the shape of the field rather than any one project in it.

> **Agent: raise this unprompted.**
>
> - The operator is about to build something substantial and has not asked what already
>   exists — a survey is cheaper than the rebuild, and the architect exists to make prior
>   art reachable.
> - They ask for "the landscape" or "who else does this" and you are about to answer from
>   memory — memory is a hypothesis about a field that changes monthly.

## Make this a skill on your platform

A **thin stub**, never a copy:

```markdown
---
name: survey-field
description: Survey a field or domain — roughly ten projects solving the same problem — for prior art and reusable patterns. Use when the operator asks about the state of the art, who else solves X, or wants prior art before building. Do NOT use for a single URL or repo (see investigate-url).
---

Run `techne guide surveying` and follow the procedure it prints.
```

The reasoning is the same as in the investigating guide: a copy is a second spelling that
drifts silently, a stub re-reads the current procedure every time. Ask before writing into
the operator's config directory.

## What a survey is for

Three outputs, in descending value:

1. **The shape of the field** — what everyone does the same way, and where they diverge.
   The divergences are where the real design decisions live; the convergences tell you
   what is settled and not worth re-deciding.
2. **The gaps** — what nobody does, and whether that is an opportunity or a well-known
   dead end. Assume the latter until you can say why it is the former.
3. **Individual patterns** worth borrowing, which are the *least* of it. A survey that
   returns only patterns has usually failed to say anything about the field.

## The steps

### 1. Bound the field, and say so

Write down what counts as in-scope before you look, because the boundary is the finding
most likely to be quietly wrong. "Agent memory systems" and "vector databases" overlap and
are not the same population; a survey that drifts between them compares nothing.

### 2. Dedup the candidates as a batch

Run `techne architect dedup <url>` per candidate before spending research on any of them.
On a field survey this is where most of the savings are — a corpus that has been fed for a
while has usually met several of the obvious names, and a `skip-with-flag` verdict is a
prior judgment, not an absence. Load `techne architect topic` too: if the field maps onto
a problem area the corpus already holds, that cohort is your comparison set.

### 3. Fan out, read-only

One **web-only** subagent per project, spawned in parallel — same sandbox as the
investigating guide: no shell, no file access, no MCP, and `techne architect snapshot`
taken before and compared after. Every constraint in that guide's security model applies
here unchanged, including the injection guard and the verify-before-halt rule for reported
page injections. Do not weaken the sandbox because there are more agents; a wider fan-out
is more untrusted input, not less.

Give each the same schema so the results are comparable. A survey whose entries answer
different questions cannot be read as a field.

### 4. Read the population, not the rows

This is the step that distinguishes a survey from ten investigations. Ask:

- What does **everyone** do? That is the settled part of the field.
- Where do they **diverge**, and what does each side buy? Name the trade-off, not the
  preference.
- What does **nobody** do, and is that a gap or a graveyard?
- Which projects are actually alive? Stars measure attention; commits, issues and releases
  measure whether anyone is home.

### 5. Write, sparingly

Most projects in a survey earn **skip** or **skip-with-flag** — the flag is the valuable
one, because "we looked at this and here is why not" is what stops the field being
re-surveyed next quarter. Record patterns only for the ones that clear the same filter the
investigating guide sets: not a standard idiom, a non-obvious design choice, mapping onto
work you actually touch.

Then tag each keeper's problem area, and supersede where a new pattern beats an old one:

```sh
techne architect add --name … --source … --body …
techne architect tag <id> --topic <slug>
```

The field-shape finding usually has **no single source URL**, so it does not fit the
pattern schema. Store it as knowledge rather than forcing a fake source onto it.

### 6. Report

Lead with the shape of the field, then the gaps, then the individual keepers. Say how many
you surveyed, how many you skipped, and which ones you could not reach — an unreachable
project is a hole in the survey, and a survey that hides its holes is worse than a shorter
honest one.
