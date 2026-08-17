"""Tests for the PreToolUse grep-assist hook (techne#206, per-repo since #209).

The hook augments the agent's own Grep calls with Codex symbol matches,
injected via hookSpecificOutput.additionalContext. Since techne#209 it resolves
the *per-repo* codex db from the Grep's target `path` (else the session `cwd`),
so symbols are scoped to the repo the agent is actually working in. It remains
strictly best-effort: it must NEVER block the tool, ALWAYS exit 0, and emit
NOTHING on any error, empty path, or unindexed repo. These tests drive main()
directly with a monkeypatched stdin and capture stdout via capsys.
"""

from __future__ import annotations

import io
import json

import pytest

from techne.core.codegraph import add_symbol
from techne.hooks import pre_tool_use


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A fresh TECHNE_HOME so Config.load() resolves db/data_dir under tmp_path."""
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    # Disable the SIGALRM budget so slow CI never trips the hard timeout mid-test.
    monkeypatch.setenv("TECHNE_HOOK_TIMEOUT", "0")
    return tmp_path


def _write_config(home, **hook_flags):
    """Write a config.yaml under TECHNE_HOME with the given hooks.* flags."""
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": hook_flags}))


def _make_repo(home, *, indexed: bool):
    """Create a git repo under home. When `indexed`, also create its per-repo
    codex db so the hook resolves to a real graph. Returns (repo, db_or_None)."""
    from techne.config import Config
    from techne.core.codex import codex_db_for_repo

    repo = home / "workrepo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)  # marker: find_repo_root just checks existence
    db = codex_db_for_repo(Config.load(), repo) if indexed else None
    return repo, db


def _grep(pattern, repo=None):
    """Build a Grep PreToolUse payload; when repo given, set cwd to it."""
    payload = {"tool_name": "Grep", "tool_input": {"pattern": pattern}}
    if repo is not None:
        payload["cwd"] = str(repo)
    return payload


def _run(monkeypatch, capsys, payload: dict) -> str:
    """Run the hook main() with `payload` on stdin; return captured stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as exc:
        pre_tool_use.main()
    assert exc.value.code == 0, f"hook must always exit 0, got {exc.value.code}"
    return capsys.readouterr().out


def test_flag_off_default_emits_nothing(home, monkeypatch, capsys):
    """Flag OFF (default) → no stdout, even with a populated per-repo db."""
    repo, db = _make_repo(home, indexed=True)
    add_symbol(
        db, id="s1", file="auth.py", name="authenticate", kind="function",
        signature="def authenticate(user): ...",
    )
    # No config.yaml at all → codex_grep_assist defaults to False.
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    assert out == ""


def test_non_grep_glob_tool_emits_nothing(home, monkeypatch, capsys):
    """A tool that isn't Grep → no stdout (short-circuits before config load)."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(
        monkeypatch, capsys,
        {"tool_name": "Read", "tool_input": {"pattern": "authenticate"}, "cwd": str(repo)},
    )
    assert out == ""


def test_unindexed_repo_emits_nothing(home, monkeypatch, capsys):
    """Flag ON but the repo has no codex index → best-effort no-op, no stdout."""
    _write_config(home, codex_grep_assist=True)
    repo, _ = _make_repo(home, indexed=False)  # git repo, but never indexed
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    assert out == ""


def test_not_in_repo_emits_nothing(home, monkeypatch, capsys):
    """cwd outside any git repo → no repo to resolve → no stdout."""
    _write_config(home, codex_grep_assist=True)
    plain = home / "notarepo"
    plain.mkdir()
    out = _run(monkeypatch, capsys, _grep("authenticate", plain))
    assert out == ""


def test_empty_pattern_emits_nothing(home, monkeypatch, capsys):
    """Empty search pattern → no stdout."""
    _write_config(home, codex_grep_assist=True)
    repo, _ = _make_repo(home, indexed=True)
    out = _run(monkeypatch, capsys, _grep("", repo))
    assert out == ""


def test_no_matching_symbol_emits_nothing(home, monkeypatch, capsys):
    """Flag ON, db present, but pattern matches nothing → no stdout."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(monkeypatch, capsys, _grep("nonexistentxyz", repo))
    assert out == ""


def test_matching_symbol_injects_context(home, monkeypatch, capsys):
    """Flag ON + matching symbol in the cwd's repo → protocol JSON injection."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(
        db, id="s1", file="auth.py", name="authenticate", kind="function",
        signature="def authenticate(user, token): ...",
    )
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    assert out.strip(), "expected protocol JSON on stdout"
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    ctx = hso["additionalContext"]
    assert "authenticate" in ctx
    assert "s1" in ctx
    assert "auth.py" in ctx


def test_grep_path_overrides_cwd_for_repo_scope(home, monkeypatch, capsys):
    """The Grep `path` (what's being searched) selects the repo over cwd (#209)."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    # cwd is a non-repo dir; the Grep path points into the indexed repo.
    outside = home / "elsewhere"
    outside.mkdir()
    payload = {
        "tool_name": "Grep",
        "tool_input": {"pattern": "authenticate", "path": str(repo)},
        "cwd": str(outside),
    }
    out = _run(monkeypatch, capsys, payload)
    assert "authenticate" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_cross_repo_symbol_not_injected(home, monkeypatch, capsys):
    """A symbol indexed in repo B must NOT surface when grepping in repo A (#209).

    This is the signal/noise fix #206 was blocked on: the old single global
    graph injected unrelated repos' symbols.
    """
    from techne.config import Config
    from techne.core.codex import codex_db_for_repo

    _write_config(home, codex_grep_assist=True)
    repo_a = home / "alpha"
    repo_b = home / "beta"
    for r in (repo_a, repo_b):
        r.mkdir()
        (r / ".git").mkdir()
    codex_db_for_repo(Config.load(), repo_a)  # A: empty
    db_b = codex_db_for_repo(Config.load(), repo_b)
    add_symbol(db_b, id="s1", file="b.py", name="beta_secret", kind="function")

    # Grep for beta_secret while working in repo A → no injection (A has no such symbol).
    out = _run(monkeypatch, capsys, _grep("beta_secret", repo_a))
    assert out == ""


def test_glob_tool_not_augmented(home, monkeypatch, capsys):
    """Glob is NOT augmented — its pattern is a file glob, not a symbol name (techne#206)."""
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    add_symbol(db, id="s1", file="auth.py", name="authenticate", kind="function")
    out = _run(
        monkeypatch, capsys,
        {"tool_name": "Glob", "tool_input": {"pattern": "authenticate"}, "cwd": str(repo)},
    )
    assert out == ""


def test_stale_symbol_shows_freshness_marker(home, monkeypatch, capsys):
    """A symbol whose file is missing on disk gets a visible freshness marker.

    An absolute path that doesn't exist classifies as 'missing' — not fresh/unknown
    — so the honesty marker must appear in the injected context.
    """
    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    ghost = home / "gone" / "auth.py"  # absolute path that does not exist
    add_symbol(
        db, id="s1", file=str(ghost), name="authenticate", kind="function",
        signature="def authenticate(user): ...", file_hash="deadbeef",
    )
    out = _run(monkeypatch, capsys, _grep("authenticate", repo))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "[missing]" in ctx


def test_malformed_stdin_exits_zero(home, monkeypatch, capsys):
    """Malformed JSON on stdin → exit 0, no stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO("not json {"))
    with pytest.raises(SystemExit) as exc:
        pre_tool_use.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""


# ---- techne#596: the rail must reach the searches agents actually run --------


class TestBashSearchPattern:
    """`_bash_search_pattern` — the parse that decides whether a Bash call is a
    symbol search worth augmenting.

    WHY BASH AT ALL: matching only `Grep` made this rail unreachable in practice.
    It was enabled, registered, and never fired once across a full session of
    searching this codebase, because every search was `grep` inside a Bash call.
    The guard's precondition was "the agent chose the Grep tool", and falsifying
    it was free, accidental and invisible.

    WHY IT IS FUSSY: the cost of a false fire is noise in every future Bash call,
    which is how the #206 original got scoped down in the first place. A missed
    augmentation costs nothing.
    """

    def _p(self, cmd):
        from techne.hooks.pre_tool_use import _bash_search_pattern

        return _bash_search_pattern(cmd)

    def test_plain_grep_yields_the_symbol(self):
        assert self._p("grep -rn recall_attribution techne/") == "recall_attribution"

    def test_quoted_pattern_is_unquoted(self):
        assert self._p('grep -rn "index_freshness" .') == "index_freshness"

    def test_ripgrep_and_friends_count(self):
        assert self._p("rg annotate_freshness") == "annotate_freshness"
        assert self._p("ag _walk_repo src/") == "_walk_repo"

    def test_explicit_dash_e_wins(self):
        assert self._p("grep -rn -e resolve_repo_db_for_query .") == "resolve_repo_db_for_query"

    def test_absolute_path_to_grep_still_counts(self):
        assert self._p("/usr/bin/grep -rn search_symbols .") == "search_symbols"

    def test_a_regex_is_not_a_symbol(self):
        """The whole point of the identifier shape: a regex fed to the symbol FTS
        is noise, and noise trains the reader to skip the injected block."""
        assert self._p("grep -rn 'def .*attribution' .") is None
        assert self._p("grep -rn 'foo|bar' .") is None

    def test_a_path_or_glob_is_not_a_symbol(self):
        assert self._p("grep -rn techne/core/codegraph.py .") is None
        assert self._p("grep -rn '*.py' .") is None

    def test_too_short_is_not_a_symbol(self):
        assert self._p("grep -rn ab .") is None

    def test_a_non_search_command_declines(self):
        """The overwhelmingly common Bash call. Declining must be silent."""
        assert self._p("git status --short") is None
        assert self._p("python -m pytest -q") is None
        assert self._p("ls -la") is None

    def test_a_piped_filter_declines_even_when_the_word_is_symbol_shaped(self):
        """THE NOISE FINDING (#596 r1), and it inverts what this test used to pin.

        Measured against a real 1237-symbol graph: 11 of 11 everyday
        `<cmd> | grep <word>` calls injected context — `git branch -a | grep main`
        put 8 symbols and 1039 characters into the model's context — while the two
        genuine symbol searches injected 1 and 2. The noise fired harder than the
        signal, which is how a rail earns its way back out of the config.

        A later pipeline segment with NO path operand is filtering the previous
        command's output, not searching a tree. Declining those scored 0/11 noise
        with 0/8 real searches lost.
        """
        assert self._p("cat foo.py | grep -n index_freshness") is None
        assert self._p("git branch -a | grep main") is None
        assert self._p("env | grep PATH") is None
        assert self._p("ps aux | grep python") is None

    def test_a_later_segment_WITH_a_path_operand_still_counts(self):
        """The discriminator is the path operand, not the pipe. `… && grep -rn X .`
        searches a tree and must survive."""
        assert self._p("cd /tmp/repo && grep -rn index_freshness .") == "index_freshness"
        assert self._p("make build; grep -rn search_symbols techne/") == "search_symbols"

    def test_an_option_value_is_not_mistaken_for_the_pattern(self):
        """Measured omission (#596 r1): `grep -D skip search_symbols .` injected
        symbols for `skip`, and `--exclude-dir node_modules search_symbols` lost
        the real pattern entirely. Context flags silently dropped the search."""
        assert self._p("grep -D skip search_symbols .") == "search_symbols"
        assert self._p("grep --exclude-dir node_modules search_symbols .") == "search_symbols"
        assert self._p("grep -A 5 index_freshness .") == "index_freshness"
        assert self._p("grep -B 3 index_freshness .") == "index_freshness"
        assert self._p("grep -m 2 annotate_freshness .") == "annotate_freshness"

    def test_attached_option_values_consume_nothing_extra(self):
        """`-A5` and `--include=*.py` carry their value inline, so the next token
        IS the pattern. A blanket 'skip the token after any flag' would eat it."""
        assert self._p("grep -A5 index_freshness .") == "index_freshness"
        assert self._p("grep --include=*.py search_symbols .") == "search_symbols"

    def test_a_short_cluster_only_consumes_when_its_last_letter_does(self):
        """`-rn` takes no value; `-rm` ends in `m` (--max-count) and does."""
        assert self._p("grep -rn search_symbols .") == "search_symbols"
        assert self._p("grep -rm 2 search_symbols .") == "search_symbols"

    def test_a_search_after_cd_is_found(self):
        """The shape that actually appears in this repo's sessions."""
        assert self._p("cd /tmp/repo && grep -rn recall_attribution .") == "recall_attribution"

    def test_a_grep_inside_a_heredoc_body_does_not_fire(self):
        """REUSES `_strip_heredoc_bodies` rather than re-deriving it (techne#544).
        A heredoc body is not quoted, so every line of it becomes a segment and a
        body line starting with `grep` would otherwise read as a command."""
        script = "cat > probe.sh <<'SH'\ngrep -rn totally_not_a_real_symbol .\nSH\n"
        assert self._p(script) is None

    def test_unbalanced_quotes_decline_rather_than_raise(self):
        assert self._p("grep -rn 'unterminated .") is None

    def test_empty_and_junk_decline(self):
        assert self._p("") is None
        assert self._p("   ") is None


def test_bash_is_registered_in_the_matcher():
    """The parse is worthless if Claude Code never invokes the hook for Bash.

    Both halves are required and they lived as two separate literals — the
    registration and the #287 drift check — so a matcher change made the drift
    check report a phantom drift against our own registration. One constant now.
    """
    from techne.platforms.claude_code.hooks import GREP_ASSIST_MATCHER

    assert "Bash" in GREP_ASSIST_MATCHER
    assert "Grep" in GREP_ASSIST_MATCHER


def test_assist_tools_and_matcher_agree():
    """A third place the same fact could drift: the hook's own ASSIST_TOOLS gate.
    If the matcher invites a tool the hook then refuses, the rail is registered
    and inert — which is techne#596 exactly, one layer over."""
    from techne.hooks.pre_tool_use import ASSIST_TOOLS
    from techne.platforms.claude_code.hooks import GREP_ASSIST_MATCHER

    assert set(GREP_ASSIST_MATCHER.split("|")) == ASSIST_TOOLS


class TestBashCdTarget:
    """`cd <repo> && grep …` must scope the graph to <repo>, not the session cwd.

    Measured in #596 r1: with the session cwd in repo alpha, `cd <beta> && grep
    -rn <alpha symbol> .` injected ALPHA's symbols for a search run in BETA — the
    techne#206 signal/noise defect, coming back through the door techne#209 was
    built to close, because Bash carries no `path` field.
    """

    def _t(self, cmd):
        from techne.hooks.pre_tool_use import _bash_cd_target

        return _bash_cd_target(cmd)

    def test_a_leading_literal_cd_is_followed(self):
        assert self._t("cd /tmp/beta && grep -rn foo .") == "/tmp/beta"

    def test_a_quoted_path_with_spaces_survives(self):
        assert self._t("cd '/tmp/my repo' && grep -rn foo .") == "/tmp/my repo"

    def test_no_cd_yields_none(self):
        assert self._t("grep -rn foo .") is None

    def test_a_cd_that_is_not_first_is_not_followed(self):
        """Only a LEADING cd is unambiguous; mid-chain the directory depends on
        what ran before it."""
        assert self._t("make build && cd /tmp/beta && grep -rn foo .") is None

    def test_unexpandable_targets_decline_rather_than_guess(self):
        """Guessing a directory wrong scopes the graph to an unrelated repo —
        which IS the failure being fixed. Declining falls back to session cwd."""
        assert self._t("cd $REPO && grep -rn foo .") is None
        assert self._t("cd ~/work && grep -rn foo .") is None
        assert self._t("cd `pwd` && grep -rn foo .") is None

    def test_cd_with_extra_args_declines(self):
        assert self._t("cd -P /tmp/beta && grep -rn foo .") is None


def test_cd_target_selects_the_repo_the_search_actually_runs_in(home, monkeypatch, capsys):
    """END-TO-END, because the helper tests could not see this.

    `_bash_cd_target` had five passing unit tests while `main()` ignored its
    result entirely — mutating the call site to `cd_target = None` left the whole
    file green. That is "an identity check on an import proves the import, not
    the use", in the tests written to pin a fix.

    So this asserts the OBSERVABLE consequence: with the session cwd in repo
    ALPHA and the command `cd <beta> && grep …`, the injected symbols must come
    from BETA's graph. Getting this wrong is techne#206's signal/noise defect
    arriving through the door techne#209 was built to close.
    """
    from techne.config import Config
    from techne.core.codegraph import add_symbol
    from techne.core.codex import codex_db_for_repo

    _write_config(home, codex_grep_assist=True)
    cfg = Config.load()

    alpha = home / "alpha"
    (alpha / ".git").mkdir(parents=True)
    beta = home / "beta"
    (beta / ".git").mkdir(parents=True)

    add_symbol(
        codex_db_for_repo(cfg, alpha),
        id="a.py::shared_name::function::1",
        file="a.py",
        name="shared_name",
        kind="function",
        signature="def shared_name(): ...  # ALPHA",
    )
    add_symbol(
        codex_db_for_repo(cfg, beta),
        id="b.py::shared_name::function::1",
        file="b.py",
        name="shared_name",
        kind="function",
        signature="def shared_name(): ...  # BETA",
    )

    out = _run(
        monkeypatch,
        capsys,
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"cd {beta} && grep -rn shared_name ."},
            "cwd": str(alpha),
        },
    )

    assert "BETA" in out, "the search ran in beta; beta's graph must be the one injected"
    assert "ALPHA" not in out, "injected the session cwd's graph for a search in another repo"
