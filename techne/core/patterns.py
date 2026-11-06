"""Pattern store data layer over architect.db (techne#87).

CRUD + dedup + search for the Architect's pattern domain. Backend-portable in
spirit (Postgres is P3) but SQLite-direct for now via plain_connect — architect.db
is a domain db, so it gets only its own schema (no techne core tables).

Dedup is lens-aware: `investigations` keyed (url × surveyed_by × lens) makes the
SRE-vs-infra comparison a join. Patterns hang off an investigation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

from techne.core.stores import plain_connect


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


# --- investigation verdicts (techne#428) -------------------------------------
#
# The verdict is meant to record the OPERATOR'S judgment about a surveyed URL.
# It did not. `ingest_patterns_md` hardcoded "full-eval" for any URL group that
# carried patterns and "skip-with-flag" for a no-lift bullet, and the CLI had no
# way to pass one — so the column recorded *whether the markdown contained
# patterns*, which the patterns table already says, and `patterns-only` was
# literally unreachable. Live corpus when this was found: 202 full-eval,
# 70 skip-with-flag, 0 patterns-only, against 19 eval docs on disk.
#
# That made the field worse than empty: reading `verdict=full-eval` on a
# re-investigated URL invited the inference "a prior pass judged this
# decision-forcing but never wrote its eval doc", which was an artifact of the
# default.
VERDICTS = ("skip", "skip-with-flag", "patterns-only", "full-eval", "unknown")

# What an ingest records when the caller does not say. `patterns-only` is the
# documented modal outcome and `full-eval` is documented as ~5-10%, so defaulting
# here is right most of the time where the old hardcode was right rarely. It is
# still a default, not a judgment — pass --verdict to state one.
DEFAULT_INGEST_VERDICT = "patterns-only"

# Reserved for rows whose verdict was auto-assigned before this existed and so
# never meant anything. Honest about not knowing rather than guessing.
UNKNOWN_VERDICT = "unknown"

# surveyed_at is TEXT holding a zero-padded ISO date (import_md enforces
# \d{4}-\d{2}-\d{2} at parse time), so `surveyed_at < ?` is a sound string
# comparison — but ONLY if the bound value is the same shape. Validated where it
# enters, because the date scoping is the sweep's only safety property.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- graph extraction (techne#87 P2) ----------------------------------------
#
# The relationship signals are already embedded in pattern *bodies* — lifted by
# the investigate-url skill — they just were never materialized into rows:
#   * `[[architectural-patterns#<name>]]` wikilinks  → pattern_relations edges
#   * `**in <target>**:` markers in the where-to-apply line → pattern_applies
# Backfill parses those out and writes the edges; resolution mirrors codex#89
# (resolve by name, keep `unresolved` + the raw name when it doesn't match).

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# A pattern's target project is named on its "Where to apply" line as
# `…where to apply in <target>…` (the live corpus folds it into the bold heading,
# `**Where to apply in techne**:`). We anchor on that exact phrase, so descriptive
# prose elsewhere (`**built in techne**`) never manufactures a spurious applies row
# (Prudence, PR#130). A target named only in non-bold prose after the colon is not
# extracted — an honest under-reach, not a false positive.
#
# The capture takes the token after "apply in" PLUS a DELIMITED continuation
# (issue #693): `in techne / cabal / liminal` and `in techne, cabal and argus` name
# three targets, and a single capture kept the first and silently dropped the rest
# while the row still looked linked. Only `/`, `,`, `, and`, `and` and `&` continue
# the list, on ONE line, and the continuation counts only when the list then ENDS —
# at a closing `**`, a `:` or the end of the line. Otherwise prose after the first
# target (`in techne, cabal's gate differs`, `in techne, similar to liminal's`,
# `in techne (vs cabal)`) would link a target the heading never claimed; there
# the capture falls back to the first target alone.
_APPLY_SEP = r"[ \t]*(?:/|,[ \t]*and\b|,|\band\b|&)[ \t]*"
_APPLY_RE = re.compile(
    r"where\s+(?:to|it might|this)\s+appl\w*\s+in\s+"
    r"([a-z][a-z0-9-]*(?:(?:" + _APPLY_SEP + r"[a-z][a-z0-9-]*)+(?=[ \t]*(?:\*\*|:|$)))?)",
    re.IGNORECASE | re.MULTILINE,
)
_APPLY_SPLIT_RE = re.compile(_APPLY_SEP, re.IGNORECASE)


# The projects a pattern may declare itself relevant to. NOT hardcoded prose:
# every name here is a real target named in the /investigate-url skill's context
# block, so a pattern that says "apply in prudence" resolves instead of being
# silently dropped (techne#674 — 14 pattern bodies named Prudence and none could
# ever link, which made `architect applies prudence` read as "we hold nothing").
def known_targets() -> set[str]:
    """The project names a pattern may be tagged as applying to.

    CONFIGURED, not hardcoded (techne#1008). This used to be a literal set of ten
    of this project's own repo names, and `mcp_server.py` interpolates it into the
    `pattern_search` tool description — so every agent connecting to a public
    techne was handed a list of private projects it can never see.

    It is validation as well as advertising: `_apply_targets` DROPS an unlisted
    target rather than minting one, because an unknown token is nearly always a
    typo. So an operator whose store has history must list what that history uses,
    which `techne architect targets` prints.
    """
    from techne.config import Config

    return {str(t) for t in Config.load().architect.pattern_targets}


# Pre-rename spellings still present in older pattern bodies. Extraction
# canonicalises through this map so ONE PROJECT CANNOT OCCUPY TWO TARGETS —
# `limenal` was renamed to `liminal` on 2026-08-11 and the set was not updated,
# so backfill emitted `limenal` while the direct-tag path wrote `liminal`,
# leaving 11 patterns double-listed under both (techne#674).
_TARGET_ALIASES = {"limenal": "liminal"}


def target_aliases() -> dict[str, str]:
    """Body spelling -> target: the built-in pre-rename aliases, overlaid with the
    operator's `architect.target_aliases` (issue #1293).

    Config wins on a clash, since it is the operator saying what a name means in
    their corpus. Read it once per loop, like `known_targets()`.
    """
    from techne.config import Config

    return {**_TARGET_ALIASES, **Config.load().architect.target_aliases}


#: Built-in search synonyms (issue #1345): old spelling -> current name. 129 active
#: patterns were written before `limenal` was renamed `liminal` (2026-08-10), and
#: FTS5 matches whole tokens, so a search for "liminal" missed every one of them.
#: Rewriting the bodies is not an option: a pattern id is a hash of its content.
_SEARCH_ALIASES = {"limenal": "liminal"}


def search_alias_groups() -> dict[str, tuple[str, ...]]:
    """Each spelling -> every spelling that means the same thing, for search.

    The built-in aliases overlaid with `architect.search_aliases`. Both directions
    resolve, and chains are followed: `{a: b, b: c}` puts a, b and c in one group,
    so a project renamed twice still finds its oldest bodies.
    """
    from techne.config import Config

    try:
        configured = Config.load().architect.search_aliases
    except Exception:  # a broken config costs the extra aliases, not the search
        configured = {}
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for old, new in {**_SEARCH_ALIASES, **configured}.items():
        parent[find(old)] = find(new)
    members: dict[str, set[str]] = {}
    for x in list(parent):
        members.setdefault(find(x), set()).add(x)
    groups: dict[str, tuple[str, ...]] = {}
    for ms in members.values():
        ordered = tuple(sorted(ms))
        for m in ms:
            groups[m] = ordered
    return groups


_PHRASE = re.compile(r'"(?:[^"]|"")*"')


def _alias_key(phrase: str) -> str:
    """The word a phrase literal is looked up by: unquoted, surrounding punctuation
    stripped, lowercased. The same normalisation `search.py` applies to identifier
    terms (`_IDENTIFIER_STRIP`, then a trailing period), so `liminal?`, `(liminal)`
    and `liminal.` find the alias exactly as FTS5 tokenises them (Prudence, #1347)."""
    from techne.core.search import _IDENTIFIER_STRIP

    return phrase[1:-1].replace('""', '"').strip(_IDENTIFIER_STRIP).rstrip(".").lower()


def _expand_aliases(match_str: str, groups: dict[str, tuple[str, ...]]) -> str:
    """Widen each aliased phrase literal in an FTS5 MATCH string to an OR group.

    Input is what `prepare_fts_query` (`"a" "b"`, implicit AND) or
    `build_or_fts_query` (`"a" OR "b"`) produced. A string with no aliased term is
    returned unchanged. Otherwise it is rebuilt with an explicit joiner, because
    FTS5 rejects implicit AND next to a parenthesised group: `("x" OR "y") "b"` is
    a syntax error, `("x" OR "y") AND "b"` is not.

    The rebuild drops phrases with no letter or digit (`"-"`, `"—"`, `"&"`). Under
    implicit AND FTS5 ignores a phrase with no tokens; under explicit AND it matches
    nothing, which would empty the AND pass (Prudence, #1347). Group members are
    quote-doubled like every other MATCH term, so a configured spelling can't break
    the string (#74). Lookup is per query word, so a multi-word old name is reached
    from its new name (it goes out as a phrase) but never from its own words.
    """
    phrases = _PHRASE.findall(match_str)
    if not any(_alias_key(ph) in groups for ph in phrases):
        return match_str
    joiner = " OR " if " OR " in _PHRASE.sub("", match_str) else " AND "
    out = []
    for ph in phrases:
        g = groups.get(_alias_key(ph))
        if g:
            out.append("(" + " OR ".join('"' + m.replace('"', '""') + '"' for m in g) + ")")
        elif any(c.isalnum() for c in ph):
            out.append(ph)
    return joiner.join(out)


# Outcome lifecycle (techne#426). 'filed' → work exists; 'shipped' → it landed;
# 'rejected' → considered and declined, which is a real signal, not an absence.
_OUTCOME_STATES = {"filed", "shipped", "rejected"}


#: WHEN A RE-INGEST REPLACES THE STORED SURVEY, as ONE definition (techne#429/#711).
#:
#: `surveyed_at` used to be updated UNCONDITIONALLY while `verdict` was guarded, so a
#: re-ingest that declined to replace the verdict still stamped the new date — pairing
#: a date from one investigation with a verdict from another. The row then asserts,
#: with no way to tell from the outside, that the OLD judgement was reached on the NEW
#: date. That is the kind of claim `investigations` exists to make trustworthy: the
#: liveness sweep in #431 and the drift mechanism in #505 both key on this date.
#:
#: The date and the verdict are ONE FACT — "this is the survey we are keeping" — so
#: they must move together or not at all. Written once and interpolated into both
#: assignments rather than repeated, because two spellings of one predicate is exactly
#: how they drifted apart in the first place.
#:
#: THE TRADE THIS MAKES, STATED BECAUSE IT IS NOT FREE (Prudence, #738). The ordinary
#: `techne architect ingest` IS the declined path — `ingest_patterns_md` passes
#: `overwrite_verdict=stated` where `stated = verdict is not None`, and the CLI's
#: `--verdict` defaults to None. So for the ordinary pattern-ingest path this
#: predicate is false and `surveyed_at` FREEZES AT THE LAST ACCEPTED WRITE — usually
#: first ingest, but on the `clears_skip` path the date moves once and freezes there.
#: Before, it always advanced.
#:
#: AND `skip%` ROWS ARE NOT AN EXCEPTION, though it reads like they might be: the
#: no-lift call site passes overwrite_verdict=False and does NOT pass clears_skip, so a
#: skip row re-ingested through it freezes too — even with `--verdict` stated.
#:
#: BOTH BEHAVIOURS ARE WRONG, and this one is the less wrong. The column is carrying two
#: facts in one slot — WHEN THE KEPT VERDICT WAS REACHED and WHEN WE LAST LOOKED — and
#: declining to restate a judgement is not the same as declining to have looked. The old
#: bug made the date too NEW (a fresh date on a stale verdict, undetectable from
#: outside); this makes it too OLD (a frozen date on a URL we did re-survey). A frozen
#: date is at least honest about the verdict it accompanies, which is what #431's
#: liveness sweep and #505's drift mechanism actually key on. Splitting the two facts
#: needs a `last_seen_at` column — tracked as techne#741, deliberately not a schema
#: migration inside a release-gated fix.
#:
#: THE BOUNDARY IS DELIBERATE: `summary`, `skip_reason` and `maturity` below are still
#: COALESCEd from the declined investigation, so a kept row can adopt a newer summary.
#: They only ever fill a NULL, and unlike the date they make no claim about WHEN — so
#: the "these are one fact" argument does not extend to them.
_TAKES_NEW_SURVEY = "(:overwrite OR (:clears_skip AND investigations.verdict LIKE 'skip%'))"


def _wikilink_targets(body: str) -> list[str]:
    """Extract the referenced pattern names from `[[...]]` links in *body*.

    Links look like `[[architectural-patterns#Hybrid rank fusion: BM25 + vec]]`
    (an Obsidian file#heading ref). We want the heading after the last `#`; a
    bare `[[Name]]` (no `#`) is taken whole. Self/empty refs are dropped by the
    caller via name resolution. Deduped, order-preserved.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _WIKILINK_RE.findall(body or ""):
        name = raw.rsplit("#", 1)[-1].strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _apply_targets(
    body: str, allowed: set[str] | None = None, aliases: dict[str, str] | None = None
) -> list[str]:
    """Extract canonical project targets from `where to apply in <target>` phrases
    in *body*. The token after "apply in" is taken, plus any further tokens joined
    to it by `/`, `,`, `and` or `&` (issue #693); unknown tokens are dropped.
    Deduped, order-preserved.

    *allowed* defaults to the configured set (`known_targets()`) and *aliases* to
    `target_aliases()`. Callers looping over many rows should resolve both ONCE and
    pass them, rather than re-reading config per row; tests pass the sets they mean
    instead of the operator's.
    """
    out: list[str] = []
    seen: set[str] = set()
    if allowed is None:
        allowed = known_targets()
    if aliases is None:
        aliases = target_aliases()
    for group in _APPLY_RE.findall(body or ""):
        for raw in _APPLY_SPLIT_RE.split(group):
            t = aliases.get(raw.lower(), raw.lower())
            if t in allowed and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _norm(name: str) -> str:
    """Normalize a pattern name for fuzzy resolution: lowercase, collapse runs of
    non-alphanumerics to a single space, strip. So `BM25 + vec` ~ `bm25 vec`."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def upsert_investigation(
    db_path: Path,
    *,
    url: str,
    surveyed_at: str,
    verdict: str,
    surveyed_by: str = "operator",
    lens: str = "infra",
    summary: str | None = None,
    skip_reason: str | None = None,
    maturity: str | None = None,
    overwrite_verdict: bool = True,
    clears_skip: bool = False,
) -> str:
    """Insert/update one investigation (the dedup unit). Returns its id.

    `verdict` must be one of VERDICTS. It is validated here rather than by a
    schema CHECK because the constraint would need a full SQLite table rebuild to
    add to an existing db, and this is the only write path — a bad value should
    fail loudly at the call site, not land as a silent out-of-vocabulary string
    that every downstream query then has to tolerate (techne#428).

    `overwrite_verdict=False` makes the verdict INSERT-ONLY: a new row gets it, an
    existing row keeps whatever it already has. Callers pass False when the value
    is a fallback rather than a stated judgment. Without this, a default that
    overwrites on every re-ingest is techne#428 again with a nicer constant — and
    concretely it undid `set_verdict`: state a judgment with `reverdict`, re-ingest
    the same markdown, and a routine ingest silently reverts it (Prudence, PR #432).

    `clears_skip=True` additionally displaces an existing `skip*` verdict even when
    the write is insert-only. Callers pass it when the write itself proves the URL
    DID yield patterns, which contradicts any skip verdict — "no patterns worth
    saving" cannot survive a pattern being saved. Without it, insert-only stranded
    a URL first seen as a no-lift bullet and later carrying patterns: the row kept
    `skip-with-flag` while owning a pattern, so `stats()` reported it under both
    `patterns` and `skips` at once.

    BOTH FLAGS NOW GATE `surveyed_at` AS WELL AS THE VERDICT (techne#429). The names
    say verdict and the behaviour is wider: declining the verdict also declines the
    date, because the two are one fact — "this is the survey we are keeping". Noted
    here because this docstring is what a call site and `help()` show.
    """
    if verdict not in VERDICTS:
        raise ValueError(
            f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}. "
            "The verdict is the operator's judgment about the URL — see techne#428."
        )
    inv_id = _hash(url, surveyed_by, lens)
    conn = plain_connect(db_path)
    try:
        # Named parameters, not positional: the overwrite flag governs a clause ten
        # `?` slots away from where it would have been bound, and a future edit that
        # inserts a column would silently misbind it (Prudence, PR #432).
        conn.execute(
            """INSERT INTO investigations
                 (id, url, surveyed_by, lens, surveyed_at, verdict, summary, skip_reason, maturity)
               VALUES (:id, :url, :surveyed_by, :lens, :surveyed_at, :verdict,
                       :summary, :skip_reason, :maturity)
               ON CONFLICT(url, surveyed_by, lens) DO UPDATE SET
                 surveyed_at = CASE WHEN """
            + _TAKES_NEW_SURVEY
            + """
                                 THEN excluded.surveyed_at
                                 ELSE investigations.surveyed_at END,
                 verdict = CASE WHEN """
            + _TAKES_NEW_SURVEY
            + """
                             THEN excluded.verdict
                             ELSE investigations.verdict END,
                 summary = COALESCE(excluded.summary, investigations.summary),
                 skip_reason = COALESCE(excluded.skip_reason, investigations.skip_reason),
                 maturity = COALESCE(excluded.maturity, investigations.maturity)""",
            {
                "id": inv_id,
                "url": url,
                "surveyed_by": surveyed_by,
                "lens": lens,
                "surveyed_at": surveyed_at,
                "verdict": verdict,
                "summary": summary,
                "skip_reason": skip_reason,
                "maturity": maturity,
                "overwrite": 1 if overwrite_verdict else 0,
                "clears_skip": 1 if clears_skip else 0,
            },
        )
        conn.commit()
        return inv_id
    finally:
        conn.close()


_KEEP_REASON = object()


def set_verdict(
    db_path: Path,
    *,
    url: str,
    verdict: str,
    surveyed_by: str = "operator",
    lens: str = "infra",
    reason: object = _KEEP_REASON,
) -> bool:
    """Correct one investigation's verdict in place. True if a row was updated.

    Deliberately does NOT touch surveyed_at — correcting a judgment is not a
    re-survey, and conflating the two is the adjacent bug in techne#429.

    *reason* (issue #1293): left out, `skip_reason` is kept as it is; a string
    replaces it; None clears it. A verdict and the reason that explained it are one
    judgment, so a correction that could only reach one of them left `dedup` printing
    `verdict=patterns-only reason=<why we skipped it>`.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}")
    conn = plain_connect(db_path)
    try:
        if reason is _KEEP_REASON:
            cur = conn.execute(
                "UPDATE investigations SET verdict = ? WHERE id = ?",
                (verdict, _hash(url, surveyed_by, lens)),
            )
        else:
            cur = conn.execute(
                "UPDATE investigations SET verdict = ?, skip_reason = ? WHERE id = ?",
                (verdict, reason, _hash(url, surveyed_by, lens)),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_investigation(
    db_path: Path, *, url: str, surveyed_by: str = "operator", lens: str = "infra"
) -> dict | None:
    """One investigation row by its identity (url, surveyed_by, lens), or None."""
    conn = plain_connect(db_path)
    try:
        row = conn.execute(
            "SELECT url, verdict, surveyed_at, skip_reason FROM investigations WHERE id = ?",
            (_hash(url, surveyed_by, lens),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# A file:line citation: a path-ish token with an extension, then :N (or :N-M).
_FILE_LINE_RE = re.compile(r"[\w./-]+\.[A-Za-z0-9]+:\d+")


def reason_cites_evidence(reason: str) -> bool:
    """True if a skip reason cites a [[pattern]] or a file:line (issue #1293).

    The investigating guide also accepts an availability fact (dead repo, licence,
    closed source) as grounds, which no pattern can recognise reliably, so a False
    here is a prompt to check, never a refusal.
    """
    return "[[" in reason or bool(_FILE_LINE_RE.search(reason))


def _url_slug(url: str) -> str:
    """The repo-ish name an eval doc is filed under: the URL's last path segment,
    lowercased, `.git` dropped. `https://github.com/x/precisionMemBench` ->
    `precisionmembench`."""
    tail = url.rstrip("/").rsplit("/", 1)[-1].lower()
    return tail[:-4] if tail.endswith(".git") else tail


def _url_key(url: str) -> str:
    """A URL as `--skip-url` compares it: case, a trailing `/` and `.git` do not
    matter, so a spare list pasted in a slightly different form still spares."""
    u = url.strip().rstrip("/").lower()
    return u[:-4] if u.endswith(".git") else u


def _eval_doc_slugs(dirs: Iterable[Path]) -> list[str]:
    """Lowercased stems of `*evaluation*.md` files in *dirs* (issue #549)."""
    stems: list[str] = []
    for d in dirs:
        for f in Path(d).glob("*.md"):
            if "evaluation" in f.stem.lower():
                stems.append(f.stem.lower())
    return stems


def _has_eval_doc(url: str, stems: list[str]) -> bool:
    """True when an eval doc is named for this URL's slug: `<slug>-evaluation*.md`,
    or `<slug>-<more>-evaluation.md` (an owner suffix). Deliberately loose — a
    false match SPARES a row, which is the safe direction for a destructive sweep."""
    slug = _url_slug(url)
    return bool(slug) and any(s == slug or s.startswith(f"{slug}-") for s in stems)


def sweep_legacy_verdicts(
    db_path: Path,
    *,
    before: str,
    dry_run: bool = True,
    spare_urls: Iterable[str] = (),
    eval_doc_dirs: Iterable[Path] = (),
) -> dict:
    """Retire pre-techne#428 auto-assigned verdicts to 'unknown'.

    SPARING (issue #549). Date alone cannot express the legacy set: measured on
    2026-08-11, ten genuine operator full-evals sat among 205 legacy rows across
    both June and July, so every `before` value destroyed some. The real predicate
    is "full-eval with no eval doc", so a row is spared when its URL is in
    *spare_urls* or an eval doc for its slug exists in any of *eval_doc_dirs*.
    Spared rows are returned with the reason, so a dry run shows what is kept.

    Every `full-eval` written before the fix was assigned by the ingester purely
    because the markdown carried patterns, so none of those values is a judgment.
    Leaving them reads as 202 deliberate full-evals; 'unknown' is the honest state.

    Scoped by `before` (a surveyed_at date) rather than sweeping the whole table,
    so a genuine operator-supplied full-eval recorded after the fix is never
    clobbered.

    Caveat, INVERTED BY techne#429: re-ingest no longer overwrites surveyed_at
    when it declines the verdict, and the ordinary ingest path IS the declined
    path. So a legacy row that has since been re-investigated now keeps its
    ORIGINAL date and falls INSIDE the sweep rather than outside it. Rows already
    re-ingested BEFORE that fix still carry the newer date and still need
    `set_verdict` — and a genuine post-fix full-eval is BYTE-IDENTICAL to a
    pre-fix clobbered legacy row in both columns, so there is no way to tell the
    two populations apart. Do not widen `before` to catch the stragglers.

    dry_run=True by default: this rewrites the operator's corpus, so the caller
    has to ask for it twice.
    """
    if not _ISO_DATE_RE.match(before):
        raise ValueError(
            f"--before must be an ISO date (YYYY-MM-DD); got {before!r}. "
            "It is this function's only safety property — an unparseable value would "
            "compare as a plain string and could select every row in the table."
        )

    conn = plain_connect(db_path)
    try:
        # Take the write lock BEFORE the read (BEGIN IMMEDIATE): with the default
        # isolation a bare SELECT opens no transaction, so a concurrent writer can
        # land between the SELECT that builds the preview/audit list and the UPDATE
        # that rewrites those rows — reporting rows as swept that were changed by
        # someone else. Same read→write TOCTOU the rest of this codebase already
        # guards (curated.py:105, cards.py:106, documents.py:71); reproducing it in
        # a *destructive* path is worse than in an idempotent one.
        conn.execute("BEGIN IMMEDIATE")
        candidates = conn.execute(
            "SELECT url, surveyed_at FROM investigations "
            "WHERE verdict = 'full-eval' AND surveyed_at < ? ORDER BY surveyed_at",
            (before,),
        ).fetchall()
        skip = {_url_key(u) for u in spare_urls}
        stems = _eval_doc_slugs(eval_doc_dirs)
        rows, spared = [], []
        for r in candidates:
            if _url_key(r[0]) in skip:
                spared.append({"url": r[0], "reason": "skip-url"})
            elif _has_eval_doc(r[0], stems):
                spared.append({"url": r[0], "reason": "eval-doc"})
            else:
                rows.append(r)
        applied = 0
        if not dry_run and rows:
            # Row by row, re-checking the scope, so a spared URL can never be
            # caught by a set-based UPDATE.
            cur = conn.executemany(
                "UPDATE investigations SET verdict = ? "
                "WHERE verdict = 'full-eval' AND surveyed_at < ? AND url = ?",
                [(UNKNOWN_VERDICT, before, u) for u in dict.fromkeys(r[0] for r in rows)],
            )
            # rowcount from the UPDATE, not len(rows) from the SELECT: the audit
            # number for a destructive rewrite must be measured against the write
            # that happened, not the read that predicted it.
            applied = cur.rowcount
        conn.commit()
        return {
            "matched": len(rows),
            "applied": applied,
            "before": before,
            "urls": [r[0] for r in rows],
            "spared": spared,
        }
    finally:
        conn.close()


def add_pattern(
    db_path: Path,
    *,
    investigation_id: str,
    name: str,
    source_url: str,
    body: str,
    source_path: str | None = None,
    lens_kind: str | None = None,
    confidence: str = "medium",
) -> tuple[str, bool]:
    """Insert a lifted pattern (content-hash id, idempotent). Returns (id, stored)."""
    pid = _hash(name, source_url, body)
    conn = plain_connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM patterns WHERE id = ?", (pid,)).fetchone():
            return pid, False
        conn.execute(
            """INSERT INTO patterns
                 (id, investigation_id, name, source_url, source_path, lens_kind, body, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, investigation_id, name, source_url, source_path, lens_kind, body, confidence),
        )
        conn.commit()
        return pid, True
    finally:
        conn.close()


def _name_index(conn) -> dict[str, str]:
    """Map normalized pattern name → id for resolution. On a name collision the
    first (lowest rowid) wins — deterministic, and name dupes are rare."""
    index: dict[str, str] = {}
    for row in conn.execute("SELECT id, name FROM patterns WHERE status = 'active' ORDER BY rowid"):
        key = _norm(row["name"])
        index.setdefault(key, row["id"])
    return index


def add_relation(
    db_path: Path, *, src_pattern_id: str, dst_name: str, kind: str = "related"
) -> tuple[str | None, bool]:
    """Record one pattern→pattern edge, resolving *dst_name* against active
    patterns. Returns (dst_pattern_id_or_None, resolved). Idempotent on
    (src, dst_name, kind); re-running upgrades unresolved→resolved as the corpus
    grows. A self-edge (dst resolves back to src) is dropped."""
    conn = plain_connect(db_path)
    try:
        dst_id = _name_index(conn).get(_norm(dst_name))
        if dst_id == src_pattern_id:  # self-reference — not an edge
            return src_pattern_id, True
        status = "resolved" if dst_id else "unresolved"
        conn.execute(
            """INSERT INTO pattern_relations
                 (src_pattern_id, dst_name, dst_pattern_id, kind, resolution_status)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(src_pattern_id, dst_name, kind) DO UPDATE SET
                 dst_pattern_id = excluded.dst_pattern_id,
                 resolution_status = excluded.resolution_status""",
            (src_pattern_id, dst_name, dst_id, kind, status),
        )
        conn.commit()
        return dst_id, dst_id is not None
    finally:
        conn.close()


def add_apply(db_path: Path, *, pattern_id: str, target: str, origin: str = "body") -> bool:
    """Tag a pattern as applying to a project target (see known_targets()).
    Idempotent: an existing row for the pair is left as it is, origin included.
    Returns True if a new row was inserted. Curation goes through `tag_pattern`."""
    conn = plain_connect(db_path)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pattern_applies (pattern_id, target, origin) VALUES (?, ?, ?)",
            (pattern_id, target, origin),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def record_outcome(
    db_path: Path,
    *,
    pattern_id: str,
    ref: str,
    kind: str = "issue",
    state: str = "filed",
    note: str | None = None,
) -> bool:
    """Record what a pattern BECAME — the return path of provenance (techne#426).

    A pattern already carries where it came from (`source_url`, `investigation_id`).
    This carries the other direction: the issue/PR/commit it turned into and whether
    that shipped. *ref* is free-form but conventionally `owner/repo#123` for issues.

    Upserts on (pattern_id, ref) so re-recording promotes state — the normal
    lifecycle is 'filed' at file-time, then 'shipped' when the issue closes as
    completed. Returns True if a row was inserted or its state/note changed.
    Raises ValueError on unknown pattern id."""
    if state not in _OUTCOME_STATES:
        raise ValueError(
            f"unknown outcome state: {state} (expected one of {sorted(_OUTCOME_STATES)})"
        )
    conn = plain_connect(db_path)
    try:
        row = conn.execute("SELECT status FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown pattern id: {pattern_id}")
        if row["status"] == "retired":
            # The mirror of retire()'s outcome guard (Prudence on #1346): a retired
            # pattern with an outcome is the state retire() refuses, and `outcomes`
            # counts active patterns only, so the outcome would vanish from it.
            raise ValueError(
                f"{pattern_id} is retired; reinstate it with `tag --status active` "
                "before recording what it became"
            )
        before = conn.execute(
            "SELECT state, note, kind FROM pattern_outcomes WHERE pattern_id = ? AND ref = ?",
            (pattern_id, ref),
        ).fetchone()
        now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        conn.execute(
            "INSERT INTO pattern_outcomes (pattern_id, ref, kind, state, recorded_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(pattern_id, ref) DO UPDATE SET"
            "   state = excluded.state,"
            "   kind = excluded.kind,"
            "   note = COALESCE(excluded.note, pattern_outcomes.note),"
            # Refreshed on promotion: `filed` -> `shipped` is a NEW event, and leaving
            # the original timestamp made `[shipped] … (date)` show the date it was filed.
            "   recorded_at = excluded.recorded_at",
            (pattern_id, ref, kind, state, now, note),
        )
        conn.commit()
        if before is None:
            return True
        # `kind` counts as a change too — it was written but not reported, so the CLI
        # said "unchanged" for a row it had just modified.
        return before[0] != state or before[2] != kind or (note is not None and before[1] != note)
    finally:
        conn.close()


def outcomes_for_pattern(db_path: Path, pattern_id: str) -> list[dict]:
    """Every recorded outcome for one pattern, newest first."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ref, kind, state, recorded_at, note FROM pattern_outcomes"
            " WHERE pattern_id = ? ORDER BY recorded_at DESC",
            (pattern_id,),
        ).fetchall()
        return [
            {"ref": r[0], "kind": r[1], "state": r[2], "recorded_at": r[3], "note": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def outcome_stats(db_path: Path) -> dict:
    """Corpus-level conversion: did this investigation programme actually pay?

    `realized` counts DISTINCT patterns with at least one outcome — not outcome
    rows — so a pattern that spawned three issues counts once. `shipped` is the
    subset with at least one outcome in state 'shipped'. The rate is the number
    that matters: what fraction of the corpus produced downstream work."""
    conn = plain_connect(db_path)
    try:
        active = conn.execute("SELECT COUNT(*) FROM patterns WHERE status = 'active'").fetchone()[0]
        # JOIN + the SAME status filter as the denominator. Without it a superseded
        # pattern carrying an outcome counted in the numerator only, so the rate could
        # exceed 1.0 (demonstrated: realized_rate 2.0). `unrealized_patterns` below
        # already filters, so the two read as complements while counting different
        # populations — the more misleading half of the bug.
        realized = conn.execute(
            "SELECT COUNT(DISTINCT o.pattern_id) FROM pattern_outcomes o"
            " JOIN patterns p ON p.id = o.pattern_id WHERE p.status = 'active'"
        ).fetchone()[0]
        shipped = conn.execute(
            "SELECT COUNT(DISTINCT o.pattern_id) FROM pattern_outcomes o"
            " JOIN patterns p ON p.id = o.pattern_id"
            " WHERE p.status = 'active' AND o.state = 'shipped'"
        ).fetchone()[0]
        by_state = dict(
            conn.execute("SELECT state, COUNT(*) FROM pattern_outcomes GROUP BY state").fetchall()
        )
        return {
            "active_patterns": active,
            "realized_patterns": realized,
            "shipped_patterns": shipped,
            "realized_rate": round(realized / active, 3) if active else 0.0,
            "shipped_rate": round(shipped / active, 3) if active else 0.0,
            "outcome_rows_by_state": by_state,
        }
    finally:
        conn.close()


def unrealized_patterns(db_path: Path, *, topic: str | None = None, limit: int = 100) -> list[dict]:
    """Active patterns with no recorded outcome — the candidate queue.

    These are patterns we lifted and then never turned into anything. Not
    inherently waste (a pattern can be reference material), but this is the list
    to read when asking "what did we learn and never act on?"."""
    conn = plain_connect(db_path)
    try:
        sql = (
            "SELECT p.id, p.name, p.topic, p.source_url FROM patterns p"
            " LEFT JOIN pattern_outcomes o ON o.pattern_id = p.id"
            " WHERE p.status = 'active' AND o.pattern_id IS NULL"
        )
        args: list = []
        if topic:
            sql += " AND p.topic = ?"
            args.append(topic)
        sql += " ORDER BY p.created_at DESC LIMIT ?"
        args.append(limit)
        return [
            {"id": r[0], "name": r[1], "topic": r[2], "source_url": r[3]}
            for r in conn.execute(sql, args).fetchall()
        ]
    finally:
        conn.close()


def supersede(
    db_path: Path,
    *,
    old_id: str,
    new_id: str,
    absorbed: str | None = None,
    invalid_at: str | None = None,
) -> bool:
    """Mark *old_id* as superseded by *new_id*, atomically (techne#308, closes #307).

    In ONE transaction: stamp `old.status='superseded'`, `old.superseded_by=new_id`,
    `old.absorbed=<salvage note>` (kept if not re-supplied), record the validity
    window (techne#306) — `old.invalid_at` and `new.valid_at` get the SAME handoff
    timestamp (default now; *invalid_at* overrides it) so the read-path can ask
    "what was current as of T?" — AND upsert a resolved `supersedes` edge (old → new)
    so the relation graph reflects it. Both patterns must exist and differ, and the
    successor must not be retired (issue #1345). Re-running the same supersession is
    a no-op on the window (COALESCE keeps the original handoff time), but it is
    refused once the successor has since been retired. Returns True if the row
    changed. `unsupersede` undoes it.

    Columns are the source of truth for supersession; the edge is written by the
    same atomic op purely so the graph view (techne#308 → #305) stays consistent."""
    if old_id == new_id:
        raise ValueError("a pattern cannot supersede itself")
    conn = plain_connect(db_path)
    try:
        old = conn.execute("SELECT id FROM patterns WHERE id = ?", (old_id,)).fetchone()
        new = conn.execute(
            "SELECT id, name, status FROM patterns WHERE id = ?", (new_id,)
        ).fetchone()
        if old is None:
            raise ValueError(f"unknown pattern id (old): {old_id}")
        if new is None:
            raise ValueError(f"unknown pattern id (new): {new_id}")
        if new["status"] == "retired":
            raise ValueError(
                f"{new_id} is retired; a supersession needs a live successor (issue #1345)"
            )
        with conn:  # atomic: all writes commit together, or none do
            # One timestamp for the handoff: old stops being current and new becomes
            # current at the same instant (techne#306 validity window).
            ts = invalid_at or conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
            cur = conn.execute(
                """UPDATE patterns
                     SET status = 'superseded', superseded_by = ?,
                         absorbed = COALESCE(?, absorbed),
                         invalid_at = COALESCE(invalid_at, ?)
                   WHERE id = ?""",
                (new_id, absorbed, ts, old_id),
            )
            conn.execute(
                "UPDATE patterns SET valid_at = COALESCE(valid_at, ?) WHERE id = ?",
                (ts, new_id),
            )
            conn.execute(
                """INSERT INTO pattern_relations
                     (src_pattern_id, dst_name, dst_pattern_id, kind, resolution_status)
                   VALUES (?, ?, ?, 'supersedes', 'resolved')
                   ON CONFLICT(src_pattern_id, dst_name, kind) DO UPDATE SET
                     dst_pattern_id = excluded.dst_pattern_id,
                     resolution_status = excluded.resolution_status""",
                (old_id, new["name"], new_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def retire(
    db_path: Path,
    *,
    pattern_id: str,
    reason: str,
    invalid_at: str | None = None,
) -> bool:
    """Retire *pattern_id*: it leaves the active set with no successor (issue #1345).

    `superseded` needs a successor, so a pattern that is simply not worth carrying (a
    docs-only source, a standard idiom, a mechanism that is only prose) had no way
    out. This stamps `status='retired'`, the reason, and the end of its validity
    window in one write. Search, topic, applies and the counts filter on
    `status = 'active'`, so the pattern drops out of them exactly as a superseded one
    does. `show` still prints it with the reason, and `related_patterns` keeps its
    edges and marks the status.

    Refused, with the reason in the error:
    - an empty reason: a retirement nobody can explain is the uncited skip the
      investigating guide warns about;
    - a superseded pattern: it already left, and has a successor that says why;
    - a pattern with a recorded outcome: something was filed or shipped from it, so
      it is evidence of use, and retiring it would hide that from `outcomes`.

    Re-retiring with a new reason updates the reason and keeps the original date.
    Returns True if the row changed."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError('a retirement needs a reason: pass --reason "<why>" (issue #1345)')
    conn = plain_connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, superseded_by FROM patterns WHERE id = ?", (pattern_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown pattern id: {pattern_id}")
        if row["status"] == "superseded":
            raise ValueError(
                f"{pattern_id} is already superseded by {row['superseded_by']}; "
                "retire is for a pattern with no successor"
            )
        have_outcomes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pattern_outcomes'"
        ).fetchone()
        if have_outcomes:
            n = conn.execute(
                "SELECT COUNT(*) FROM pattern_outcomes WHERE pattern_id = ?", (pattern_id,)
            ).fetchone()[0]
            if n:
                raise ValueError(
                    f"{pattern_id} has {n} recorded outcome(s); a pattern something was "
                    "filed or shipped from is not retired (see `techne architect outcomes`)"
                )
        with conn:
            ts = invalid_at or conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
            cur = conn.execute(
                """UPDATE patterns
                     SET status = 'retired', retired_reason = ?,
                         invalid_at = COALESCE(invalid_at, ?)
                   WHERE id = ?
                     AND (status != 'retired' OR COALESCE(retired_reason, '') != ?)""",
                (reason, ts, pattern_id, reason),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def unsupersede(db_path: Path, *, pattern_id: str) -> bool:
    """Undo a supersession: make *pattern_id* active again (issue #1348).

    `tag --status active` used to flip only `status`, which left a row that was
    active by status but superseded by every other measure: `invalid_at` still set,
    so `current_as_of` called it not current, `superseded_by` still naming a
    successor, and the resolved `supersedes` edge still in the graph. In one
    transaction this clears `superseded_by` and `invalid_at` and deletes the
    pattern's outbound `supersedes` edge. `absorbed` stays as history: `show`
    prints it only for a superseded row. The successor's `valid_at` is left alone,
    since it did become current at that time. Returns True if the row changed.
    """
    conn = plain_connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM patterns WHERE id = ?", (pattern_id,)).fetchone() is None:
            raise ValueError(f"unknown pattern id: {pattern_id}")
        with conn:
            cur = conn.execute(
                """UPDATE patterns
                     SET status = 'active', superseded_by = NULL, invalid_at = NULL
                   WHERE id = ? AND status = 'superseded'""",
                (pattern_id,),
            )
            if cur.rowcount:
                conn.execute(
                    "DELETE FROM pattern_relations"
                    " WHERE src_pattern_id = ? AND kind = 'supersedes'",
                    (pattern_id,),
                )
        return cur.rowcount > 0
    finally:
        conn.close()


def tag_pattern(
    db_path: Path,
    pattern_id: str,
    *,
    topic: str | None = None,
    status: str | None = None,
    superseded_by: str | None = None,
    absorbed: str | None = None,
    applies: list[str] | tuple[str, ...] = (),
    no_applies: list[str] | tuple[str, ...] = (),
    reason: str | None = None,
) -> bool:
    """Curation mutator for a single pattern (techne#308 `architect tag`).

    `status='retired'` (issue #1345) routes to `retire()` and needs *reason*; *reason*
    is refused with any other status, so it cannot land on a live row by accident.

    *applies* / *no_applies* (issue #1293) set or remove project targets by hand, for
    patterns whose body never says "where to apply in <target>". See `set_applies`.

    If *superseded_by* is given, routes to the atomic `supersede()` (status is
    forced to 'superseded' and the `supersedes` edge written); a *topic* supplied
    alongside is applied too. Otherwise updates whichever of topic/status/absorbed
    are provided. Returns True if a row changed. Raises ValueError on unknown id.

    `status='superseded'` without *superseded_by* is refused (techne#880). It made a
    row `stats()` counts as superseded but `topic_show` lists under no parent, so
    the summary said a pattern was superseded and nothing could say by what. The
    status is a consequence of a supersession, not an independent fact, so the only
    way to reach it is the atomic path. `status='active'` alongside *superseded_by*
    is refused for the same reason: it asks for both states at once."""
    if status == "superseded" and superseded_by is None:
        raise ValueError(
            "a pattern is superseded BY another: use --superseded-by <new-id> "
            "(it sets the status too); a bare --status superseded leaves the row "
            "with no successor (techne#880)"
        )
    if status == "active" and superseded_by is not None:
        raise ValueError("--status active contradicts --superseded-by; pass one or the other")
    if status == "retired" and superseded_by is not None:
        raise ValueError(
            "--status retired is for a pattern with no successor; drop it to supersede instead"
        )
    if reason is not None and status != "retired":
        raise ValueError("--reason goes with --status retired")
    if status == "retired" and absorbed is not None:
        raise ValueError(
            "--absorbed is the salvage note for a supersession; a retirement takes --reason"
        )
    conn = plain_connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM patterns WHERE id = ?", (pattern_id,)).fetchone() is None:
            raise ValueError(f"unknown pattern id: {pattern_id}")
    finally:
        conn.close()

    # Validated before anything is written, so a typo in one target does not leave
    # the topic or supersession applied and the applies change refused.
    resolved = _resolve_applies(applies, no_applies)
    applies_changed = False

    if status == "retired":
        changed = retire(db_path, pattern_id=pattern_id, reason=reason or "")
        if topic is not None:
            changed = _set_fields(db_path, pattern_id, {"topic": topic}) or changed
        if resolved:
            changed = set_applies(db_path, pattern_id, resolved) or changed
        return changed

    if superseded_by is not None:
        changed = supersede(db_path, old_id=pattern_id, new_id=superseded_by, absorbed=absorbed)
        if topic is not None:
            changed = _set_fields(db_path, pattern_id, {"topic": topic}) or changed
        if resolved:
            applies_changed = set_applies(db_path, pattern_id, resolved)
        return changed or applies_changed
    if resolved:
        applies_changed = set_applies(db_path, pattern_id, resolved)

    unsuperseded = False
    if status == "active" and _status_of(db_path, pattern_id) == "superseded":
        unsuperseded = unsupersede(db_path, pattern_id=pattern_id)
        status = None  # done; the rest of the fields still apply below

    fields: dict[str, str | None] = {}
    if topic is not None:
        fields["topic"] = topic
    if status is not None:
        fields["status"] = status
        if status == "active" and _status_of(db_path, pattern_id) == "retired":
            # Un-retiring reopens the validity window: `invalid_at` NULL means "still
            # current", and `current_as_of` reads that column, not `status`. The
            # reason stays as history; nothing reads it on an active row.
            fields["invalid_at"] = None
    if absorbed is not None:
        fields["absorbed"] = absorbed
    if not fields:
        return applies_changed or unsuperseded
    return _set_fields(db_path, pattern_id, fields) or applies_changed or unsuperseded


def _resolve_applies(
    applies: list[str] | tuple[str, ...], no_applies: list[str] | tuple[str, ...]
) -> dict[str, str]:
    """Canonicalise hand-set targets to {target: 'curated' | 'excluded'} (issue #1293).

    An added target must be configured: `tag` is where a typo would otherwise mint a
    target the read side has never heard of, which is the failure `_apply_targets`
    already refuses for bodies. A removed target need NOT be configured, because the
    common reason to remove one is that the project was retired from the list.
    Naming the same target on both sides is refused rather than resolved by order.
    """
    aliases = target_aliases()

    def canon(t: str) -> str:
        t = t.strip().lower()
        return aliases.get(t, t)

    add = [canon(t) for t in applies if t.strip()]
    drop = [canon(t) for t in no_applies if t.strip()]
    both = sorted(set(add) & set(drop))
    if both:
        raise ValueError(f"--applies and --no-applies both name: {', '.join(both)}")
    if add:
        allowed = known_targets()
        unknown = sorted(t for t in set(add) if t not in allowed)
        if unknown:
            raise ValueError(
                f"not a configured target: {', '.join(unknown)} "
                f"(architect.pattern_targets: {', '.join(sorted(allowed))})"
            )
    return {**{t: "curated" for t in add}, **{t: "excluded" for t in drop}}


def set_applies(db_path: Path, pattern_id: str, origins: dict[str, str]) -> bool:
    """Write hand-set applies rows (issue #1293). True if any row changed.

    'curated' adds the target, or turns an earlier exclusion back into a tag; a
    body-derived row it meets is left as 'body', since it already applies. 'excluded'
    keeps the row and marks it, rather than deleting it, because a deleted pair is
    re-derived by the next `backfill_graph` whenever the body still names it.
    """
    conn = plain_connect(db_path)
    try:
        changed = 0
        for target, origin in origins.items():
            if origin == "curated":
                cur = conn.execute(
                    """INSERT INTO pattern_applies (pattern_id, target, origin)
                       VALUES (?, ?, 'curated')
                       ON CONFLICT(pattern_id, target) DO UPDATE SET origin = 'curated'
                       WHERE pattern_applies.origin = 'excluded'""",
                    (pattern_id, target),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO pattern_applies (pattern_id, target, origin)
                       VALUES (?, ?, 'excluded')
                       ON CONFLICT(pattern_id, target) DO UPDATE SET origin = 'excluded'
                       WHERE pattern_applies.origin != 'excluded'""",
                    (pattern_id, target),
                )
            changed += cur.rowcount
        conn.commit()
        return changed > 0
    finally:
        conn.close()


def applies_of(db_path: Path, pattern_id: str) -> list[dict]:
    """Every applies row for one pattern, with its origin, excluded ones included,
    target order. What `architect tag` prints after a change."""
    conn = plain_connect(db_path)
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT target, origin FROM pattern_applies WHERE pattern_id = ? ORDER BY target",
                (pattern_id,),
            )
        ]
    finally:
        conn.close()


def _status_of(db_path: Path, pattern_id: str) -> str | None:
    conn = plain_connect(db_path)
    try:
        row = conn.execute("SELECT status FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def _set_fields(db_path: Path, pattern_id: str, fields: dict[str, str | None]) -> bool:
    """Update a whitelisted set of columns on one pattern. Column names are from a
    fixed internal allowlist (never user input), so the f-string is injection-safe."""
    allowed = {"topic", "status", "absorbed", "superseded_by", "invalid_at"}
    cols = [c for c in fields if c in allowed]
    if not cols:
        return False
    assignments = ", ".join(f"{c} = ?" for c in cols)
    values = [fields[c] for c in cols] + [pattern_id]
    conn = plain_connect(db_path)
    try:
        cur = conn.execute(f"UPDATE patterns SET {assignments} WHERE id = ?", values)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def backfill_graph(db_path: Path) -> dict:
    """Populate pattern_relations + pattern_applies from the signals already in
    pattern bodies (techne#87 P2). Parses `[[wikilinks]]` → relations and
    `**in <target>**` → applies for every active pattern, in one transaction.

    Idempotent and incremental: edges upsert on (src, dst_name, kind), so a
    re-run after new patterns land upgrades previously-`unresolved` edges to
    `resolved`. Returns counts for reporting."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, body FROM patterns WHERE status = 'active'"
        ).fetchall()
        index = _name_index(conn)
        # Resolved ONCE for the whole backfill: _apply_targets would otherwise
        # re-read config per row, and this loop runs over every active pattern.
        _allowed_targets = known_targets()
        _aliases = target_aliases()
        rel_total = rel_resolved = applies_total = 0
        for r in rows:
            src = r["id"]
            for dst_name in _wikilink_targets(r["body"]):
                dst_id = index.get(_norm(dst_name))
                if dst_id == src:  # self-reference
                    continue
                status = "resolved" if dst_id else "unresolved"
                conn.execute(
                    """INSERT INTO pattern_relations
                         (src_pattern_id, dst_name, dst_pattern_id, kind, resolution_status)
                       VALUES (?, ?, ?, 'related', ?)
                       ON CONFLICT(src_pattern_id, dst_name, kind) DO UPDATE SET
                         dst_pattern_id = excluded.dst_pattern_id,
                         resolution_status = excluded.resolution_status""",
                    (src, dst_name, dst_id, status),
                )
                rel_total += 1
                rel_resolved += int(dst_id is not None)
            for target in _apply_targets(r["body"], allowed=_allowed_targets, aliases=_aliases):
                # OR IGNORE is what keeps curation (issue #1293): a pair already held
                # as 'curated' stays curated, and one held as 'excluded' stays out,
                # so `tag --no-applies` survives every later backfill.
                conn.execute(
                    "INSERT OR IGNORE INTO pattern_applies (pattern_id, target) VALUES (?, ?)",
                    (src, target),
                )
                applies_total += 1
        conn.commit()
        # All counts are TOTALS over the corpus (not per-run deltas), so the
        # numbers are stable across idempotent re-runs.
        return {
            "relations": rel_total,
            "resolved": rel_resolved,
            "unresolved": rel_total - rel_resolved,
            "applies": applies_total,
        }
    finally:
        conn.close()


def related_patterns(db_path: Path, pattern_id: str) -> dict:
    """Edges touching *pattern_id*, both directions. Returns
    {'outbound': [...], 'inbound': [...]}. Outbound includes unresolved edges
    (dst_id NULL, dst_name carries the dangling reference); inbound is resolved
    edges only (an unresolved edge has no known source-of-truth target).

    Edges to and from non-active patterns are kept, not filtered, and each row
    carries the other end's status (`dst_status` / `src_status`). After an
    `architect backfill`, an outbound edge to a non-active pattern is re-derived as
    unresolved (backfill resolves names against active patterns only), so it reads
    `(unresolved) <name>` with `dst_status` None until that pattern is live again. A `supersedes`
    edge from a superseded row is meant to be seen, and a link to a retired pattern
    should read as one rather than as a live lead (issue #1345, Prudence on #1346)."""
    conn = plain_connect(db_path)
    try:
        outbound = [
            dict(r)
            for r in conn.execute(
                """SELECT r.dst_name, r.dst_pattern_id, r.kind, r.resolution_status,
                          p.name AS dst_pattern_name, p.status AS dst_status
                   FROM pattern_relations r
                   LEFT JOIN patterns p ON p.id = r.dst_pattern_id
                   WHERE r.src_pattern_id = ?
                   ORDER BY r.resolution_status, r.dst_name""",
                (pattern_id,),
            )
        ]
        inbound = [
            dict(r)
            for r in conn.execute(
                """SELECT r.src_pattern_id, r.kind, p.name AS src_pattern_name,
                          p.status AS src_status
                   FROM pattern_relations r JOIN patterns p ON p.id = r.src_pattern_id
                   WHERE r.dst_pattern_id = ? ORDER BY p.name""",
                (pattern_id,),
            )
        ]
        return {"outbound": outbound, "inbound": inbound}
    finally:
        conn.close()


def target_counts(db_path: Path) -> list[dict]:
    """Per stored target: how many ACTIVE patterns are tagged with it, and how many
    retired (any other status). Active first, most-tagged first.

    One definition of "a target the store holds" for every surface (issue #1016).
    `techne architect targets` counted every `pattern_applies` row while
    `pattern_search` probed with `patterns_for_target`, which serves active
    patterns only, so a target whose patterns were all retired was "unknown" to
    MCP and "present" to the CLI.
    """
    conn = plain_connect(db_path)
    try:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT a.target,
                          SUM(p.status = 'active') AS active,
                          SUM(p.status != 'active') AS retired
                   FROM pattern_applies a JOIN patterns p ON p.id = a.pattern_id
                   WHERE a.origin != 'excluded'
                   GROUP BY a.target
                   ORDER BY active DESC, retired DESC, a.target"""
            )
        ]
    finally:
        conn.close()


def patterns_for_target(db_path: Path, target: str, *, limit: int = 100) -> list[dict]:
    """All active patterns tagged as applying to *target* (see known_targets()).

    Takes the target VERBATIM — no aliasing. A pre-rename spelling such as
    `limenal` therefore returns [] rather than `liminal`'s rows; canonicalisation
    happens on the WRITE side (`_apply_targets`), so the stored value is already
    canonical and a miss here means the caller asked for something real that is
    not tagged, which is a true answer (techne#674)."""
    conn = plain_connect(db_path)
    try:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT p.id, p.name, p.source_url, p.confidence
                   FROM pattern_applies a JOIN patterns p ON p.id = a.pattern_id
                   WHERE a.target = ? AND p.status = 'active' AND a.origin != 'excluded'
                   ORDER BY p.name LIMIT ?""",
                (target, limit),
            )
        ]
    finally:
        conn.close()


def topic_list(db_path: Path) -> list[dict]:
    """The topic INDEX (techne#597): every topic with its active-pattern count,
    plus an `(untopiced)` bucket for patterns not yet grouped. Count-desc so the
    crowded topics — the ones worth curating first — surface at the top."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT COALESCE(topic, '(untopiced)') AS topic, COUNT(*) AS count
               FROM patterns WHERE status = 'active'
               GROUP BY COALESCE(topic, '(untopiced)')
               ORDER BY count DESC, topic"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def topic_show(db_path: Path, topic: str) -> list[dict]:
    """The read-path for one topic (techne#597): active patterns in *topic*, each
    with its superseded predecessors folded in under a `superseded` list (their
    `absorbed` salvage notes carried along). `(untopiced)` selects the ungrouped
    bucket, matching `topic_list`."""
    conn = plain_connect(db_path)
    try:
        where = "topic IS NULL" if topic == "(untopiced)" else "topic = ?"
        params: tuple = () if topic == "(untopiced)" else (topic,)
        actives = conn.execute(
            f"""SELECT id, name, source_url, confidence FROM patterns
                WHERE status = 'active' AND {where} ORDER BY name""",
            params,
        ).fetchall()
        out = []
        for a in actives:
            superseded = conn.execute(
                """SELECT id, name, absorbed FROM patterns
                   WHERE status = 'superseded' AND superseded_by = ? ORDER BY name""",
                (a["id"],),
            ).fetchall()
            row = dict(a)
            row["superseded"] = [dict(s) for s in superseded]
            out.append(row)
        return out
    finally:
        conn.close()


def current_as_of(db_path: Path, when: str, *, topic: str | None = None) -> list[dict]:
    """Patterns that were CURRENT at timestamp *when* (techne#306 validity windows).

    A pattern is current at T when it had become valid — COALESCE(valid_at,
    created_at) <= T — and had not yet been superseded — invalid_at IS NULL OR
    invalid_at > T. *when* is a SQLite datetime string ('YYYY-MM-DD[ HH:MM:SS]').
    Optional *topic* scope ('(untopiced)' = the NULL bucket). This is the temporal
    read-path: "what was the current pattern for this topic as of D?"."""
    conn = plain_connect(db_path)
    try:
        clauses = [
            "COALESCE(valid_at, created_at) <= ?",
            "(invalid_at IS NULL OR invalid_at > ?)",
        ]
        params: list = [when, when]
        if topic is not None:
            if topic == "(untopiced)":
                clauses.append("topic IS NULL")
            else:
                clauses.append("topic = ?")
                params.append(topic)
        rows = conn.execute(
            f"""SELECT id, name, topic, status, valid_at, invalid_at FROM patterns
                WHERE {" AND ".join(clauses)} ORDER BY name""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def graph_data(db_path: Path, topic: str | None = None) -> dict:
    """Nodes + resolved edges for a relation-graph render (techne#305).

    Full store when *topic* is None, else scoped to that topic ('(untopiced)' =
    the NULL bucket). Each node carries `status` so a renderer can de-emphasize
    superseded ones. Edges are RESOLVED relations whose BOTH endpoints are in the
    node set — so a topic-scoped subgraph has no dangling edges. Unresolved edges
    are omitted (they point at a name with no node yet)."""
    conn = plain_connect(db_path)
    try:
        if topic is None:
            node_rows = conn.execute(
                "SELECT id, name, topic, status FROM patterns ORDER BY name"
            ).fetchall()
        else:
            where = "topic IS NULL" if topic == "(untopiced)" else "topic = ?"
            params: tuple = () if topic == "(untopiced)" else (topic,)
            node_rows = conn.execute(
                f"SELECT id, name, topic, status FROM patterns WHERE {where} ORDER BY name",
                params,
            ).fetchall()
        nodes = [dict(r) for r in node_rows]
        node_ids = {n["id"] for n in nodes}
        edges = [
            {"src": r["src_pattern_id"], "dst": r["dst_pattern_id"], "kind": r["kind"]}
            for r in conn.execute(
                """SELECT src_pattern_id, dst_pattern_id, kind FROM pattern_relations
                   WHERE resolution_status = 'resolved'"""
            )
            if r["src_pattern_id"] in node_ids and r["dst_pattern_id"] in node_ids
        ]
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.close()


def investigation_exists(
    db_path: Path, url: str, *, surveyed_by: str = "operator", lens: str = "infra"
) -> bool:
    """Lens-aware dedup probe — the fast SELECT the flat md couldn't do."""
    conn = plain_connect(db_path)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM investigations WHERE url = ? AND surveyed_by = ? AND lens = ?",
                (url, surveyed_by, lens),
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def investigations_for_url(db_path: Path, url: str) -> list[dict]:
    """All lenses/verdicts for a URL side by side — the comparison query."""
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT surveyed_by, lens, verdict, summary FROM investigations
               WHERE url = ? ORDER BY surveyed_by, lens""",
            (url,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_patterns(db_path: Path, query: str, *, limit: int = 20) -> list[dict]:
    """FTS over active patterns (name + body), topped up by an OR-of-terms pass.

    The corpus is queried in problem language ("resume work after a crash without
    losing the task") while patterns are titled metaphorically ("Rebuild-not-reference
    handle across durable-workflow step boundaries"). Under FTS5 implicit-AND every
    term must appear, so a natural-language question matches nothing and the empty
    result reads as "we have no pattern for this" while a directly on-point one sits
    in the store. Measured on the 477-pattern corpus (#557): 1/15 problem-phrased
    queries found their target, 14/15 returned zero; with this fallback, 14/15 and
    zero empties.

    Same #70 fallback that :func:`techne.core.search` and
    :func:`techne.core.codegraph.search_symbols` already apply: the OR-of-terms
    pass tops up the AND pass's pool when it falls short of `limit`, appending
    OR rows not already present (techne#1079). A query whose AND pass already
    fills `limit` is returned unchanged. This function does no reranking, so
    AND-then-OR ordering is unconditional.
    """
    from techne.core.search import fts_with_or_fallback, prepare_fts_query

    if prepare_fts_query(query) is None:  # nothing searchable: don't open the db (#143)
        return []
    groups = search_alias_groups()
    conn = plain_connect(db_path)
    try:

        def _match(match_str: str) -> list[dict]:
            match_str = _expand_aliases(match_str, groups)
            rows = conn.execute(
                """SELECT p.id, p.name, p.source_url, p.body, p.confidence,
                          bm25(patterns_fts) AS rank
                   FROM patterns_fts f JOIN patterns p ON p.rowid = f.rowid
                   WHERE patterns_fts MATCH ? AND p.status = 'active'
                   ORDER BY rank LIMIT ?""",
                (match_str, limit),
            ).fetchall()
            return [dict(r) for r in rows]

        # The AND pass, topped up by OR-of-terms when it falls short of `limit`
        # (#70, #392, techne#1079). Rows key on `id` — this table alone.
        return fts_with_or_fallback(query, _match, want=limit, key=lambda r: r["id"])
    finally:
        conn.close()


def get_pattern(db_path: Path, pattern_id: str) -> dict | None:
    conn = plain_connect(db_path)
    try:
        row = conn.execute("SELECT * FROM patterns WHERE id = ?", (pattern_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_patterns(db_path: Path, *, limit: int = 100) -> list[dict]:
    conn = plain_connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, name, source_url, confidence FROM patterns
               WHERE status = 'active' ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(db_path: Path) -> dict:
    conn = plain_connect(db_path)
    try:
        return {
            "investigations": conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0],
            # ACTIVE ONLY — and the excluded rows are reported beside it, not left
            # to be inferred. `patterns` has always meant "active patterns", but
            # nothing in the output said so, so a reader who checks it against a
            # plain `SELECT COUNT(*) FROM patterns` finds a gap and reasonably
            # concludes the counter is broken. It isn't; it was under-labelled.
            # Measured 2026-09-02: 555 here against 561 total, and the 6-row
            # difference read as a bug in the summary rather than as supersessions.
            # Same defect class as techne#787's `heading`->`label` rename — a name
            # that describes less than the value it holds.
            "patterns": conn.execute(
                "SELECT COUNT(*) FROM patterns WHERE status = 'active'"
            ).fetchone()[0],
            "patterns_superseded": conn.execute(
                "SELECT COUNT(*) FROM patterns WHERE status = 'superseded'"
            ).fetchone()[0],
            "patterns_retired": conn.execute(
                "SELECT COUNT(*) FROM patterns WHERE status = 'retired'"
            ).fetchone()[0],
            "skips": conn.execute(
                "SELECT COUNT(*) FROM investigations WHERE verdict LIKE 'skip%'"
            ).fetchone()[0],
        }
    finally:
        conn.close()


def retire_legacy_pattern_knowledge(techne_db_path: Path, *, dry_run: bool = False) -> int:
    """Delete the legacy `type='pattern'` rows from techne.db (techne#87 P2).

    Before the pattern store existed, lifted patterns were dumped into personal
    memory as `knowledge` rows with `type='pattern'`. They're now fully owned by
    architect.db, but recall/search don't filter by type — so these dupes leak
    *team* patterns into *personal* recall. They're superseded; delete them. Uses
    the migrated core connection so the knowledge FTS triggers stay consistent.
    Returns the count removed (or that *would* be removed under dry_run)."""
    from techne.core.database import connect

    conn = connect(techne_db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM knowledge WHERE type = 'pattern'").fetchone()[0]
        if not dry_run and n:
            conn.execute("DELETE FROM knowledge WHERE type = 'pattern'")
            conn.commit()
        return n
    finally:
        conn.close()


def export_patterns_md(db_path: Path) -> str:
    """Regenerate an architectural-patterns.md from architect.db — the shareable
    export (the md becomes a generated artifact; architect.db is canonical). Round-
    trips with ingest_patterns_md."""
    conn = plain_connect(db_path)
    try:
        pats = conn.execute(
            """SELECT p.name, p.source_url, p.body, i.surveyed_at
               FROM patterns p JOIN investigations i ON i.id = p.investigation_id
               WHERE p.status = 'active'
               ORDER BY i.surveyed_at, p.name"""
        ).fetchall()
        no_lifts = conn.execute(
            """SELECT url, surveyed_at, skip_reason FROM investigations
               WHERE verdict LIKE 'skip%' ORDER BY surveyed_at, url"""
        ).fetchall()
    finally:
        conn.close()

    out = [
        "# Architectural patterns",
        "",
        "Shared team corpus of design patterns lifted from other people's code. "
        "**How to use**: grep for a source URL before investigating it — if it's "
        "below, we've seen it. **To add one**: append a block using the *Template "
        "for new entries* at the bottom, then `git commit`. Load into your local "
        "store to query with `techne architect ingest patterns/architectural-patterns.md`.",
        "",
        "This file round-trips with the Architect store (`architect.db`): "
        "`architect ingest` loads it, `architect export` regenerates it. Hand-edits "
        "survive a regenerate **as long as you `ingest` before you `export`** (ingest "
        "pulls your additions into the store first). See `patterns/README.md`.",
        "",
        "---",
        "",
    ]
    for p in pats:
        out.append(f"## {p['name']}")
        out.append("")
        out.append(f"- **Source**: {p['source_url']}")
        out.append(f"- **Surveyed**: {p['surveyed_at']}")
        out.append("")
        out.append((p["body"] or "").strip())
        out.append("")
        out.append("---")
        out.append("")
    if no_lifts:
        out.append("## Surveyed (no patterns worth saving)")
        out.append("")
        for nl in no_lifts:
            reason = nl["skip_reason"] or "nothing to lift"
            out.append(f"- {nl['url']} — surveyed {nl['surveyed_at']} — {reason}")
        out.append("")

    # The format/schema, embedded so the file is self-documenting. This heading is
    # in import_md.SKIPPED_HEADINGS (and the body is indented), so it is NOT ingested
    # as a pattern.
    out += [
        "## Template for new entries",
        "",
        "Append a pattern as a block like this (parsed on `architect ingest`):",
        "",
        "    ## <Short pattern name>",
        "",
        "    - **Source**: <url> — <optional file:line or §section>",
        "    - **Surveyed**: <YYYY-MM-DD>",
        "",
        "    <What it does — 1–2 paragraphs.>",
        "",
        "    **Why interesting**: <the non-obvious design choice / trade-off>.",
        "",
        "    **Where to apply**: <where in our stack it fits>.",
        "",
        "    ---",
        "",
        "For a URL you investigated but found nothing to lift, add a bullet under "
        '"## Surveyed (no patterns worth saving)": `- <url> — surveyed <date> — <reason>`.',
        "",
    ]
    return "\n".join(out)


def ingest_patterns_md(
    db_path: Path,
    md_text: str,
    verdict: str | None = None,
    *,
    surveyed_by: str = "operator",
    lens: str = "infra",
) -> dict:
    """Seed architect.db from architectural-patterns.md (reuses import_md.parse):
    patterns → grouped by url into investigations + patterns;
    no-lift bullets → investigations(verdict=skip-with-flag). Idempotent.

    `verdict` is the operator's judgment for the URLs carrying patterns. When
    omitted it falls back to DEFAULT_INGEST_VERDICT ("patterns-only"), NOT the old
    hardcoded "full-eval" — see techne#428 and the VERDICTS comment above.

    No-lift bullets ignore the caller's `verdict` and write "skip-with-flag", but
    they do NOT write it unconditionally — that write is insert-only, so an
    operator who narrowed the judgment to "skip" via `reverdict` keeps it. The
    document's structure establishes that the URL was surveyed and nothing was
    lifted; it does not establish which of the two skip verdicts the operator
    means. (This paragraph previously claimed "unconditionally" and "correct by
    construction", contradicting the call-site comment 30 lines below in the very
    commit that made it insert-only — Prudence, PR #432.)

    KNOWN LIMITATION — verdicts have no provenance, so the earliest fallback wins
    forever, and two transitions are frozen (tracked in techne#433):
      * patterns → later demoted to a no-lift bullet: the row keeps its
        pattern-era verdict, because the no-lift write is insert-only. Pinned by
        test_demotion_to_no_lift_keeps_the_pattern_era_verdict so it changes
        deliberately rather than silently.
      * `--verdict` is per-INGEST, not per-URL, so passing one to correct a single
        URL also overwrites every other pattern-carrying URL in the same document,
        including ones whose judgment was set by `reverdict`. Use `reverdict` for
        single-row corrections.
    The opposite transition — a no-lift URL that later DOES yield a pattern — is
    handled, via `clears_skip`: a skip cannot survive a pattern being saved.
    """
    from techne.import_md import parse

    # `is None`, not falsy: `verdict or DEFAULT` silently coerced "" to the
    # default, so the empty string that upsert_investigation explicitly rejects
    # was quietly accepted on this path (Prudence, PR #432).
    stated = verdict is not None
    verdict = DEFAULT_INGEST_VERDICT if verdict is None else verdict
    if verdict not in VERDICTS:  # fail before any write, not partway through
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}; got {verdict!r}")

    parsed = parse(md_text)
    patterns_added = 0
    for p in parsed.patterns:
        inv_id = upsert_investigation(
            db_path,
            url=p.source_url,
            surveyed_at=p.surveyed_date,
            verdict=verdict,
            # Carried from the caller so `add` and `ingest` land on ONE row per URL.
            # These two are half of UNIQUE(url, surveyed_by, lens); letting either
            # fall back to its own default is how one URL becomes two investigations.
            surveyed_by=surveyed_by,
            lens=lens,
            # A stated --verdict overwrites; the fallback is insert-only, so a
            # judgment recorded via `reverdict` survives a later plain re-ingest.
            overwrite_verdict=stated,
            # …but this write proves the URL DID yield a pattern, so a stale skip
            # verdict must not survive it, or the row is counted as a skip while
            # owning a pattern.
            clears_skip=True,
        )
        _pid, stored = add_pattern(
            db_path, investigation_id=inv_id, name=p.name, source_url=p.source_url, body=p.body
        )
        patterns_added += int(stored)
        # Typed graph edges declared as md fields (techne#308). Resolution is
        # idempotent and upgrades unresolved→resolved on later re-ingests, same as
        # the wikilink `related` edges backfilled below.
        for kind, dst_name in p.relations:
            add_relation(db_path, src_pattern_id=_pid, dst_name=dst_name, kind=kind)
    for nl in parsed.no_lifts:
        upsert_investigation(
            db_path,
            url=nl.source_url,
            surveyed_at=nl.surveyed_date,
            verdict="skip-with-flag",
            skip_reason=nl.reason,
            surveyed_by=surveyed_by,
            lens=lens,
            # Insert-only here too. "Correct by construction" is only half true: the
            # section header proves the URL was surveyed and nothing was lifted, but
            # NOT whether the operator's judgment is `skip` or `skip-with-flag` —
            # and `skip` is offered in reverdict's choices. Left overwriting, this
            # was Important #2 surviving on the second call site: `reverdict
            # --verdict skip` then a plain re-ingest reverted it (Prudence, PR #432).
            overwrite_verdict=False,
        )
    # Materialize the relation/applies graph from the bodies we just stored, and
    # re-resolve any edges that earlier ingests left dangling (P2 write-path).
    graph = backfill_graph(db_path)
    return {
        "patterns_added": patterns_added,
        # SEEN, NOT ADDED — and the distinction is load-bearing, not pedantry.
        #
        # `patterns_added` counts WRITES, which dedup. A caller keying "was this file
        # understood?" on it gets the wrong answer for a RE-INGEST of an unchanged corpus:
        # everything parses, nothing is written, and the caller reports the file was
        # unreadable. Measured before this fix — a second `ingest` of a correct file
        # printed "no pattern sections found … must start with `## `", which is a false
        # statement about a file whose headings are right.
        #
        # That is the same defect the message was added to fix, reintroduced by the fix for
        # it, so the counters the message keys on now report what was PARSED.
        "patterns_seen": len(parsed.patterns),
        "no_lifts_seen": len(parsed.no_lifts),
        # SECTIONS THAT LOOKED LIKE PATTERNS AND WERE DROPPED (techne#754). Returned so a
        # caller can say which and why. Without this the command printed `patterns: 0` and
        # exited 0 on a file the operator had just written — the bold markers in
        # `- **Source**:` are load-bearing, `- Source:` parses as body text, and a
        # plausible first attempt therefore failed in the one way this project keeps
        # failing: silently, looking like success.
        # `label` + `kind`, not `heading` (techne#787): this list now carries
        # malformed no-lift BULLETS as well as pattern sections, and a key called
        # `heading` holding a bullet is a field whose name is a lie.
        "skipped": [
            {"label": sk.label, "kind": sk.kind, "reason": sk.reason} for sk in parsed.skipped
        ],
        "graph": graph,
        **stats(db_path),
    }


#: host -> how many leading path segments that host case-folds.
#:
#: NOT THE WHOLE PATH, which was this fix's own second defect (Prudence, #807 r2 I2).
#: `github.com` folds `owner/repo` because those live in GitHub's namespace; everything
#: deeper is served out of git, where tree paths and refs are byte-exact. Folding the
#: whole path relocated the wikipedia over-fold inside github:
#:     /o/r/blob/main/Setup.py  ==  /o/r/blob/main/setup.py   <- different files
#:     /o/r/tree/Release-1.0    ==  /o/r/tree/release-1.0     <- different refs
#: The measurement that justifies the entry was `owner/repo`, and `owner/repo` is what it
#: supports. Gists, raw.githubusercontent.com and enterprise hosts are absent on purpose:
#: absence under-folds, which costs a subagent, and guessing over-folds, which hides work.
_CASE_INSENSITIVE_PATH_SEGMENTS = {"github.com": 2}


def _canonical_url(url: str) -> str:
    """Fold the spellings of one URL that mean the same page.

    THE DEDUP CHECK IS ONLY AS GOOD AS THIS (techne#754). A prior investigation
    recorded as `https://github.com/Owner/Repo` must be found when the operator
    pastes `github.com/owner/repo/`, or the check reports "never seen" and a
    subagent is spent re-deriving a judgment already on the record — which is the
    whole failure the check exists to prevent.

    SUB-PATHS STAY DISTINCT on purpose: `owner/repo` must not match
    `owner/repo-extended`, so only a TRAILING slash is stripped, never a segment.
    """
    u = url.strip()
    for prefix in ("https://", "http://", "//"):
        if u.lower().startswith(prefix):
            u = u[len(prefix) :]
            break

    # ORDER MATTERS AND IT BIT TWICE (Prudence, #807 r3 I1 + nit 1). Each of these steps
    # was individually right and two of them ran in the wrong place:
    #
    #   - the fragment strip ran AFTER the query was partitioned off, so on any URL with a
    #     query the fragment rode along inside `query` and survived — and
    #     `?tab=readme-ov-file#readme` is exactly what a browser holds on a GitHub README
    #     tab, which is the paste this function exists to fold;
    #   - the query split ran AFTER host/path partition, so a URL with NO path put the
    #     whole query into `host`, which is then unconditionally lowercased — folding a
    #     query for every host, which is the over-fold direction.
    #
    # So: terminate at the fragment, remove the query, THEN split authority from path.
    # Nothing downstream can reach past a step that already ran.
    u = u.split("#", 1)[0]
    u, qmark, query = u.partition("?")
    host, slash, path = u.partition("/")

    host = host.lower()
    if host.startswith("www."):
        host = host[4:]

    depth = _CASE_INSENSITIVE_PATH_SEGMENTS.get(host)
    if depth:
        segs = path.split("/")
        path = "/".join([seg.lower() for seg in segs[:depth]] + segs[depth:])

    # A `.git` suffix and a trailing slash name the same repo.
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return (host + slash + path + qmark + query).rstrip("/")


def dedup_url(db_path: Path, url: str) -> dict:
    """Every prior contact with a URL — patterns AND verdicts, which differ in meaning.

    TWO TABLES, BECAUSE ONE OF THEM IS THE WHOLE POINT. A `skip-with-flag`
    investigation records a verdict and a reason but NO pattern row, so a
    patterns-only check misses exactly the set the dedup exists to catch: the URLs
    someone already looked at and deliberately passed over. Measured on a real
    corpus, 96 of 357 investigations had no pattern row, all of them skips.

    AND `architect search` CANNOT DO THIS. Its FTS index covers name and body only
    — `source_url` is never offered to it — so searching a URL returns "No patterns
    matched" for a repo that is demonstrably in the store. A confirmed miss, not a
    theoretical one, which is why this queries the columns directly.
    """
    canon = _canonical_url(url)
    conn = plain_connect(db_path)
    try:
        patterns = [
            {"id": r[0], "name": r[1], "source_url": r[2]}
            for r in conn.execute(
                "SELECT id, name, source_url FROM patterns WHERE source_url IS NOT NULL"
            ).fetchall()
            if _canonical_url(r[2] or "") == canon
        ]
        investigations = [
            {
                "url": r[0],
                "surveyed_by": r[1],
                "surveyed_at": r[2],
                "verdict": r[3],
                "skip_reason": r[4],
            }
            for r in conn.execute(
                "SELECT url, surveyed_by, surveyed_at, verdict, skip_reason FROM investigations"
            ).fetchall()
            if _canonical_url(r[0] or "") == canon
        ]
    finally:
        conn.close()

    return {
        "url": url,
        "canonical": canon,
        "seen": bool(patterns or investigations),
        "patterns": patterns,
        "investigations": investigations,
    }


def store_snapshot(db_path: Path) -> str:
    """A content digest of the corpus, for the read-only-subagent backstop.

    THE OPERATOR USES THE CONVERSE, WHICH IS WHY COVERAGE MATTERS (Prudence, #807 r1 I3).
    Nobody reads this as "a difference means something wrote". They read "no difference
    means nothing wrote", because that is the branch where they proceed with the
    subagent's findings. So every column this does NOT cover is a write the operator is
    told did not happen.

    The first version hashed id+name+body+source_url — 4 of the `patterns` table's 15
    columns — and its docstring claimed an id-only hash "catches an insert or a delete".
    It did not catch `status` -> 'superseded', which IS a delete: the row stops appearing
    in search while every hashed column stays byte-identical. It also missed a whole new
    `investigations` row, which is not out of scope by this module's own argument —
    `dedup_url` exists precisely because a `skip-with-flag` with no pattern row is the
    population worth protecting.

    So: every column of `patterns`, plus a second pass over `investigations`. Both are
    ordered by id so the digest is stable across row order. `pattern_relations`,
    `pattern_topics` and body-derived `pattern_applies` rows remain outside it — they are derived by
    `backfill_graph` from content this DOES cover, so a change there without a change
    here means the graph was rebuilt, not that the corpus was edited. That is a
    deliberate boundary rather than an oversight, and `techne architect snapshot --help`
    states what the digest covers so the converse can be read honestly.

    HAND-SET `pattern_applies` ROWS ARE COVERED (issue #1293). `architect tag
    --applies/--no-applies` writes 'curated' and 'excluded' rows that no backfill can
    re-derive, so they fail the exclusion test above exactly as `pattern_outcomes`
    did. Only those rows are hashed; 'body' rows stay out, so a backfill still moves
    nothing here.

    `pattern_outcomes` IS COVERED, and leaving it out was the same defect one table over
    (Prudence, #807 r2 I3). Nothing derives it — `record_outcome` writes it with a real
    INSERT and `techne architect realized` exposes that from the CLI — so it failed the
    exclusion test the other two pass. The tell was that the first version of this
    docstring listed `pattern_topics` among the exclusions: no such table exists, and the
    real sibling that got dropped from the list was the one that is not derived.
    """
    import hashlib

    h = hashlib.sha256()
    conn = plain_connect(db_path)
    try:
        for table in ("patterns", "investigations", "pattern_outcomes"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if not cols:
                # A missing table would silently NARROW the digest, which is the shape
                # this function exists to eliminate. Fold its absence in instead, so a
                # store that gains or loses one does not read as unchanged.
                h.update(f"{table}:absent".encode("utf-8"))
                h.update(b"\x1d")
                continue
            # Column names go into the digest too: adding a column is a schema change
            # that alters what "the whole row" means, and the snapshot should not read
            # as unchanged across it.
            h.update(f"{table}:{','.join(cols)}".encode("utf-8"))
            h.update(b"\x1e")
            order = "id" if "id" in cols else cols[0]
            select = ", ".join(f"COALESCE(CAST(\"{c}\" AS TEXT), '')" for c in cols)
            for row in conn.execute(f'SELECT {select} FROM "{table}" ORDER BY "{order}"'):
                for field in row:
                    h.update(str(field).encode("utf-8"))
                    h.update(b"\x1f")
            h.update(b"\x1d")
        applies_cols = {r[1] for r in conn.execute("PRAGMA table_info(pattern_applies)")}
        if "origin" in applies_cols:
            h.update(b"pattern_applies:hand-set\x1e")
            for row in conn.execute(
                "SELECT pattern_id, target, origin FROM pattern_applies "
                "WHERE origin != 'body' ORDER BY pattern_id, target"
            ):
                for field in row:
                    h.update(str(field).encode("utf-8"))
                    h.update(b"\x1f")
            h.update(b"\x1d")
    finally:
        conn.close()
    return h.hexdigest()
