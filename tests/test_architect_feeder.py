"""The architect's feeder that ships, and the silence it replaces (techne#754).

techne teaches four domain names. On a fresh install the architect — "what is worth
borrowing" — had no way in that came with the tool: `ingest` takes a markdown corpus in an
undocumented shape, and the skills that actually fill the store live in a separate repo.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main
from techne.core.architect import architect_db
from techne.core.database import init_db
from techne.import_md import parse


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    from techne.config import Config

    db = tmp_path / "techne.db"
    init_db(db).close()
    cfg = Config()
    cfg.db_path = db
    cfg.data_dir = tmp_path
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


# --------------------------------------------------------------------------- parser


def test_a_section_missing_its_bold_markers_is_reported_not_only_logged():
    """THE BOLD MARKERS ARE LOAD-BEARING AND THAT IS NOT GUESSABLE. `- Source:` parses as
    body text, so the section is dropped — and before techne#754 it was dropped with a
    `logger.warning` and nothing else, which is invisible to anyone running the CLI."""
    result = parse(
        "## A pattern\n\n- Source: https://example.com/x\n- Surveyed: 2026-01-01\n\nbody\n"
    )

    assert result.patterns == []
    # `.label`, not `.heading` (techne#787): the field was renamed when
    # SkippedSection was widened to carry malformed no-lift bullets too, where a
    # key called `heading` holding a bullet line would be a lie.
    assert [s.label for s in result.skipped] == ["A pattern"]
    assert "bold markers" in result.skipped[0].reason


def test_the_reason_names_which_line_is_missing():
    """"missing Source or Surveyed line" does not tell an operator staring at a file that
    plainly has both which spelling the parser wanted."""
    only_source = parse("## P\n\n- **Source**: https://example.com/x\n\nbody\n")

    assert only_source.skipped[0].reason.startswith("no `- **Surveyed**:` line")


def test_a_well_formed_section_reports_nothing_skipped():
    """The signal is only worth anything if a clean file is quiet."""
    result = parse("## P\n\n- **Source**: https://example.com/x\n- **Surveyed**: 2026-01-01\n\nb\n")

    assert len(result.patterns) == 1
    assert result.skipped == ()


# --------------------------------------------------------------------------- ingest


def test_ingest_says_what_it_dropped(cli_config, tmp_path):
    """`patterns: 0` and exit 0 on a file the operator just wrote is this project's
    signature failure: silent, and shaped exactly like success."""
    md = tmp_path / "notes.md"
    md.write_text("## My pattern\n\n- Source: https://example.com/x\n- Surveyed: 2026-01-01\n\nb\n")

    result = CliRunner().invoke(main, ["architect", "ingest", str(md)])

    assert result.exit_code == 0, result.output
    assert 'skipped section "My pattern"' in result.stderr
    assert "bold markers are required" in result.stderr
    assert "techne architect add" in result.stderr, "does not offer the way that works"
    assert json.loads(result.stdout)["patterns_added"] == 0


# --------------------------------------------------------------------------- add


def _query(db, sql):
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _add(*args):
    return CliRunner().invoke(main, ["architect", "add", *args])


def test_add_stores_a_pattern_without_any_markdown(cli_config):
    """The whole point: no format to get wrong, because there is no format in the path."""
    result = _add(
        "--name", "Content-hash ids", "--source", "https://github.com/x/y",
        "--surveyed", "2026-08-25", "--body", "Ids are hashes of the content, so writes dedup.",
    )

    assert result.exit_code == 0, result.output
    assert result.output.startswith("added ")

    found = CliRunner().invoke(main, ["architect", "search", "content-hash"])
    assert "Content-hash ids" in found.output, "stored but not findable"


def test_add_is_idempotent(cli_config):
    """Ids are content hashes, so a repeated add is the same pattern, not a collision."""
    args = ("--name", "P", "--source", "https://github.com/x/y", "--body", "same body text")

    first, second = _add(*args), _add(*args)

    assert first.output.startswith("added ")
    assert second.output.startswith("already present ")
    assert first.output.split()[1] == second.output.split()[2]


@pytest.mark.parametrize(
    "args,expected",
    [
        (("--source", "https://x.com/a", "--body", "b"), "Missing option"),  # no --name
        (("--name", "P", "--body", "b"), "Missing option"),  # no --source
        (("--name", "P", "--source", "nonsense", "--body", "b"), "http(s) URL"),
        (("--name", "P", "--source", "https://x.com/a", "--surveyed", "25-08-2026",
          "--body", "b"), "YYYY-MM-DD"),
        (("--name", "P", "--source", "https://x.com/a", "--body", "   "), "body is empty"),
    ],
)
def test_add_refuses_before_writing_anything(cli_config, args, expected):
    """A usage error belongs before any write, and it must SAY the shape it wanted."""
    result = _add(*args)

    assert result.exit_code != 0
    assert expected in result.output
    # "BEFORE WRITING ANYTHING" IS IN THE NAME AND WAS NOT ASSERTED (Prudence, #758 round
    # 4). She moved the `_ISO_DATE_RE` guard below `upsert_investigation` and this stayed
    # 5/5 green while an orphan row landed carrying `surveyed_at='25-08-2026'` — the exact
    # non-ISO shape the round-1 Critical was about. A test that names three claims and
    # checks two is a test that reads as stronger than it is.
    rows = _query(architect_db(cli_config), "SELECT COUNT(*) FROM investigations")
    assert rows[0][0] == 0, "a rejected `add` wrote an investigation row anyway"


@pytest.mark.parametrize("url", ["https://x.com/a", "http://x.com/a", "https://x.com/a/b?c=1"])
def test_add_accepts_every_url_shape_ingest_accepts(cli_config, url):
    """`add` and `ingest` must not disagree about what a URL is — a second opinion here
    would be a second spelling of the same rule, and the two paths would drift. Asserted
    on BEHAVIOUR against the parser itself rather than on `add`'s source text: a test that
    greps for an import passes when the import is present and unused."""
    accepted_by_ingest = parse(
        f"## P\n\n- **Source**: {url}\n- **Surveyed**: 2026-01-01\n\nbody\n"
    ).patterns
    assert len(accepted_by_ingest) == 1, "fixture URL is not one ingest accepts"

    result = _add("--name", "P", "--source", url, "--body", "b")

    assert result.exit_code == 0, f"ingest accepts {url!r} and add rejects it:\n{result.output}"


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/x/y",
        # A TRAILING SLASH, which is the case that catches an ADDITION to the parser
        # (Prudence, #758 round 2). The derived oracle catches extraction being REMOVED;
        # her mutation added `.rstrip("/")` to `_parse_pattern_body` and every fixture
        # stayed green, because none of them had a trailing slash to strip. Deriving the
        # expectation is necessary and not sufficient — the fixtures still have to contain
        # the character a new transformation would act on.
        "https://github.com/x/y/",
        # the shape `export_patterns_md` itself teaches — a URL followed by a path
        "https://github.com/x/y — src/foo.ts",
        "see https://github.com/x/y for details",
        "https://github.com/x/y).",
        "(https://github.com/x/y;",
    ],
)
def test_add_stores_what_ingest_would_store(cli_config, raw):
    """PARITY OF WHAT LANDS IN THE DATABASE, not of what is accepted (Prudence, #758
    Critical). `_URL_RE` and `_DATE_RE` are unanchored EXTRACTORS — `_DATE_RE` carries a
    capture group so callers take `group(1)`, and `_parse_pattern_body` does. `add` used
    them as booleans and stored the raw argument, so sharing the regex bought ACCEPTANCE
    parity while normalization, where the disagreement actually lives, was never shared.

    It matters because `investigations` is `UNIQUE(url, surveyed_by, lens)`: the two paths
    minted DIFFERENT ROWS FOR ONE URL, and add → export → re-ingest turned one
    investigation into two, against an invariant `test_export_roundtrips_with_ingest`
    already names.

    The oracle my previous test could not be: its three fixtures were the subset where raw
    equals normalized, so it was unfalsifiable by construction — and no mutant could have
    exposed it, because AN ABSENT TRANSFORMATION HAS NO MUTANT to kill."""
    # THE EXPECTATION IS DERIVED FROM `parse`, NOT HARDCODED. The first version of this
    # test parametrised the expected value and asserted `add` matched it — while its own
    # failure message claimed to be comparing against ingest. That is the absent-link shape
    # again: nothing tied `add` to `parse`, so a change to `_parse_pattern_body`'s
    # extraction would silently re-open the gap with every one of these cases still green.
    # Running the SAME source through `parse` makes the link the thing under test.
    parsed = parse(
        f"## P\n\n- **Source**: {raw}\n- **Surveyed**: 2026-01-01\n\nb\n"
    ).patterns
    assert len(parsed) == 1, f"fixture is not one ingest accepts: {raw!r}"
    ingest_would_store = parsed[0].source_url

    result = _add("--name", "P", "--source", raw, "--surveyed", "2026-01-01", "--body", "b")

    assert result.exit_code == 0, result.output
    urls = [r[0] for r in _query(architect_db(cli_config), "SELECT url FROM investigations")]
    assert urls == [ingest_would_store], (
        f"add stored {urls}, parse extracts [{ingest_would_store!r}] from the same source"
    )


def test_add_and_ingest_agree_on_one_investigation_per_url(cli_config, tmp_path):
    """THE INVARIANT THE NORMALIZATION GAP BROKE. Same URL through both paths must be one
    investigation row, not two."""
    _add(
        "--name", "P", "--source", "https://github.com/x/y — src/foo.ts",
        "--surveyed", "2026-01-01", "--body", "b",
    )
    md = tmp_path / "corpus.md"
    md.write_text(
        "## P2\n\n- **Source**: https://github.com/x/y — src/foo.ts\n"
        "- **Surveyed**: 2026-01-01\n\nanother body\n"
    )
    CliRunner().invoke(main, ["architect", "ingest", str(md)])

    rows = _query(architect_db(cli_config), "SELECT url FROM investigations")

    assert len(rows) == 1, f"one URL became {len(rows)} investigations: {rows}"


def test_add_rejects_a_date_that_is_not_exactly_iso(cli_config):
    """`patterns.py:50` records that `surveyed_at` is a zero-padded ISO date BECAUSE
    import_md enforces it at parse time, and calls that scoping "the sweep's only safety
    property". `add` bypassed parse time: `"surveyed 2026-01-01 roughly"` was accepted and
    stored raw, and the sweep's own predicate then returns [] for it — `'s' > '2'`, so no
    `< date` comparison reaches it. `_ISO_DATE_RE` is anchored and already lived in the
    module `add` writes into."""
    result = _add(
        "--name", "P", "--source", "https://x.com/a",
        "--surveyed", "surveyed 2026-01-01 roughly", "--body", "b",
    )

    assert result.exit_code != 0
    assert "exactly YYYY-MM-DD" in result.output


def test_add_builds_the_relation_graph_like_ingest_does(cli_config):
    """`ingest` writes four things; `add` wrote two — no relations, no backfill — so
    `pattern_relations` stayed EMPTY for add-fed patterns and `architect related` returned
    nothing for exactly the operator this command exists for (Prudence, #758 Important B)."""
    _add("--name", "Alpha", "--source", "https://x.com/a", "--body", "mentions [[Beta]]")
    _add("--name", "Beta", "--source", "https://x.com/b", "--body", "the other one")

    # ASSERT THE EDGE, NOT A COUNT (Prudence, #758 round 2, weakest oracle). `COUNT(*) > 0`
    # survives a `backfill_graph` that resolves NOTHING — she mutated it to no-op and this
    # file stayed 24/24, because unresolved rows still count. The claim is that Alpha's
    # `[[Beta]]` reaches Beta, so that is what is asserted.
    # ASSERT THE RESOLUTION, NOT THE ROW (Prudence, #758 round 3 — my round-2 repair
    # survived her mutation VERBATIM). `dst_name` is the raw wikilink text and is written
    # identically for UNRESOLVED rows, so with the index empty the row still comes back
    # with `dst_pattern_id=None, resolution_status='unresolved'` and the assertion passed.
    # The claim is that Alpha's `[[Beta]]` RESOLVES TO Beta's row, so the resolved id is
    # what has to be checked.
    rows = _query(
        architect_db(cli_config),
        # `kind` INCLUDED (Prudence, #758 round 4). Relabelling `backfill_graph`'s
        # `'related'` literal to `'supersedes'` — which would retype every wikilink edge in
        # the store — passes both this test without it AND all 43 of
        # `test_patterns_graph.py`. That literal is untested repo-wide; one column closes
        # it here for the path this command actually walks.
        "SELECT src.name, r.dst_name, dst.name, r.resolution_status, r.kind "
        "FROM pattern_relations r "
        "JOIN patterns src ON src.id = r.src_pattern_id "
        "LEFT JOIN patterns dst ON dst.id = r.dst_pattern_id",
    )

    assert ("Alpha", "Beta", "Beta", "resolved", "related") in rows, (
        f"the [[Beta]] wikilink did not RESOLVE to Beta's row: {rows}"
    )


def test_prose_before_the_metadata_is_diagnosed_as_misplaced_not_missing():
    """BEING TOLD TO FIX SOMETHING THAT IS NOT BROKEN IS WORSE THAN A VAGUE ERROR
    (Prudence, #758 Important C). The metadata bullets must come first: the first
    non-metadata, non-blank line ends the preamble, so a section opening with framing prose
    — an ordinary way to write — has its perfectly correct `- **Source**:` line eaten as
    body. The message then told the operator the bold markers were required, about a file
    whose markers were right."""
    result = parse(
        "## P\n\nFraming prose first.\n\n- **Source**: https://x.com/a\n"
        "- **Surveyed**: 2026-01-01\n\nBody.\n"
    )

    assert len(result.skipped) == 1
    reason = result.skipped[0].reason
    assert "AFTER the body starts" in reason, reason
    assert "bold markers are required" not in reason, (
        "still blaming the markers on a file whose markers are correct"
    )


def test_add_reads_stdin_only_when_it_is_piped(tmp_path):
    """The one guard with no test (Prudence, #758 D — she ran both halves by hand). On a
    tty, reading stdin would HANG looking like the command is working, which is the exact
    failure this feeder exists to stop making.

    A REAL PTY, NOT A MONKEYPATCH. `CliRunner` swaps `sys.stdin` at invoke time, so
    patching `sys.stdin.isatty` before the call is undone by the runner and the guard can
    never be reached — my first version asserted the tty message and got the piped one,
    testing the opposite branch from the one it named. The only way to put a tty on stdin
    is to give the process a tty."""
    import os
    import pty
    import subprocess
    import sys

    env = {**os.environ, "TECHNE_HOME": str(tmp_path)}
    cmd = [sys.executable, "-m", "techne.cli", "architect", "add",
           "--source", "https://x.com/a"]

    controller, follower = pty.openpty()
    try:
        with_tty = subprocess.run(
            cmd + ["--name", "P"], stdin=follower, capture_output=True, text=True,
            timeout=60, env=env,
        )
    finally:
        os.close(controller)
        os.close(follower)
    # PIPE REAL CONTENT, NOT "" (Prudence, #758 round 2). `input=""` only proves the tty
    # branch was not taken; it never shows stdin is actually READ, because an empty read
    # and no read both end at "the body is empty". Piping a body and asserting it is STORED
    # is what distinguishes them.
    # THE NEEDLE MUST APPEAR ONLY IN THE BODY (Prudence, #758 round 3). My first version
    # named the pattern `FromStdin` and searched `"arrived on stdin"` — and the token
    # `stdin` matches the NAME, which arrives on the command line. With `sys.stdin.read()`
    # replaced by a constant the test still passed. The name is now unrelated to the body,
    # and the needle is a nonsense token that can only have come through the pipe.
    piped = subprocess.run(
        cmd + ["--name", "PipedPattern"], input="zqxjkv unique body token",
        capture_output=True, text=True, timeout=60, env=env,
    )

    assert "pipe it on stdin" in with_tty.stdout + with_tty.stderr, (
        "on a tty the command must refuse rather than block on stdin"
    )
    assert "pipe it on stdin" not in piped.stdout + piped.stderr, (
        "a piped run must READ stdin, not refuse — the two branches are not distinguished"
    )
    assert piped.returncode == 0, piped.stdout + piped.stderr
    stored = subprocess.run(
        [sys.executable, "-m", "techne.cli", "architect", "search", "zqxjkv"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert "PipedPattern" in stored.stdout, (
        f"the piped body never reached the store — searching for a token that exists "
        f"ONLY in the piped body found nothing:\n{stored.stdout}"
    )


def test_a_single_hash_heading_is_reported_not_silently_ignored(cli_config, tmp_path):
    """THE HINT HAD THE DEFECT IT WAS WRITTEN TO FIX (Prudence, #758 round 2). It was gated
    on `counts["skipped"]`, and `skipped` only fills for sections that reached
    `_parse_pattern_body` — which requires exactly `## `. So `# My pattern` with perfect
    metadata printed `"patterns_added": 0`, exit 0, and NOTHING on stderr: this command's
    own CHANGELOG entry verbatim, for a mistake at least as guessable as the bold markers.

    Worse than the silence it replaced, in fact, because operators have now learned that
    ingest speaks up when something is wrong — so silence reads as 'the file was fine'."""
    md = tmp_path / "onehash.md"
    md.write_text(
        "# My pattern\n\n- **Source**: https://github.com/x/y\n"
        "- **Surveyed**: 2026-01-01\n\nBody.\n"
    )

    result = CliRunner().invoke(main, ["architect", "ingest", str(md)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["patterns_added"] == 0
    assert "must start with `## `" in result.output, (
        f"a file whose headings ingest cannot see produced no diagnosis:\n{result.output}"
    )


def test_a_no_lift_only_corpus_is_not_called_a_failure(cli_config, tmp_path):
    """The other side of the same condition. A corpus of only no-lift bullets ingests
    investigations while `patterns_added` stays 0, and a message keyed on patterns alone
    would call that nothing-was-ingested. It is not — which is why the count is now
    reported rather than inferred."""
    md = tmp_path / "nolift.md"
    # The shape `_NO_LIFT_RE` actually accepts: `- <url> — surveyed <date> — <reason>`.
    # My first fixture invented a different one and parsed to zero entries — a fixture
    # asserting a shape the product refuses, which is the third time in this PR.
    md.write_text(
        "## Surveyed (no patterns worth saving)\n\n"
        "- https://github.com/x/y — surveyed 2026-01-01 — nothing reusable here\n"
    )

    result = CliRunner().invoke(main, ["architect", "ingest", str(md)])

    counts = json.loads(result.stdout)
    assert counts["no_lifts_seen"] == 1, counts
    assert "nothing was ingested" not in result.output


def test_re_ingesting_an_unchanged_corpus_is_not_called_a_failure(cli_config, tmp_path):
    """`patterns_added` COUNTS WRITES, AND WRITES DEDUP. Keying "was this file understood?"
    on it gave the wrong answer for a re-ingest: everything parses, nothing is written, and
    the operator was told `no pattern sections found — sections must start with "## "`
    about a file whose headings are correct.

    That is the same defect the message was added to fix, reintroduced by the fix for it —
    found by attacking my own round-3 brief rather than by review. The counters the message
    keys on now report what was PARSED.

    PRUDENCE'S EXAMPLE IS THE ONE THAT MATTERS, and is better than the plain re-ingest
    tested below: a `--verdict` re-ingest is THE DOCUMENTED multi-row correction path
    (`ingest_patterns_md`'s own docstring points at it), and it rewrote `patterns-only` →
    `full-eval` — a real, intended write — while being told nothing was ingested. Keying on
    `patterns_seen` covers it, because the file parses either way."""
    md = tmp_path / "good.md"
    md.write_text(
        "## Good pattern\n\n- **Source**: https://github.com/x/y\n"
        "- **Surveyed**: 2026-01-01\n\nA real body.\n"
    )

    first = CliRunner().invoke(main, ["architect", "ingest", str(md)])
    second = CliRunner().invoke(main, ["architect", "ingest", str(md)])

    assert json.loads(first.stdout)["patterns_added"] == 1
    counts = json.loads(second.stdout)
    assert counts["patterns_added"] == 0, "fixture is wrong — the second write was not a dedup"
    assert counts["patterns_seen"] == 1, counts
    assert "must start with" not in second.output, (
        f"a correct file was told its headings are wrong on re-ingest:\n{second.output}"
    )
    assert "nothing was ingested" not in second.output


def test_the_feeder_does_not_stamp_a_stranger_with_our_persona(tmp_path, monkeypatch):
    """`architect add` defaulted `--surveyed-by` to "raven" — one of THIS project's agent
    names — hardcoded at cli.py. A stranger who installed techne and fed the architect got
    every pattern attributed to someone they had never heard of, and `core/architect.py`
    called the field "persona-agnostic" while the default shipped a persona.

    Not cosmetic: `investigations` is UNIQUE(url, surveyed_by, lens), so the value is part
    of row identity. This asserts the DEFAULT is neutral; the next test asserts an operator
    with history can pin it back, which is why the fix is a config key and not a new literal.
    """
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    res = CliRunner().invoke(
        main,
        ["architect", "add", "--name", "P", "--source", "https://example.com/x", "--body", "b"],
    )
    assert res.exit_code == 0, res.output

    import sqlite3

    rows = sqlite3.connect(tmp_path / "architect.db").execute(
        "SELECT surveyed_by FROM investigations"
    ).fetchall()
    assert rows == [("operator",)], rows

    # NOT a raw-bytes scan of the db: the first version of this test asserted
    # b"raven" was absent from the file and failed on a CORRECT store, because the
    # DDL comment on the column named the personas and sqlite_master keeps schema
    # text verbatim. Assert on the stored VALUES, and separately on the schema.
    schema = sqlite3.connect(tmp_path / "architect.db").execute(
        "SELECT sql FROM sqlite_master WHERE name = 'investigations'"
    ).fetchone()[0]
    assert "raven" not in schema, "a persona name still ships in the schema comment"


def test_an_operator_with_history_can_pin_surveyed_by_so_rows_still_join(tmp_path, monkeypatch):
    """The reason the fix is a config key rather than a changed literal.

    A store with 360 investigations under one `surveyed_by` would mint a SECOND row for
    every URL it already held if the default simply moved. Setting `architect.surveyed_by`
    keeps new rows joining the old ones.
    """
    from click.testing import CliRunner

    from techne.cli import main

    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("architect:\n  surveyed_by: raven\n  lens: infra\n")
    res = CliRunner().invoke(
        main,
        ["architect", "add", "--name", "P", "--source", "https://example.com/y", "--body", "b"],
    )
    assert res.exit_code == 0, res.output

    import sqlite3

    rows = sqlite3.connect(tmp_path / "architect.db").execute(
        "SELECT surveyed_by, lens FROM investigations"
    ).fetchall()
    assert rows == [("raven", "infra")], rows


def test_the_architect_config_survives_a_save_round_trip(tmp_path):
    """techne#601/#167, the same trap twice already: a key parsed by `_from_dict` but not
    written back by `save()` is silently reset by `techne init`, which is save()'s only
    caller. An operator who pinned surveyed_by would lose it on their next init and their
    corpus would split anyway — the exact failure the config key exists to prevent.
    """
    from techne.config import Config

    cfg = Config()
    cfg.architect.surveyed_by = "raven"
    cfg.save(tmp_path / "config.yaml")

    assert "surveyed_by: raven" in (tmp_path / "config.yaml").read_text()
    assert Config.load(tmp_path / "config.yaml").architect.surveyed_by == "raven"
