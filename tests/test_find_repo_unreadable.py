"""find_repo degrades on an unreadable path instead of aborting detection (techne#963).

Reported by Grip: one session lost thread detection for all 201 of its
observations on `[Errno 13] Permission denied: '/Library/Sentinel/.../.git'`.

Detection walks up from the cwd RECORDED IN THE TRANSCRIPT, which on a
historical corpus may be gone, belong to another machine, or be readable only
by root. `find_repo` is the only filesystem call on the whole detection path,
so whatever it raises takes the session's threads with it.

WHY IT REACHED PRODUCTION: the code relied on `Path.exists()` swallowing the
error, and that guarantee is version-dependent. Measured on this box:

    3.11/3.12/3.13:  exists() RAISED PermissionError [Errno 13]
    3.14:            exists() -> False

`requires-python` is >=3.11 and CI runs 3.12/3.13, so the only interpreter
where the old code was safe is the 3.14 we develop on.

These tests inject a raising probe rather than chmod-ing a directory, so they
have teeth on EVERY interpreter including 3.14, and they stay meaningful if
pathlib changes again. They also run as root, where chmod 000 would not bite.
"""

from __future__ import annotations

from pathlib import Path

from techne.core.threads.features import extract_features, find_repo


def _denying_probe(_p: Path, _m: str) -> bool:
    raise PermissionError(13, "Permission denied")


def test_find_repo_returns_none_when_every_probe_denies():
    """The reported shape: nothing readable anywhere up the walk."""
    path = "/Library/Sentinel/_sentinel/temporary/x/file.py"
    assert find_repo(path, fs_check=_denying_probe) is None


def test_find_repo_keeps_walking_past_an_unreadable_ancestor():
    """An unreadable directory is 'no marker here', not 'stop looking'.

    Degrading the whole walk to None on the first denial would be a second bug:
    a repo above the unreadable directory is still the right answer.
    """

    def probe(p: Path, m: str) -> bool:
        if p.name == "locked":
            raise PermissionError(13, "Permission denied")
        return p.name == "myrepo" and m == ".git"

    assert find_repo("/home/me/myrepo/locked/deep/file.py", fs_check=probe) == "myrepo"


def test_extract_features_survives_a_denying_probe():
    """The caller's contract: features still come back, just without repo context."""
    obs = {
        "tool_name": "Bash",
        "content": "ran /Library/Sentinel/_sentinel/temporary/abc/run.sh",
        "timestamp": "2026-09-17T10:00:00",
    }
    feats = extract_features(0, obs, fs_check=_denying_probe)
    assert feats.repos == set(), "an unreadable path must yield no repo, not an exception"
    assert feats.tool_family, "the rest of the feature vector must still be built"


def test_probe_that_raises_oserror_subclass_is_also_caught():
    """A future probe that OPENS .git instead of stat-ing it must not reintroduce this."""

    def probe(_p: Path, _m: str) -> bool:
        raise IsADirectoryError(21, "Is a directory")

    assert find_repo("/a/b/c/file.py", fs_check=probe) is None
