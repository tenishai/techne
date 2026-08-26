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
    """`techne.__version__` resolves through `importlib.metadata.version()` — the
    INSTALLED dist-info — not `pyproject.toml`. An editable install made at an
    older version keeps reporting that older version indefinitely, so the running
    code believes it is a release it is not.

    That value is not cosmetic: `backup_all_dbs` stamps it into every backup
    manifest as `techne_version`, which is exactly what an operator reads to
    answer "what wrote this set, and can this techne restore it?" (techne#763).

    This test is green on any fresh install — including CI, which installs from
    the tree under test — and red for precisely the person who needs to know: a
    developer on a stale editable install, whose remedy is in the message.
    """
    import tomllib

    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    if not pyproject_path.exists():
        pytest.skip("running against an installed package, not a source tree")

    with pyproject_path.open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]

    import techne

    installed = techne.__version__
    if installed == "0.0.0+unknown":
        pytest.skip("techne is not installed as a distribution")

    assert installed == declared, (
        f"techne.__version__ is {installed!r} but pyproject.toml declares "
        f"{declared!r}. The dist-info in this environment is stale, so every "
        f"backup manifest written here would be stamped {installed!r}. "
        f"Remedy: pip install -e ."
    )
