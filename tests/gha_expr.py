"""A tiny evaluator for the GitHub Actions expression subset our guards use.

WHY THIS EXISTS AND WHY IT IS TRUSTWORTHY (techne#590). Inverting the fork guard
to an allow-list makes a claim that cannot be checked by string matching: that an
UNKNOWN event fails closed. Demonstrating it needs the expression actually
evaluated under a synthetic context, which means an evaluator.

An evaluator is only worth having if it agrees with GitHub. This one is validated
against `tests/fixtures/github/expression_truth_table.json` — values GitHub's own
evaluator produced under a real `pull_request` run — so the tests that use it rest
on measurement rather than on my reading of the docs. This repo derived these
semantics wrongly TWICE from the reference pages before measuring; the fixture is
the reason we stopped guessing, and this module inherits that.

Supported: `==`, `!=`, `&&`, `||`, `!`, parentheses, string literals, `true`/`false`,
and dotted context lookups. An ABSENT property evaluates to None, which is falsy and
compares unequal to any string — the measured behaviour, not an assumption.
"""

from __future__ import annotations

import ast
import re


def _lookup(context: dict, path: str):
    """Dotted lookup. A missing segment yields None — GitHub's `null` (measured)."""
    cur: object = context
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


_TOKEN = re.compile(
    r"""\s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<and>&&)
      | (?P<or>\|\|)
      | (?P<eq>==)
      | (?P<ne>!=)
      | (?P<not>!)
      | (?P<str>'(?:[^']*)')
      | (?P<path>[A-Za-z_][A-Za-z0-9_.\-]*)
    )""",
    re.X,
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            if expr[pos:].strip() == "":
                break
            raise ValueError(f"unlexable at {pos}: {expr[pos : pos + 20]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _Parser:
    """Recursive descent: or > and > not > comparison > primary."""

    def __init__(self, tokens, context):
        self.t, self.i, self.ctx = tokens, 0, context

    def peek(self):
        return self.t[self.i][0] if self.i < len(self.t) else None

    def eat(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def parse(self):
        v = self.or_()
        assert self.i == len(self.t), f"trailing tokens at {self.i}"
        return v

    def or_(self):
        left = self.and_()
        while self.peek() == "or":
            self.eat()
            right = self.and_()
            left = _truthy(left) or _truthy(right)
        return left

    def and_(self):
        left = self.not_()
        while self.peek() == "and":
            self.eat()
            right = self.not_()
            left = _truthy(left) and _truthy(right)
        return left

    def not_(self):
        if self.peek() == "not":
            self.eat()
            return not _truthy(self.not_())
        return self.cmp_()

    def cmp_(self):
        left = self.primary()
        if self.peek() in ("eq", "ne"):
            op = self.eat()[0]
            right = self.primary()
            return _eq(left, right) if op == "eq" else not _eq(left, right)
        return left

    def primary(self):
        kind, text = self.eat()
        if kind == "lparen":
            v = self.or_()
            assert self.eat()[0] == "rparen", "unbalanced parentheses"
            return v
        if kind == "str":
            return ast.literal_eval(text)
        if kind == "path":
            if text == "true":
                return True
            if text == "false":
                return False
            return _lookup(self.ctx, text)
        raise ValueError(f"unexpected token {kind}:{text}")


def _truthy(v) -> bool:
    """GitHub's falsiness for the values our guards produce: null and false."""
    return bool(v)


def _number(v) -> float:
    """GitHub's cast-to-number, used when `==` compares two different types."""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.strip()
        if t == "":
            return 0.0
        try:
            return float(t)
        except ValueError:
            return float("nan")
    return float("nan")


def _eq(a, b) -> bool:
    """GitHub's `==`, which is NOT Python's — and the difference is load-bearing.

    GitHub casts to a number when the operands are different types, so
    `null == false` is `0 == 0` -> TRUE. Python says False. MEASURED, not read
    off a doc page: the truth-table fixture records `<absent> == false: true`,
    and `test_gha_expr_evaluator` fails if this drifts from it.

    That exact comparison is why `head.repo.fork == false` is NOT a usable fork
    guard — a typo'd or absent property equals false, so the guard fails OPEN.
    An evaluator using Python equality would have called that guard safe.
    """
    if type(a) is type(b) or (isinstance(a, str) and isinstance(b, str)):
        return a == b
    na, nb = _number(a), _number(b)
    return na == nb  # NaN != NaN, which is the intended outcome for a non-numeric string


def evaluate(expr: str, context: dict):
    """Evaluate a `${{ ... }}` expression (wrapper optional) against *context*."""
    inner = expr.strip()
    if inner.startswith("${{") and inner.endswith("}}"):
        inner = inner[3:-2]
    return _Parser(_tokenize(inner), context).parse()
