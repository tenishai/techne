"""Architectural-patterns markdown corpus parser.

`parse()` turns an `architectural-patterns.md`-style corpus into a `ParseResult`
(patterns + no-lift entries). It is the shared front end for the Architect store:
`patterns.ingest_patterns_md` seeds `architect.db` from a parsed corpus. This module
is pure parsing — it does NOT write to any store. (The old `import_into` writer that
dumped `type="pattern"` rows into personal techne.db was removed in techne#137; the
pattern corpus lives only in architect.db now.)

Two kinds of sections recognized:

  1. Pattern sections — `## <Pattern Name>` followed by lines
     `- **Source**: <text>` and `- **Surveyed**: YYYY-MM-DD`, then a
     body of paragraphs until the next `---` separator or `## ` heading.

  2. No-lift list — bullets under `## Surveyed (no patterns worth saving)`
     of the form `- <url> — surveyed YYYY-MM-DD — <reason>`. Preserves the
     "don't re-investigate" dedup signal.

Skipped sections: `## Composite observations`, `## Template for new entries`,
and any `## Surveyed` that isn't the no-lift list (defensive — we only
match the exact heading we ship).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedPattern:
    """A pattern section ready to be stored."""

    name: str
    source_url: str
    surveyed_date: str  # YYYY-MM-DD
    body: str  # full markdown body — everything after the Source/Surveyed lines
    relations: tuple[tuple[str, str], ...] = ()  # (kind, dst_name) — typed graph edges (techne#308)
    # The citation after the URL on the Source line (`path::symbol`, a line, a
    # section), kept as written. Until 2026-11-22 it was dropped here as
    # "decorative", so no stored pattern carried the code it was verified against.
    source_path: str | None = None
    # The commit the citation was read at, from `- **Commit**:` or a /blob/<sha>/ URL.
    source_commit: str | None = None


@dataclass(frozen=True, slots=True)
class NoLiftEntry:
    """A 'surveyed but nothing to lift' bullet."""

    source_url: str
    surveyed_date: str  # YYYY-MM-DD
    reason: str  # short human-readable why-not


@dataclass(frozen=True, slots=True)
class SkippedSection:
    """A `## ` section that looked like a pattern and could not be parsed.

    RETURNED, NOT ONLY LOGGED (techne#754). A section missing its `- **Source**:`
    line was dropped with a `logger.warning` and nothing else, so `techne architect
    ingest` printed `patterns: 0` and exited 0 on a file the operator had just
    written by hand. The bold markers are load-bearing — `- Source:` parses as body
    text — which makes a plausible first attempt fail in the one way this project
    keeps failing: silently, looking like success. The caller cannot report what the
    parser does not hand back.

    ALSO CARRIES NO-LIFT BULLETS NOW (techne#787), which is why `kind` exists and
    why `label` is no longer called `heading`. A malformed bullet under `## Surveyed
    (no patterns worth saving)` produced no entry here at all, so `architect ingest`
    fell into its heading-level branch and told the operator that "sections must
    start with `## `" about a file whose heading was already exactly that. Being
    sent to fix something that is not broken is worse than a vague error — the same
    sentence `_parse_pattern_body` carries about misplaced markers. One list rather
    than two: every consumer wants "what did you drop and why", and a second
    parallel field is a second thing to forget to print.
    """

    #: "section" for a `## ` pattern section, "no-lift bullet" for a bullet under
    #: the no-lift heading. Defaulted so the pattern-section constructions and any
    #: caller predating techne#787 keep working unchanged.
    label: str
    reason: str
    kind: str = "section"


@dataclass(frozen=True, slots=True)
class ParseResult:
    patterns: list[ParsedPattern]
    no_lifts: list[NoLiftEntry]
    #: Sections that looked like patterns and were not parsed. Defaulted so the two
    #: existing constructions (and any caller predating techne#754) keep working.
    skipped: tuple[SkippedSection, ...] = ()


# Skip these heading titles (case-insensitive prefix match) when scanning
# for pattern sections — they're meta or template content, not patterns.
SKIPPED_HEADINGS = (
    "composite observations",
    "template for new entries",
)

NO_LIFT_HEADING = "surveyed (no patterns worth saving)"


# A pattern's URL line looks like:
#   - **Source**: https://github.com/foo/bar — `path/to/file.ts::Symbol`
# The first http(s) URL is `source_url`; whatever follows it is the citation and is
# stored as `source_path` (`split_source`). It used to be called decorative and
# dropped, which threw away every code citation the investigating guide asks for.
_URL_RE = re.compile(r"https?://\S+")
# A git object name: 7 to 40 hex characters, as `git rev-parse --short` and full shas.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# A commit pinned in a GitHub-style URL: .../blob/<sha>/... or .../tree/<sha>/...
_URL_SHA_RE = re.compile(r"/(?:blob|tree|commit)/([0-9a-f]{7,40})(?:/|$|#)")
# Separators between the URL and the citation: em/en dash, hyphen, colon, comma.
_CITATION_LEAD = " \t—–-:,;"


def split_source(text: str) -> tuple[str, str | None] | None:
    """Split a Source value into (url, citation). None when there is no http(s) URL.

    One function for `ingest` and `architect add`, so both store the same thing for
    the same line: the URL normalised exactly as before, and the rest of the line,
    stripped of its leading separator, as the citation (None when empty).
    """
    m = _URL_RE.search(text)
    if m is None:
        return None
    url = m.group(0).rstrip("),.;")
    # From the end of the whole match: punctuation `rstrip` removed from the URL closed
    # the sentence around it and is not part of the citation.
    rest = text[m.end() :].strip().lstrip(_CITATION_LEAD).strip()
    return url, (rest or None)


def commit_from(value: str | None, url: str | None = None) -> str | None:
    """A commit sha from a `- **Commit**:` value, else from a pinned URL, else None.

    The value must be one hex object name, lower-cased; anything else is not a commit
    and returns None rather than storing prose in the column.
    """
    if value:
        v = value.strip().strip("`").lower()
        if _SHA_RE.match(v):
            return v
    if url:
        m = _URL_SHA_RE.search(url)
        if m:
            return m.group(1)
    return None
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Typed graph edges declared as preamble fields (techne#308), e.g.
#   - **Composes-with**: [[Other pattern]], [[Another]]
#   - **Variant-of**: [[Base pattern]]
# Only the coexistence kinds are declarative here; `supersedes` is a deliberate,
# reviewed curation action (`architect tag … --superseded-by`), never an ingest
# side-effect. The `[[wikilink]]` targets are resolved to ids by the store layer.
_RELATION_LABELS = {"composes-with": "composes-with", "variant-of": "variant-of"}
_REL_BULLET_RE = re.compile(r"^-\s+\*\*(?P<label>[\w-]+)\*\*:\s*(?P<rest>.+)$")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _relation_targets(rest: str) -> list[str]:
    """Extract `[[...]]` target names from a relation-field value, dropping any
    `file#heading` prefix (heading after the last `#`). Order-preserved."""
    out: list[str] = []
    for raw in _WIKILINK_RE.findall(rest):
        name = raw.rsplit("#", 1)[-1].strip()
        if name:
            out.append(name)
    return out


def parse(text: str) -> ParseResult:
    """Parse a markdown document into ParsedPattern + NoLiftEntry lists.

    Tolerant by design: unrecognized sections are skipped silently. Malformed
    pattern bodies (missing source/date) are skipped too, but are RETURNED in
    `skipped` as well as logged, so a caller can tell the operator which section it
    dropped and why (techne#754) rather than reporting a successful zero.
    """
    patterns: list[ParsedPattern] = []
    no_lifts: list[NoLiftEntry] = []
    skipped: list[SkippedSection] = []

    sections = _split_into_sections(text)
    for heading, body in sections:
        heading_lower = heading.lower()
        if heading_lower == NO_LIFT_HEADING:
            found, malformed = _parse_no_lift_bullets(body)
            no_lifts.extend(found)
            skipped.extend(malformed)
            continue
        if any(heading_lower.startswith(skip) for skip in SKIPPED_HEADINGS):
            continue
        # Anything else is a pattern candidate
        parsed = _parse_pattern_body(heading, body)
        if isinstance(parsed, ParsedPattern):
            patterns.append(parsed)
        else:
            skipped.append(SkippedSection(label=heading, reason=parsed))

    return ParseResult(patterns=patterns, no_lifts=no_lifts, skipped=tuple(skipped))



# The entry separator `export_patterns_md` writes: ---, ***, or ___ (3+), alone on
# its line, at COLUMN 0. Deliberately NOT tolerating leading indentation
# (Prudence, #778 r2 Nit 1): export always emits the separator unindented
# (patterns.py), so allowing up to 3 spaces made this recognise "a markdown rule"
# when its job is to recognise "export's separator" — and the difference is real,
# since `'- alpha\n\n  ---'` is a divider inside a trailing list item, which the
# wider form ate. Convergence is untouched; every accreted rule is unindented.
_RULE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")


def _strip_trailing_rule(body: str) -> str:
    """Drop the `---` separators `export_patterns_md` writes AFTER each entry.

    Without this the separator is absorbed into the preceding section's body, and
    since a pattern id is a content hash of (name, source_url, body), the id
    changes on every export -> ingest cycle and the store ACCRETES a near-duplicate
    each time instead of recognising the pattern it already holds (techne#770).

    Strips ALL trailing rules, not one, so the fix is CONVERGENT: re-ingesting a
    corpus that already carries `---\n\n---\n\n---` from earlier cycles normalises
    back to the clean body and its original id, rather than fixing the growth rate
    while leaving the damage.

    A POPPED RULE MUST BE PRECEDED BY A BLANK LINE (or be the whole body). Without
    that condition this also eats a trailing SETEXT heading underline, silently
    demoting a heading to a paragraph:

        'Alpha beta.\n\nCaveats\n---'  ->  'Alpha beta.\n\nCaveats'

    The format itself distinguishes the two and the strip should use it:
    `export_patterns_md` ALWAYS emits the separator after a blank line
    (patterns.py: body, "", "---", ""), whereas a setext underline is by
    definition attached to the non-blank line above it. Convergence is unaffected —
    accreted rules are all of the form `\n\n---`, so they still collapse.
    (Prudence, #778 Nit 2.)

    A rule inside a CLOSED fenced block is safe: the closing ``` is the last line,
    so the loop stops before reaching it. This helper has no fence awareness of its
    own, so the guarantee is that narrow one — an UNCLOSED fence ending in blank +
    rule would have its rule stripped. That is unreachable in practice for a louder
    reason: an unclosed fence already swallows every subsequent `## ` heading in
    `_split_into_sections`, so the document fails long before this matters
    (Prudence, #778 r2 Nit 2).
    """
    lines = body.rstrip().splitlines()
    while lines:
        last = lines[-1]
        if not last.strip():
            lines.pop()
            continue
        if _RULE_RE.match(last) and (len(lines) == 1 or not lines[-2].strip()):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Return [(heading, body), ...] split on `## ` top-level headings.

    Anything before the first `## ` heading is dropped (frontmatter,
    file-level prose). Headings INSIDE fenced code blocks (```) are
    treated as body content, not section breaks, so example markdown in
    a "Template for new entries" section doesn't fragment the parse.
    """
    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_body: list[str] = []
    in_fence = False

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if current_heading is not None:
                current_body.append(line)
            continue
        # `## ` heading (exactly two hashes; deeper levels stay in body).
        # Skip detection while inside a fenced code block.
        if not in_fence and line.startswith("## ") and not line.startswith("### "):
            if current_heading is not None:
                sections.append(
                    (current_heading, _strip_trailing_rule("\n".join(current_body)))
                )
            current_heading = line[3:].strip()
            current_body = []
        elif current_heading is not None:
            current_body.append(line)

    if current_heading is not None:
        sections.append((current_heading, _strip_trailing_rule("\n".join(current_body))))

    return sections


def _parse_pattern_body(heading: str, body: str) -> ParsedPattern | str:
    """Extract URL + date + remaining body for a pattern section.

    Returns the parsed pattern, or a REASON STRING if the section could not be
    parsed. It used to return None and log — which left the caller unable to tell
    the operator anything (techne#754). The string is the message a human sees, so
    it names the missing marker rather than the field.
    """
    source_line: str | None = None
    date_line: str | None = None
    commit_line: str | None = None
    body_lines: list[str] = []
    relations: list[tuple[str, str]] = []

    state: Literal["preamble", "body"] = "preamble"
    for line in body.splitlines():
        if state == "preamble":
            stripped = line.strip()
            if stripped.startswith("- **Source**:"):
                source_line = stripped
                continue
            if stripped.startswith("- **Surveyed**:"):
                date_line = stripped
                continue
            if stripped.startswith("- **Commit**:"):
                commit_line = stripped[len("- **Commit**:") :]
                continue
            rel = _REL_BULLET_RE.match(stripped)
            if rel is not None and rel.group("label").lower() in _RELATION_LABELS:
                kind = _RELATION_LABELS[rel.group("label").lower()]
                relations.extend((kind, name) for name in _relation_targets(rel.group("rest")))
                continue
            if stripped == "":
                # Blank line still in preamble — skip
                continue
            # First non-source/date/relation/blank line: body starts
            state = "body"
        if state == "body":
            body_lines.append(line)

    if source_line is None or date_line is None:
        # Name the MISSING one, and say the markers are required. "missing Source or
        # Surveyed line" does not tell an operator staring at a file that plainly has
        # both which of `- Source:` / `- **Source**:` the parser wanted.
        absent = [
            n for n, v in (("`- **Source**:`", source_line), ("`- **Surveyed**:`", date_line))
            if v is None
        ]
        # DIAGNOSE THE RIGHT THING (Prudence, #758 Important C). The metadata bullets must
        # come FIRST: the first non-metadata, non-blank line ends the preamble and
        # everything after it is body. So a section that opens with framing prose — an
        # ordinary way to write — has its perfectly correct `- **Source**:` line eaten as
        # body, and the old message then told the operator "the ** bold markers are
        # required" about a file whose markers were right. Being told to fix something
        # that is not broken is worse than a vague error.
        misplaced = [
            n
            for n, marker in (
                ("`- **Source**:`", "- **Source**:"),
                ("`- **Surveyed**:`", "- **Surveyed**:"),
            )
            if n in absent and any(ln.strip().startswith(marker) for ln in body.splitlines())
        ]
        if misplaced:
            reason = (
                f"{' and '.join(misplaced)} appears AFTER the body starts — the metadata "
                "bullets must come first, before any prose, or they are read as body text"
            )
        else:
            reason = (
                f"no {' or '.join(absent)} line{'s' if len(absent) > 1 else ''} "
                "(the ** bold markers are required)"
            )
        logger.warning("skip_section", extra={"heading": heading, "reason": reason})
        return reason

    split = split_source(source_line[len("- **Source**:") :])
    date_match = _DATE_RE.search(date_line)
    if split is None or date_match is None:
        reason = (
            "no http(s) URL in the Source line"
            if split is None
            else "no YYYY-MM-DD date in the Surveyed line"
        )
        logger.warning("skip_section", extra={"heading": heading, "reason": reason})
        return reason

    url, citation = split
    return ParsedPattern(
        name=heading,
        source_url=url,
        surveyed_date=date_match.group(1),
        body="\n".join(body_lines).strip(),
        relations=tuple(relations),
        source_path=citation,
        source_commit=commit_from(commit_line, url),
    )


# A no-lift bullet looks like:
#   - https://github.com/foo/bar — surveyed 2026-06-12 — short reason here
# Em-dashes (—) separate the URL, the surveyed clause, and the reason.
_NO_LIFT_RE = re.compile(
    r"^\s*-\s+(?P<url>https?://\S+)\s*—\s*surveyed\s+(?P<date>\d{4}-\d{2}-\d{2})\s*—\s*(?P<reason>.+)$"
)


#: A bullet whose FIRST token is an http(s) URL. Used ONLY to decide whether an
#: unmatched line under the no-lift heading is a malformed ENTRY or ordinary prose
#: — see `_parse_no_lift_bullets`. Deliberately not reused as the entry parser.
#:
#: ANCHORED, NOT `.*?` (Prudence, #788 r1 Important). The first version matched a
#: URL ANYWHERE in the bullet, which is not the predicate its own docstring
#: promised, and it flagged three ordinary constructions — a commentary bullet
#: carrying a markdown link, a nested sub-bullet note, and a `- [ ]` task item.
#: Worse than noise: `architect ingest` gates its heading-level advice on
#: `if not counts["skipped"]`, so a false positive SUPPRESSES a correct
#: diagnosis. Measured — a file with a single-hash heading plus one commentary
#: bullet lost the right message and gained a wrong one, which is precisely the
#: #787 defect inverted. The required form puts the URL first, so anchoring there
#: keeps every malformed-entry case (including the motivating `— RE-SURVEYED —`)
#: and drops all three false positives.
#:
#: DECORATIONS BEFORE THE URL ARE ALLOWED, and this comment does NOT claim the
#: list is closed (Prudence, #788 r2 and r3). The narrow `<?` anchor dropped four
#: malformed spellings while declaring one, and its cost was misstated: main's
#: behaviour on a dropped entry is not silence, it is the #787 misdiagnosis.
#: Measured through the real CLI — a link-wrapped entry under a correct `## `
#: heading made ingest answer "sections must start with `## ` (two hashes)".
#:
#: COVERED: bare, `<angle-bracketed>`, `[link-wrapped](…)`, and a run of
#: `` ` `` / `*` / `_` / `~` (so `` `code` ``, `*i*`, `**b**`, `_i_`, `__b__`,
#: `~~s~~`). NOT EXHAUSTIVE, and deliberately not presented as such: quoted
#: `"https://…"` and parenthesised `(https://…)` are still dropped, and someone
#: will write a decoration none of us imagined. An uncovered decoration falls back
#: to main's behaviour, which is the #787 misdiagnosis — that is the standing cost
#: of this predicate, not a defect to be surprised by. r3 rejected "add `_`" as the
#: fix precisely because chasing spellings has no natural end; the durable change
#: is a comment that stays true when the next one appears.
#:
#: THE ACCEPTED TRADE — LINK-FIRST PROSE IS ACCUSED. `- [foo](url) — surveyed … — r`
#: (an entry) and `- [foo](url) explains why.` (commentary) are structurally
#: identical up to the first token, so no prefix anchor can separate them; `](`
#: after the bullet separates link-FIRST from link-LATER, not entry from prose.
#: Link-first commentary is therefore reported, and `_BULLET_BOUNDARY` pins that as
#: a deliberate True so the next reader inherits the trade instead of re-deriving
#: it. Separating them properly means keying on the REMAINDER (does the rest carry
#: `surveyed` and a date?) rather than the prefix — filed rather than done here,
#: because adding capability in a terminator round is how the last three rounds
#: each introduced something.
#:
#: Link-LATER prose stays out, which is what `](`-immediately-after-the-bullet
#: does buy: `- Context lives in [the notes](https://…)` does not match, nor
#: `- [ ] revisit https://…` (`] r`, not `](`), nor `- [x] https://… was checked`.
_URL_BULLET_RE = re.compile(r"^\s*-\s+(?:<|\[[^\]]*\]\(|[`*_~]+)?https?://\S+")


def _parse_no_lift_bullets(body: str) -> tuple[list[NoLiftEntry], list[SkippedSection]]:
    """Walk the body, returning (parsed entries, bullets that looked like entries).

    THE SECOND LIST IS THE POINT (techne#787). This used to `continue` past any
    non-matching line and record nothing, so a bullet that was one word wrong --
    `— RE-SURVEYED 2026-08-26 —` for `— surveyed 2026-08-26 —` -- vanished, and
    `architect ingest` then had an empty `skipped` and fell into its heading-level
    branch: "sections must start with `## ` (two hashes)", said about a file whose
    heading was exactly that and whose real defect was never named. Measured on main
    before this change.

    A BULLET CARRYING A URL IS THE PREDICATE, not "any bullet". Under this heading a
    line with an `http(s)://` in it is unambiguously a no-lift entry that failed to
    parse; a bullet without one is plausibly commentary, and flagging it would make
    the report noisy in exactly the situation where the operator most needs to trust
    it. Narrow on purpose: a false "you got this wrong" is the failure this change
    exists to remove, so it must not introduce one.
    """
    out: list[NoLiftEntry] = []
    malformed: list[SkippedSection] = []
    # FENCED LINES ARE NOT ENTRIES (Prudence, #788 r1). A counter-example shown
    # under this heading as "what not to write" is the exact shape this function
    # now accuses, and `_split_into_sections` already tracks precisely this state —
    # this was the one line-walker in the module that did not inherit it. The
    # URL-first anchoring above does NOT cover this case: a fenced counter-example
    # is usually a well-formed-looking entry, which is the point of showing it.
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _NO_LIFT_RE.match(line)
        if match is not None:
            out.append(
                NoLiftEntry(
                    source_url=match.group("url").rstrip("),.;"),
                    surveyed_date=match.group("date"),
                    reason=match.group("reason").strip(),
                )
            )
            continue
        if _URL_BULLET_RE.match(line):
            # Quote the line rather than describe it: the difference is usually one
            # token, and an operator comparing their line to the required form spots
            # it faster than they parse a sentence about it.
            malformed.append(
                SkippedSection(
                    label=line.strip(),
                    reason=(
                        "does not match `- <url> — surveyed YYYY-MM-DD — <reason>` "
                        "(em-dashes —, the literal word `surveyed`, and the date in "
                        "that order)"
                    ),
                    kind="no-lift bullet",
                )
            )
    return out, malformed
