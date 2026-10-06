#!/usr/bin/env bash
# Acceptance test for the INSTALLED artifact — the packaging defect class an
# editable install can't see (missing package data, a broken entry point).
# Builds the wheel, installs it into fresh venvs (core, then the
# `recommended` extra), and smoke-tests each from a dir that can't shadow the
# install with the checkout. Full rationale and the incident that prompted
# this: techne#1059.
#
# Called from the `acceptance` job in .github/workflows/test.yml on every PR,
# and by hand from docs/releasing.md's SOP before tagging.
#
# Usage:
#   scripts/acceptance.sh                 # Python 3.12, the `recommended` extra
#   ACCEPTANCE_PYTHON=3.13 scripts/acceptance.sh
#   ACCEPTANCE_EXTRA=vectors scripts/acceptance.sh   # test a different extra

set -euo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

PYTHON_VERSION="${ACCEPTANCE_PYTHON:-3.12}"
EXTRA="${ACCEPTANCE_EXTRA:-recommended}"

command -v uv >/dev/null || { echo "FATAL: uv not found (brew install uv)" >&2; exit 1; }

step() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# --- build ---------------------------------------------------------------
BUILD_DIR="$WORKDIR/dist"
step "Build wheel from $REPO_ROOT"
uv build --wheel --out-dir "$BUILD_DIR" "$REPO_ROOT"
WHEEL="$(ls "$BUILD_DIR"/techne-*.whl 2>/dev/null || true)"
[ -n "$WHEEL" ] && [ -f "$WHEEL" ] || fail "no techne-*.whl produced in $BUILD_DIR"
echo "wheel: $WHEEL"

# Read the version FRESH from pyproject.toml every run — deliberately not
# cached anywhere else in this script. That is what lets this same check catch
# a stale install: build+install once, bump pyproject.toml's version without
# rebuilding, and this line reports what the checkout NOW declares, which the
# already-installed venv no longer matches (the techne#825/#763 class).
EXPECTED_VERSION="$(python3 -c '
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    print(tomllib.load(f)["project"]["version"])
' "$REPO_ROOT/pyproject.toml")"
echo "pyproject version: $EXPECTED_VERSION"

# --- one pass: install into a fresh venv, smoke-test from a fresh cwd ------
run_pass() {
  local label="$1" pip_target="$2"
  step "Pass: $label ($pip_target)"

  local venv="$WORKDIR/venv-$label"
  uv venv --python "$PYTHON_VERSION" "$venv" --clear --quiet
  uv pip install --python "$venv/bin/python" "$pip_target" --quiet

  local scratch="$WORKDIR/run-$label"
  mkdir -p "$scratch"
  (
    cd "$scratch"
    # Never the developer's real store, and never the CI runner's ambient one
    # either — a fresh, scoped-to-this-pass directory under $WORKDIR.
    export TECHNE_HOME="$scratch/techne-home"

    # Smoke only — build_identity's docstring says the string this prints must
    # never be parsed, so this script never does; see the pyproject-comparison
    # step below instead.
    echo "  + techne --version"
    out="$("$venv/bin/techne" --version)"
    echo "    -> $out"

    echo "  + installed package version matches pyproject"
    got_version="$("$venv/bin/python" -c "import importlib.metadata as m; print(m.version('tenish-techne'))")"
    [ "$got_version" = "$EXPECTED_VERSION" ] \
      || fail "$label: importlib.metadata reports tenish-techne==$got_version, pyproject declares $EXPECTED_VERSION"

    echo "  + entry points (console_scripts, techne.sync_adapters)"
    "$venv/bin/python" "$REPO_ROOT/tests/test_entry_points.py"

    echo "  + bundled guides (package data) — every techne.docs _MANIFEST entry"
    "$venv/bin/python" -c "
from techne.docs import _MANIFEST, bundled_guide

for name in _MANIFEST:
    assert len(bundled_guide(name)) > 500, f'{name}: resolved to something implausibly short'
print(f'    -> {len(_MANIFEST)} guides present')
" || fail "$label: a bundled guide is missing from the wheel"

    echo "  + hook modules (package files) — every module a registered hook bakes in"
    "$venv/bin/python" -c "
import importlib

from techne.platforms.claude_code.hooks import _BAKED_IN_MODULES

for module in _BAKED_IN_MODULES:
    importlib.import_module(module)
print(f'    -> {len(_BAKED_IN_MODULES)} hook modules import cleanly')
" || fail "$label: a hook module is missing from the wheel"

    echo "  + CLI wiring"
    "$venv/bin/techne" init --help >/dev/null || fail "$label: 'techne init --help' failed"
    "$venv/bin/techne" doctor --help >/dev/null || fail "$label: 'techne doctor --help' failed"

    echo "  + techne.__file__ is the installed copy, not the checkout"
    "$venv/bin/python" -c "
import os
import techne

resolved = os.path.realpath(techne.__file__)
venv_root = os.path.realpath('$venv')
repo_root = os.path.realpath('$REPO_ROOT')

assert resolved.startswith(venv_root + os.sep), (
    f'techne.__file__ {resolved!r} is not inside the venv {venv_root!r} — '
    'something on sys.path is shadowing the installed package'
)
assert not resolved.startswith(repo_root + os.sep), (
    f'techne.__file__ {resolved!r} resolves inside the checkout {repo_root!r} — '
    'this run is testing the source tree, not the installed artifact'
)
print('    ->', resolved)
"
  )
  echo "  $label: OK"
}

run_pass core "$WHEEL"
run_pass "$EXTRA" "${WHEEL}[$EXTRA]"

echo
printf '\033[32mALL GREEN\033[0m — installed-artifact acceptance (core + [%s])\n' "$EXTRA"
