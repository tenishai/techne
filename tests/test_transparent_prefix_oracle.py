"""The `_TRANSPARENT_PREFIXES` table, executed against the real utilities (issue #740).

`_getopt_cluster_advance` is pinned against stdlib getopt elsewhere; that proves the
RULE. This proves each ROW: for every spelling, the real binary must exec the wrapped
command (a marker script prints), and `_skip_transparent_prefixes` must land on that
command's argv index. A utility missing from this machine skips its spellings, so the
Linux CI runner is where `timeout`, `stdbuf`, `ionice` and `setsid` actually run.

The table is a GNU/BSD union, like `_ENV_VALUE_LETTERS`. Spellings marked GNU-only
(BSD `nice` rejects `--adjustment`) are skipped unless `<util> --version` identifies
GNU coreutils or util-linux, rather than asserted everywhere.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from techne.hooks.post_tool_use import _skip_transparent_prefixes

MARKER = "TECHNE_PREFIX_ORACLE"

# (argv before the wrapped command, gnu_only)
SPELLINGS: list[tuple[list[str], bool]] = [
    (["nohup"], False),
    (["setsid"], False),
    (["setsid", "-w"], False),
    (["nice", "-n", "5"], False),
    (["nice", "-n5"], False),
    (["nice", "--adjustment=5"], True),
    (["nice", "--adjustment", "5"], True),
    (["stdbuf", "-o0"], False),
    (["stdbuf", "-oL"], False),
    (["stdbuf", "-o", "L"], False),
    (["stdbuf", "-i0", "-e0"], False),
    (["stdbuf", "--output=L"], False),
    (["stdbuf", "--output", "L"], False),
    (["timeout", "60"], False),
    (["timeout", "-s", "KILL", "60"], False),
    (["timeout", "-sKILL", "60"], False),
    (["timeout", "-vs", "TERM", "60"], False),
    (["timeout", "-k", "5", "60"], False),
    (["timeout", "-k5", "60"], False),
    (["timeout", "--signal=TERM", "60"], False),
    (["timeout", "--signal", "TERM", "60"], False),
    (["timeout", "--kill-after", "5", "60"], False),
    (["timeout", "--preserve-status", "60"], False),
    (["timeout", "--foreground", "60"], False),
    (["ionice", "-c", "3"], False),
    (["ionice", "-c3"], False),
    (["ionice", "-tc", "3"], False),
    (["ionice", "-c", "2", "-n", "7"], False),
    (["ionice", "--class", "2", "--classdata", "7"], False),
    (["nice", "-n", "5", "timeout", "60"], False),
]


def _is_gnu(util: str) -> bool:
    try:
        r = subprocess.run([util, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = r.stdout + r.stderr
    return r.returncode == 0 and ("GNU" in text or "util-linux" in text)


@pytest.mark.parametrize(("prefix", "gnu_only"), SPELLINGS, ids=[" ".join(p) for p, _ in SPELLINGS])
def test_the_real_utility_runs_the_command_the_table_finds(tmp_path, prefix, gnu_only):
    script = tmp_path / "marker.sh"
    script.write_text(f'#!/bin/sh\necho {MARKER} "$@"\n')
    script.chmod(0o755)
    argv = [*prefix, str(script), "arg"]

    # The table's half runs everywhere; only the execution needs the binary.
    assert _skip_transparent_prefixes(argv) == len(prefix), (
        f"the table resolves {' '.join(prefix)} to {argv[_skip_transparent_prefixes(argv)]!r}"
    )

    utils = [t for t in prefix if t in {"nohup", "setsid", "nice", "stdbuf", "timeout", "ionice"}]
    for util in utils:
        if shutil.which(util) is None:
            pytest.skip(f"{util} is not installed here")
        if gnu_only and not _is_gnu(util):
            pytest.skip(f"{' '.join(prefix)} is a GNU-only spelling; this {util} is not GNU")

    r = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd=tmp_path)
    assert f"{MARKER} arg" in r.stdout, (
        f"{' '.join(prefix)} did not exec the command the table points at "
        f"(rc={r.returncode}, stderr={r.stderr.strip()!r})"
    )
