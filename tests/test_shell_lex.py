"""techne/core/shell_lex.py: the one quote model the capture filter, the evidence
gate and grep-assist share (techne#1339, PR 1353 review)."""

import itertools

import pytest

from techne.core import shell_lex
from techne.core.shell_lex import classify, has_substitution, mask_line, split_segments, words


@pytest.mark.parametrize(
    "text, want",
    [
        ("ls 'a b'", "cccsssss"),
        ('echo "it\'s"', "cccccdddddd"),
        ("echo it\\'s", "ccccccceec"),
        ("a \\\nb", "ccxxc"),
        ("ls # it's", "ccckkkkkk"),
        ("echo $'a\\'b'", "cccccaaaaaaa"),
        ('x "a\\"b"', "ccddeedd"),
    ],
)
def test_classify(text, want):
    assert classify(text)[0] == want


def test_a_hash_inside_a_word_is_not_a_comment():
    assert classify("a#b")[0] == "ccc"


def test_a_hash_inside_a_parameter_expansion_is_not_a_comment():
    """PR 1353 review, round 3: `${x:- #}` made the rest of the line a comment and
    hid a runner after it from the evidence gate."""
    assert shell_lex.COMMENT not in classify("echo ${x:- #}; pytest -q")[0]
    assert split_segments("echo ${x:- #}; pytest -q") == ["echo ${x:- #}", "pytest -q"]


def test_a_continuation_resumes_mid_word_or_at_a_word_start():
    """`x\\<newline># y` is the word `x#`; `x \\<newline># y` starts a comment."""
    assert split_segments("ls x\\\n# y") == ["ls x# y"]
    assert split_segments("ls \\\n# it's\ntouch x") == ["ls", "touch x"]


def test_split_with_ops_reports_the_operator_before_each_segment():
    assert shell_lex.split_with_ops("a | b && c; d") == [
        ("a", None), ("b", "|"), ("c", "&&"), ("d", ";")
    ]
    assert shell_lex.split_with_ops("a |\nb") == [("a", None), ("b", "|")]
    assert shell_lex.split_with_ops("a \\| b") == [("a \\| b", None)]


def test_words_resolve_quotes_escapes_and_continuations():
    assert words("sed -n '1,20p' \"a b\" c\\ d") == ["sed", "-n", "1,20p", "a b", "c d"]
    assert words("find . -name in \\\n-delete") == ["find", ".", "-name", "in", "-delete"]
    assert words("echo $'it\\'s' ''") == ["echo", "it's", ""]
    # inside double quotes only \\$ \\` \\" \\\\ escape; \\a stays literal
    assert words('echo "\\$x \\a"') == ["echo", "$x \\a"]
    assert words("ls # git push") == ["ls"]


def test_segments_split_only_on_code_operators():
    assert split_segments("ls & git push") == ["ls", "git push"]
    assert split_segments("ls 2>&1 | grep x") == ["ls 2>&1", "grep x"]
    assert split_segments("ls &>f") == ["ls &>f"]
    assert split_segments("echo it\\'s; touch x") == ["echo it\\'s", "touch x"]
    assert split_segments("cd . # don't\ngit commit") == ["cd .", "git commit"]
    assert split_segments('grep "a|b" f') == ['grep "a|b" f']


def test_substitution():
    assert has_substitution('echo "it\'s $(x)"')
    assert has_substitution("echo `x`")
    assert has_substitution("cat <(x)")
    assert not has_substitution("echo '$(x)'")
    assert not has_substitution("echo \\$(x)")
    assert not has_substitution('echo "<(x)"')  # process substitution is code-only
    assert not has_substitution("echo $((1+2))")
    assert not has_substitution("ls # $(x)")
    assert has_substitution("echo $\\\n(touch x)")  # bash removes the continuation first


def test_property_the_line_mask_agrees_with_the_whole_text_scan():
    """The gate masks line by line, threading state; the splitter scans the whole
    text. The Critical in the PR 1353 review was these two disagreeing. Over
    generated commands, every character's code/not-code verdict must match."""
    pieces = [
        "ls",
        "'a'",
        '"b"',
        "it\\'s",
        '"5\\" x"',
        "$'c\\'d'",
        "# don't",
        "x\\",
        " \\",
        "${x:- #}",
        "${",
        "}",
        "<<EOF",
        ";",
        "&",
        " ",
    ]
    for combo in itertools.product(pieces, repeat=3):
        for sep in (" ", "\n"):
            text = sep.join(combo)
            whole, _ = classify(text)
            state = None
            masked = []
            for line in text.split("\n"):
                m, state = mask_line(line, state)
                masked.append(m)
            joined = "\n".join(masked)
            for k, ch in enumerate(text):
                if ch == "\n":
                    continue
                assert (joined[k] != "\x00") == (whole[k] == shell_lex.CODE), (text, k)
