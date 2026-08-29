"""Packaging-contract tests: assert litellm is an optional extra, not a core dep."""

import subprocess
import sys
from pathlib import Path

import pytest


def _get_dist_requires():
    """Return (requires_list, from_pyproject) where from_pyproject is True if we
    fell back to reading pyproject.toml directly (editable-install edge case)."""
    import importlib.metadata

    reqs = importlib.metadata.requires("techne")
    if reqs is not None:
        return reqs, False

    # Fallback: parse pyproject.toml with tomllib.
    import tomllib

    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data, True


def test_litellm_not_in_core_deps():
    """litellm must NOT appear as a bare core dependency."""
    result, from_pyproject = _get_dist_requires()

    if from_pyproject:
        # result is the full parsed pyproject dict
        core_deps = result["project"]["dependencies"]
        for dep in core_deps:
            assert not dep.lower().startswith("litellm"), (
                f"litellm found in core [project.dependencies]: {dep!r}"
            )
    else:
        # result is a list of requirement strings from importlib.metadata.
        # Core deps have no 'extra ==' marker; extras do.
        # (pip normalises quotes to single-quotes in the marker string)
        for req in result:
            if req.lower().startswith("litellm") and "extra ==" not in req.lower():
                raise AssertionError(
                    f"litellm found as a bare core requirement (no extra marker): {req!r}"
                )


def test_litellm_in_llm_extra():
    """litellm must be present under the [llm] extra."""
    result, from_pyproject = _get_dist_requires()

    if from_pyproject:
        optional = result["project"]["optional-dependencies"]
        llm_deps = optional.get("llm", [])
        assert any(d.lower().startswith("litellm") for d in llm_deps), (
            "litellm not found in [project.optional-dependencies].llm"
        )
    else:
        # pip normalises quotes to single-quotes; accept both forms.
        def _is_llm_extra(req: str) -> bool:
            low = req.lower()
            return low.startswith("litellm") and (
                "extra == 'llm'" in low or 'extra == "llm"' in low
            )

        assert any(_is_llm_extra(req) for req in result), (
            "litellm not found with extra == 'llm' marker in dist metadata"
        )


def test_anthropic_extra_removed():
    """The vestigial [anthropic] extra must no longer exist."""
    result, from_pyproject = _get_dist_requires()

    if from_pyproject:
        optional = result["project"]["optional-dependencies"]
        assert "anthropic" not in optional, (
            "Vestigial [anthropic] extra still present in pyproject.toml"
        )
    else:
        for req in result:
            low = req.lower()
            assert "extra == 'anthropic'" not in low and 'extra == "anthropic"' not in low, (
                f"Vestigial anthropic extra still in dist metadata: {req!r}"
            )


def test_core_imports_do_not_load_litellm():
    """Importing core + hook modules must not pull litellm into sys.modules.

    Runs in a subprocess for full hermeticity (avoids order-sensitivity when
    another test has already imported litellm in the same process).
    """
    check_script = """
import sys
import importlib

modules_to_import = [
    "techne.core.state",
    "techne.core.search",
    "techne.core.knowledge",
    "techne.hooks.session_start",
    "techne.hooks.post_tool_use",
    "techne.mcp_server",
]
for m in modules_to_import:
    importlib.import_module(m)

if "litellm" in sys.modules:
    print("FAIL: litellm was loaded by core/hook imports", flush=True)
    sys.exit(1)
sys.exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"core/hook import path loaded litellm.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_installed_version_matches_pyproject():
    """The version a SOURCE TREE reports must be the one its own pyproject declares.

    SUPERSEDED IN MECHANISM, KEPT AS A CONTRACT (techne#825). This test used to
    assert that `techne.__version__` came from `importlib.metadata` and would go
    red for a developer on a stale editable install. It could not do that job:
    pytest runs in the repo's own `.venv`, whose dist-info is refreshed whenever
    anyone runs `pip install -e .`, while the stale stamp that actually bit us
    lived in the PIPX venv that runs the MCP server and the hooks. Measured
    2026-08-29: `.venv` dist-info 0.16.0 (green), pipx dist-info 0.15.0 against a
    0.16.0 checkout (the lie on every banner and backup manifest).

    A test cannot witness an environment it is not running in. So the mechanism
    moved into the code — `_resolve_version` reads pyproject for a checkout — and
    what remains here is the end-to-end contract that the two agree.
    """
    import tomllib

    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    if not pyproject_path.exists():
        pytest.skip("running against an installed package, not a source tree")

    with pyproject_path.open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]

    import techne

    assert techne.__version__ == declared


def test_a_checkout_reports_pyproject_not_the_dist_info_stamp(tmp_path, monkeypatch):
    """techne#825. An editable install stamps dist-info once and never moves it, so
    for a checkout the dist-info is a known lie and pyproject is the truth.

    Synthetic tree, so this witnesses the RULE rather than whichever environment
    happens to be running the suite — which is exactly the gap that let the old
    test stay green through #763 and then #825.
    """
    import techne

    pkg = tmp_path / "techne"
    pkg.mkdir()
    # `name` is written for realism only — NOTHING consults it, so a checkout
    # believes any neighbouring pyproject. Unreachable in techne's shipped install
    # shapes (pipx and pip venvs all sit under site-packages), hence not guarded.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "techne"\nversion = "9.9.9"\n')
    monkeypatch.setattr(techne, "__file__", str(pkg / "__init__.py"))

    assert techne._resolve_version() == "9.9.9"


def test_an_installed_build_still_trusts_dist_info(tmp_path, monkeypatch):
    """The checkout rule must not leak into a real install: there, dist-info IS
    authoritative and a stray pyproject next to site-packages must be ignored.

    The build test mirrors core/health.py::_revision_info deliberately — a version
    string that disagrees with the build/revision line printed beside it is the
    #825 failure itself.
    """
    import techne

    pkg = tmp_path / "site-packages" / "techne"
    pkg.mkdir(parents=True)
    (pkg.parent / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
    monkeypatch.setattr(techne, "__file__", str(pkg / "__init__.py"))

    import importlib.metadata

    # POSITIVE, not `!= "9.9.9"`: the negative also passes if the dist-info lookup
    # broke entirely and returned "0.0.0+unknown", which would witness nothing.
    assert techne._resolve_version() == importlib.metadata.version("techne")


@pytest.mark.parametrize(
    "contents",
    [
        None,  # no pyproject.toml at all
        "this is not valid toml {{{",  # unparseable
        '[project]\nname = "techne"\n',  # parseable, no version key
        '[project]\nversion = ""\n',  # present but empty
        '[[project]]\nversion = "9.9.9"\n',  # parseable, WRONG SHAPE: subscripts a list
    ],
    ids=["missing", "unparseable", "no-version-key", "empty-version", "wrong-shape"],
)
def test_an_unreadable_checkout_degrades_instead_of_raising(tmp_path, monkeypatch, contents):
    """`import techne` must not blow up because a checkout is malformed, and it must
    not quietly answer from the dist-info either.

    The `wrong-shape` case is a regression: `[[project]]` parses fine and then
    subscripts a LIST, raising TypeError — which was missing from the except tuple
    and escaped `__getattr__`. Because cli.py, mcp_server.py and core/health.py all
    do `from techne import __version__` at module scope, that was the CLI and the
    MCP server failing to import, not a degraded string.

    The assertion is `== "0.0.0+unknown"`, not merely "some string": on a checkout
    the dist-info is known-unreliable by this function's own premise, so falling
    back to it here would silently restore #825 under a `build: "checkout"` label.
    """
    import techne

    pkg = tmp_path / "techne"
    pkg.mkdir()
    if contents is not None:
        (tmp_path / "pyproject.toml").write_text(contents)
    monkeypatch.setattr(techne, "__file__", str(pkg / "__init__.py"))

    assert techne._resolve_version() == "0.0.0+unknown"


def test_a_checkout_never_answers_from_the_dist_info(tmp_path, monkeypatch):
    """The Important-2 regression, stated as the invariant it protects.

    `_revision_info` labels a non-site-packages tree `build: "checkout"` and reads a
    LIVE revision from git. If `_resolve_version` answers from `importlib.metadata`
    on that same tree, the banner prints a stale version beside a live revision
    under a "checkout" label — #825 verbatim, reached by a different route and with
    no signal that it happened. An earlier revision of this fix did exactly that, by
    falling through to the installed path when pyproject could not be read.

    This is the discriminating assertion: the checkout branch must NOT return the
    dist-info value even when it is sitting right there and would look plausible.

    It has to be synthetic. On the real tree the two sources agree (both 0.16.0),
    so an end-to-end health-payload assertion cannot tell the good behaviour from
    the bad one — which is the same reason `test_installed_version_matches_pyproject`
    was green through #763 and #825.
    """
    import importlib.metadata

    import techne

    installed = importlib.metadata.version("techne")

    pkg = tmp_path / "techne"
    pkg.mkdir()  # a checkout with NO pyproject: the branch cannot read a version
    monkeypatch.setattr(techne, "__file__", str(pkg / "__init__.py"))

    resolved = techne._resolve_version()
    assert resolved == "0.0.0+unknown"
    assert resolved != installed, (
        "the checkout branch fell back to the dist-info, which is the exact "
        "stale-version-beside-live-revision failure #825 exists to remove"
    )
