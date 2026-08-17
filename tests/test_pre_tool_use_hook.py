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
        # `ag` DECLINES BY DESIGN (#596 r4). Its table entries were wrong — `-f`
        # (--follow) and `-D` (--debug) take no value but were filed as taking one,
        # so `ag -f <sym> src` injected `src`. ag is not installed here, so any
        # table for it would be asserted from memory rather than measured against
        # the binary, which is how the wrong entries arrived. A missed augmentation
        # costs nothing; re-add ag only WITH A MEASURED TABLE.
        assert self._p("ag _walk_repo src/") is None
        assert self._p("ag -f _walk_repo src") is None
        assert self._p("ag -D _walk_repo src") is None

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
        IS the pattern. A blanket 'skip the token after any flag' would eat it,
        and so would treating a value-taking letter as always separate."""
        assert self._p("grep -A5 index_freshness .") == "index_freshness"
        assert self._p("grep -A 5 index_freshness .") == "index_freshness"
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


class TestRoundTwoFindings:
    """The four #596 r2 findings, each pinned by the case that exposed it."""

    def _p(self, cmd):
        from techne.hooks.pre_tool_use import _bash_search_pattern

        return _bash_search_pattern(cmd)

    def test_a_control_operator_is_not_a_pipe(self):
        """`_split_bash_segments` splits on `&&`, `||`, `;`, newline and `|`
        IDENTICALLY, so "not the first segment" is not "later in a pipeline".
        Testing position declined `cd /repo && rg foo` — and `rg foo` with no
        operand is rg's NORMAL form, so that was a real search lost, by the same
        commit that added `_bash_cd_target` for exactly that shape."""
        assert self._p("cd /repo && rg foo_symbol") == "foo_symbol"
        assert self._p("make build; rg foo_symbol") == "foo_symbol"
        assert self._p("test -d x || rg foo_symbol") == "foo_symbol"
        # ...while a genuine pipe still declines.
        assert self._p("cat x.py | rg foo_symbol") is None

    def test_an_ambiguous_flag_declines_rather_than_guessing(self):
        """An option we cannot read declines the whole command — measured in #596
        r3: 13 of 13 UNLISTED value-taking options injected a WRONG pattern and 0
        declined, because an allow-list that fails OPEN promotes an operand to the
        pattern (`rg --type json` searched for `json`).

        `-t` IS NOT AN EXAMPLE OF THAT ANY MORE, and the r3 commit message claiming
        it was is false (#596 r4). `t` sat in the shared value-taking table, so
        `_consumes_next("-t")` returned True: it GUESSED. The assertion below passed
        only because `-t` ate `search_symbols` and the leftover `.` failed
        `_SYMBOLISH` — the RESULT, not the RULE. Change the operand and the guess
        became visible: `grep -t search_symbols src` injected `src`.

        Arity is now per-head, so `-t` is answered rather than guessed: rg has
        `--type` (takes a value), grep has no `-t` at all (declines)."""
        assert self._p("rg -t json search_symbols") == "search_symbols"
        assert self._p("rg --type json search_symbols") == "search_symbols"
        # Unknown long options decline.
        assert self._p("grep --unknown-opt x search_symbols .") is None

        # `-t` ON GREP, WITH OPERANDS THAT MAKE A GUESS VISIBLE. Each of these
        # returned a WRONG PATH as the pattern before per-head tables; the leading
        # case is the one the old assertion used, kept so the pair is legible.
        assert self._p("grep -t search_symbols .") is None
        assert self._p("grep -t search_symbols src") is None
        assert self._p("grep -t search_symbols techne") is None
        assert self._p("grep -rn -t sym_name src") is None

        # THE CASES THAT DISCRIMINATE. The two above also return None under a
        # mutant that guesses, because the guessed token happens to be
        # non-symbol-shaped — so they pin the RESULT and not the RULE. These use
        # an unknown option whose separate value IS symbol-shaped, so guessing
        # produces a WRONG PATTERN rather than nothing: exactly the 13-of-13
        # failure measured in #596 r3.
        assert self._p("rg --somefuture-opt jsonlike sym_name") is None
        assert self._p("rg -Y jsonlike sym_name") is None

    def test_the_attached_spellings_of_e_and_regexp_name_the_pattern(self):
        """`-e<sym>` and `--regexp=<sym>` are the two options whose value IS the
        pattern, and the arity walk had both exactly backwards (#596 r4).

        `--opt=value` returned False on "the value is attached", and a cluster
        containing `e` returned False on "the caller handles it". Both are right
        for `--type=json` and `-ie <sym>`; both were wrong here, because the
        caller's special case only matched the SEPARATE spellings `-e` and
        `--regexp`. So the pattern was skipped as if it were a flag and the NEXT
        OPERAND was promoted — measured against /usr/bin/grep, not a man page:

            grep --regexp=my_symbol src  ->  injected `src`
            grep -emy_symbol mydir       ->  injected `mydir`
        """
        assert self._p("grep --regexp=my_symbol src") == "my_symbol"
        assert self._p("grep -emy_symbol mydir") == "my_symbol"
        assert self._p("grep -rn -emy_symbol src") == "my_symbol"
        # The separate spellings, including inside a cluster, still work.
        assert self._p("grep -e my_symbol src") == "my_symbol"
        assert self._p("grep -ie my_symbol src") == "my_symbol"
        assert self._p("grep --regexp my_symbol src") == "my_symbol"
        # An ordinary attached long value is still a value, not a pattern.
        assert self._p("rg --type=py my_symbol src") == "my_symbol"

    def test_option_arity_is_per_head_because_the_same_letter_differs(self):
        """A letter in a SHARED table asserts it means the same thing in every
        tool. Three of those assertions were false, and the class was not
        exhausted by the review that found them (#596 r4).

        Measured with `<tool> -X </dev/null`, where "requires an argument" is the
        discriminator:

            -r   grep: recursive, NO value  |  rg: --replace, TAKES a value
            -E   grep: --extended-regexp    |  rg: --encoding, TAKES a value
            -t   grep: not an option        |  rg: --type, TAKES a value
            -D   grep: --devices, TAKES one |  rg: not an option

        `-E` was NOT in the review's list — it was found by auditing every listed
        letter against the binaries instead of patching the three reported. No
        single shared table can be right, because the same letter genuinely has
        different arity per tool. Per-head tables need not be COMPLETE (unlisted
        still declines); they need only be CORRECT for what they list.
        """
        # rg: -r/-E take values, so the pattern is the token AFTER the value.
        # Verified against the binary: `rg -r new_name old_name t.txt` prints
        # "new_name here", i.e. it matched the line containing `old_name`.
        assert self._p("rg -r new_name old_name .") == "old_name"
        assert self._p("rg --replace new_name old_name .") == "old_name"
        assert self._p("rg -E utf8 my_symbol .") == "my_symbol"
        # grep: the same letters take no value, so the pattern comes first.
        assert self._p("grep -r my_symbol .") == "my_symbol"
        assert self._p("grep -E my_symbol .") == "my_symbol"
        # grep has no -t; rg has no -D. Both decline rather than guess.
        assert self._p("grep -t my_symbol src") is None
        assert self._p("rg -D my_symbol src") is None
        # grep DOES have -D, and it takes a value.
        assert self._p("grep -D skip my_symbol src") == "my_symbol"

    def test_optional_argument_long_options_do_not_eat_the_pattern(self):
        """GNU/BSD spell these `--color[=WHEN]` and `--context[=NUM]`, so written
        bare they consume NOTHING. They were filed as value-taking, which ate the
        pattern: `grep --context my_symbol src` injected `src` (#596 r4).

        Found by probing the binaries, not by reading the review — and the same
        options take a REAL value on rg, which is the divergence per-head tables
        exist for.
        """
        assert self._p("grep --context my_symbol src") == "my_symbol"
        assert self._p("grep --color my_symbol src") == "my_symbol"
        assert self._p("grep --colour my_symbol src") == "my_symbol"
        # rg's --context genuinely takes a value.
        assert self._p("rg --context 3 my_symbol src") == "my_symbol"
        assert self._p("rg --color always my_symbol src") == "my_symbol"

    def test_a_cluster_ending_in_e_does_not_eat_the_pattern(self):
        """`-e` is handled before the option loop, so a cluster ENDING in `e`
        must not ALSO be treated as value-taking — it would consume the pattern
        and promote the next operand."""
        assert self._p("grep -ie search_symbols .") == "search_symbols"

    def test_a_relative_cd_is_not_resolved_against_the_hook_process(self):
        """`find_repo_root` resolves a relative path against os.getcwd() — wherever
        the hook happens to run — so `cd ../beta && grep …` injected a THIRD
        repo's graph. The helper still returns the literal; `main()` anchors it to
        the SESSION cwd."""
        assert self._t("cd ../beta && grep -rn foo .") == "../beta"

    def _t(self, cmd):
        from techne.hooks.pre_tool_use import _bash_cd_target

        return _bash_cd_target(cmd)


def test_a_relative_cd_resolves_against_the_session_cwd(home, monkeypatch, capsys):
    """END-TO-END for finding 2. Session cwd in alpha, `cd ../beta` — beta's graph
    must be injected, not the hook process's notion of `../beta`."""
    from techne.config import Config
    from techne.core.codegraph import add_symbol
    from techne.core.codex import codex_db_for_repo

    _write_config(home, codex_grep_assist=True)
    cfg = Config.load()
    nest = home / "nest"
    for name in ("alpha", "beta"):
        (nest / name / ".git").mkdir(parents=True)
    add_symbol(
        codex_db_for_repo(cfg, nest / "beta"),
        id="b.py::shared::function::1",
        file="b.py",
        name="shared",
        kind="function",
        signature="def shared(): ...  # BETA",
    )

    out = _run(
        monkeypatch,
        capsys,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cd ../beta && grep -rn shared ."},
            "cwd": str(nest / "alpha"),
        },
    )
    assert "BETA" in out


def test_a_non_search_bash_call_does_not_clobber_liveness(home, monkeypatch, capsys):
    """Matching Bash makes this hook fire on nearly every tool call. Recording
    each one overwrote `last_run`, so a single `git status` clobbered a successful
    injection and techne#468's detector read identically whether the rail worked
    perfectly or never fired — #596's own invisibility, relocated into the
    observability layer by the fix for it."""
    from techne.config import Config
    from techne.core.database import connect
    from techne.core.extension_state import get_state

    def _last_run():
        conn = connect(Config.load().db_path)
        try:
            return get_state(conn, "hook:codex_grep_assist:last_run")
        finally:
            conn.close()

    _write_config(home, codex_grep_assist=True)
    repo, db = _make_repo(home, indexed=True)
    from techne.core.codegraph import add_symbol

    add_symbol(
        db, id="s.py::authenticate::function::1", file="s.py", name="authenticate", kind="function"
    )

    _run(monkeypatch, capsys, _grep("authenticate", repo))
    after_success = _last_run()

    _run(
        monkeypatch,
        capsys,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git status --short"},
            "cwd": str(repo),
        },
    )
    after_noise = _last_run()

    assert after_noise == after_success, "a non-search Bash call overwrote the rail's liveness"


class TestRoundThreeFindings:
    """The #596 r3 findings."""

    def _p(self, cmd):
        from techne.hooks.pre_tool_use import _bash_search_pattern

        return _bash_search_pattern(cmd)

    def test_a_redirect_target_is_not_a_path_operand(self):
        """`cat x | grep sym > out.txt` fired, because `out.txt` counted as "this
        searches a tree". It is where the output GOES; the command is still
        filtering stdin. Attachment is decided by what FOLLOWS the operator —
        keying on token length gets `2>` wrong, since it is two characters with a
        separate target."""
        assert self._p("cat x.py | grep sym_name > out.txt") is None
        assert self._p("cat x.py | grep sym_name >out.txt") is None
        assert self._p("cat x.py | grep sym_name 2> err.txt") is None
        assert self._p("cat x.py | grep sym_name 2>&1") is None
        # A real tree search keeps firing even when its output is redirected.
        assert self._p("grep -rn sym_name . > out.txt") == "sym_name"

    def test_the_operand_half_of_the_guard_is_pinned(self):
        """Deleting `operands == 0` from the piped-filter guard survived the FULL
        2836-test suite (#596 r3) — the branch was unpinned, so the discriminator
        could silently become "decline every piped grep", losing real searches.

        `cat x | grep sym .` is piped AND has an operand: it searches a tree whose
        stdin happens to be attached, so it must fire."""
        assert self._p("cat list.txt | grep -rn sym_name techne/") == "sym_name"
        assert self._p("cat list.txt | grep sym_name") is None


def test_the_prefilter_is_strictly_weaker_than_the_parse(home, monkeypatch, capsys):
    """The prefilter exists for cost (#596 r3: ~113ms added to every Bash call,
    79% of it on the decline path). It should not reject something the real parse
    would have accepted — it is an optimisation, and an optimisation that changes
    behaviour is a bug.

    THIS IS A CORPUS, NOT A PROOF, and the r3 phrase "strictly weaker BY
    CONSTRUCTION" overclaimed (#596 r4 Nit). The prefilter is a SUBSTRING test on
    the raw command; the parse needs the BASENAME OF TOKEN 0 after shlex
    unquoting. Those differ, and there are real counterexamples:

        g''rep -rn my_symbol .      parses to my_symbol; prefilter rejects
        gr\\ep -rn my_symbol .       parses to my_symbol; prefilter rejects

    Both are missed augmentations, which cost nothing, and no agent writes them —
    so the gap is accepted deliberately rather than closed. What the loop below
    establishes is that the corpus is clean, not that the property holds.
    """
    from techne.hooks.pre_tool_use import _SEARCH_HEADS, _bash_search_pattern

    accepted = [
        "grep -rn sym_name .",
        "cd /repo && rg sym_name",
        "/usr/bin/grep -rn sym_name .",
        "make build; grep -rn sym_name techne/",
    ]
    for cmd in accepted:
        assert _bash_search_pattern(cmd) is not None, cmd
        assert any(h in cmd for h in _SEARCH_HEADS), (
            f"the prefilter would drop a command the parse accepts: {cmd}"
        )


class TestRoundFiveFindings:
    """#596 round 5 — two Importants that survived four rounds of option work."""

    def _p(self, cmd):
        from techne.hooks.pre_tool_use import _bash_search_pattern

        return _bash_search_pattern(cmd)

    def test_options_that_leave_no_inline_pattern_decline(self):
        """CORRECT ARITY IS NOT ENOUGH — an option can consume its value properly
        and still leave no pattern behind (#596 r5).

        `-e`/`--regexp` were special-cased because their value IS the pattern. Their
        siblings fail the opposite way: with `-f` the patterns come from a FILE, so
        every remaining operand is a path, and `rg --files` does not search at all.
        The rail promoted the first operand, so `rg --files src` injected `src`.

        BEING LISTED IS WHAT MADE THESE FIRE: `-f` sat in the value-taking table, so
        it was skipped cleanly and the path after it looked exactly like a pattern.
        An option in NO table would have declined.
        """
        assert self._p("rg --files src") is None
        assert self._p("rg --files techne") is None
        assert self._p("rg --files") is None
        assert self._p("grep -f pats.txt src") is None
        assert self._p("rg -f pats.txt src") is None
        assert self._p("grep --file=pats.txt src") is None
        assert self._p("grep -rn -f pats.txt techne") is None
        assert self._p("rg --type-list") is None
        # An `f` that is part of another option's VALUE is data, not a flag.
        assert self._p("grep -A2 my_symbol src") == "my_symbol"

    def test_grep_letters_whose_arity_differs_between_shim_and_bsd_decline(self):
        """`grep` in a Bash tool call is NOT the grep on $PATH (#596 r5).

        Claude Code installs a shell FUNCTION shimming grep to a ugrep bundled in
        the `claude` binary; `shutil.which("grep")` reports /usr/bin/grep (BSD) and
        never sees it. The r4 claim that "BSD grep and ugrep agree exactly" came
        from a discriminator that false-negatives: ugrep given a value-taking option
        with nothing after it says "no PATTERN specified", not "requires an
        argument".

        Re-derived via `grep --help -X` (which prints `-M MAGIC` vs `-y`): J, M and
        O all take values and were all in the no-value set, so
        `grep -M ELF <sym> src` injected `ELF`.

        They now decline rather than moving to the value table, because the shim
        falls back to real BSD grep on `-[Zz]*`, `--null`, `-*-filter*` and friends
        — so a letter whose arity differs between the two binaries cannot be
        resolved statically, and declining is the only answer right for the union.
        """
        assert self._p("grep -M ELF my_symbol src") is None
        assert self._p("grep -J 5 my_symbol src") is None
        assert self._p("grep -O py my_symbol src") is None
        # Letters that DO agree still work.
        assert self._p("grep -rn my_symbol .") == "my_symbol"
        assert self._p("grep -i my_symbol src") == "my_symbol"

    def test_multiple_pattern_naming_options_decline(self):
        """The tool searches for ALL of them; this rail can inject one. Silently
        keeping the last is an arbitrary choice dressed as an answer (#596 r5 Nit).
        """
        assert self._p("grep -e alpha_one -e beta_two .") is None
        assert self._p("grep -e alpha_one --regexp=beta_two .") is None
        assert self._p("grep -ealpha_one -ebeta_two .") is None
        # One is still fine.
        assert self._p("grep -e alpha_one .") == "alpha_one"

    def test_a_head_without_a_table_declines_instead_of_raising(self):
        """`_TABLES[head]` was a bare subscript, so re-adding a head to
        `_SEARCH_HEADS` without a table would KeyError inside a hook (#596 r5 Nit).
        Closed today, but this is exactly what re-adding `ag` would trip.
        """
        import techne.hooks.pre_tool_use as M

        original = M._SEARCH_HEADS
        try:
            M._SEARCH_HEADS = frozenset(original | {"ag"})
            assert self._p("ag my_symbol src") is None
        finally:
            M._SEARCH_HEADS = original


class TestRoundSixFindings:
    """#596 round 6 — the long-option table, derived from the wrong binary."""

    def _p(self, cmd):
        from techne.hooks.pre_tool_use import _bash_search_pattern

        return _bash_search_pattern(cmd)

    def test_group_separator_is_optional_and_must_not_skip_the_pattern(self):
        """ugrep spells it `--group-separator[=SEP]` — OPTIONAL and attached-only —
        so bare it consumes nothing. It sat in the value-taking table, so the rail
        skipped the next token and injected the path (#596 r6):

            grep --group-separator my_symbol mydir  ->  injected `mydir`

        Same class as the `--color`/`--context` fix documented directly above it in
        the table. The r4 reasoning could not find this one: it was traced from
        GNU/BSD, where `--group-separator` is not optional (and BSD grep does not
        have the option at all), so it found exactly the GNU-optional set.

        The fix is TWO-PART — `_consumes_next` tests `long_value` first, so the
        entry has to be removed there, not merely added to `long_novalue`.
        """
        assert self._p("grep --group-separator my_symbol mydir") == "my_symbol"
        assert self._p("grep --group-separator my_symbol src") == "my_symbol"
        # The attached spelling is a genuine value and must stay skipped.
        assert self._p("grep --group-separator=X my_symbol mydir") == "my_symbol"

    def test_long_context_declines_on_grep_and_that_is_the_safe_direction(self):
        """`--context` is NOT optional in ugrep — it REQUIRES its argument, the
        opposite of what the r4 comment asserted. It stays classified no-value
        because that is the only choice safe in BOTH binaries: GNU/BSD do treat it
        as optional, so `long_value` would skip a real pattern there.

        The cost is that the LONG spelling declines on grep — the numeric argument
        lands as the first operand and is never `_SYMBOLISH`. A missed
        augmentation, not a wrong injection, and the short form is unaffected.
        """
        assert self._p("grep --context 3 my_symbol src") is None
        assert self._p("grep -C 3 my_symbol src") == "my_symbol"
        # rg genuinely takes a value there, and resolves.
        assert self._p("rg --context 3 my_symbol src") == "my_symbol"
