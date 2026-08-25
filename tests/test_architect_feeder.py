"""The architect's feeder that ships, and the silence it replaces (techne#754).

techne teaches four domain names. On a fresh install the architect — "what is worth
stealing" — had no way in that came with the tool: `ingest` takes a markdown corpus in an
undocumented shape, and the skills that actually fill the store live in a separate repo.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from techne.cli import main
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
    assert [s.heading for s in result.skipped] == ["A pattern"]
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
