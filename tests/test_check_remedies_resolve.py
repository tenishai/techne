"""Every rail remedy names a command that exists (techne#936).

`check_maintenance_rails` built its remedy as f"techne {rail} --help", and only
one of the three rail names is a CLI verb:

    techne reflect --help       works
    techne consolidate --help   Error: No such command 'consolidate'.
    techne dream --help         Error: No such command 'dream'.

The remedy line is the one part of a check an operator acts on without reading
the code, so two of three rails handed them a dead end at exactly the moment
the check fired.

The parametrised resolver test below is the point: it catches the CLASS, not
the two instances. Remedies are free-text, so nothing else stops the next one.
"""

from __future__ import annotations

import re

import click
import pytest

from techne.cli import main
from techne.core.checks import _RAIL_EXPECTATIONS


def _leading_command(remedy: str) -> list[str]:
    """The `techne a b c` prefix of a remedy, as a list of subcommand names.

    Remedies carry prose after the command ("(bound model), or techne_x via MCP"),
    so the walk stops at the first token that is not a bare word.
    """
    head = re.split(r"[(,]", remedy, maxsplit=1)[0].strip()
    parts = head.split()
    assert parts and parts[0] == "techne", f"remedy must start with 'techne': {remedy!r}"
    out: list[str] = []
    for tok in parts[1:]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", tok):
            break
        out.append(tok)
    return out


def _resolves(path: list[str]) -> bool:
    cmd: click.Command = main
    for name in path:
        if not isinstance(cmd, click.Group):
            return False
        nxt = cmd.get_command(click.Context(cmd), name)
        if nxt is None:
            return False
        cmd = nxt
    return True


@pytest.mark.parametrize("rail", sorted(_RAIL_EXPECTATIONS))
def test_rail_remedy_resolves_to_a_real_command(rail):
    """THE REGRESSION. Before the fix, consolidate and dream failed this."""
    _interval, _note, remedy = _RAIL_EXPECTATIONS[rail]
    path = _leading_command(remedy)
    assert path, f"{rail}: remedy names no subcommand: {remedy!r}"
    assert _resolves(path), (
        f"{rail}: remedy points at `techne {' '.join(path)}`, which is not a command. "
        f"Full remedy: {remedy!r}"
    )


def test_remedy_is_not_derived_from_the_rail_name():
    """Guards the shape, not just the strings.

    If someone reverts to f"techne {rail} --help" the parametrised test above
    catches it — but only while `consolidate`/`dream` stay absent from the CLI.
    This asserts the table carries its own remedy so the coupling cannot return
    silently if a `techne dream` verb is ever added for unrelated reasons.
    """
    for rail, (_i, _n, remedy) in _RAIL_EXPECTATIONS.items():
        if rail in {"consolidate", "dream"}:
            assert remedy != f"techne {rail} --help", f"{rail}: remedy is name-derived again"
            assert "custodian run" in remedy or "_emit" in remedy, (
                f"{rail}: remedy must name the path that actually runs it: {remedy!r}"
            )
