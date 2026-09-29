#!/usr/bin/env bash
# Acceptance test for the INSTALLED artifact (techne#1059).
#
# Every job in test.yml installs techne editable (`uv pip install -e .`), so
# pytest always runs with the checkout importable. That can't see a packaging
# defect: a module or package-data file missing from the wheel, a wrong
# `[project.scripts]` target, or a broken `techne.sync_adapters` entry point —
# because the source tree always satisfies the import regardless of what the
# wheel actually contains. v0.16.0 already reached external partners with
# exactly this class of defect (knowledge #2836: a hook template segment
# missing from the wheel, fixed in 0.16.1).
#
# This script builds the wheel from the checkout, installs it into a FRESH venv
# (twice — once core-only, once with the `recommended` extra the README's
# blessed pipx install line uses), and `cd`s into a fresh temp dir before
# running anything, so the checkout can never shadow the installed package on
# `sys.path`. It then runs the documented smoke: `--version`, every entry
# point (tests/test_entry_points.py, standalone), two bundled guides (package
# data), and `init --help` / `doctor --help`.
#
# CALLED FROM TWO PLACES, deliberately: the `acceptance` job in
# .github/workflows/test.yml, on every PR — and docs/releasing.md's SOP, by
# hand, before tagging. Prior art: mothrag (juliangeymonat-jpg/mothrag),
# architect pattern 39f2293a7dd1a50d — its 0.6.0 declared a console_script
# whose module wasn't in the wheel, and every source-tree test stayed green.
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

    echo "  + techne --version"
    out="$("$venv/bin/techne" --version)"
    echo "    -> $out"
    got_version="$(echo "$out" | grep -Eo 'version [^ ]+' | awk '{print $2}')"
    [ "$got_version" = "$EXPECTED_VERSION" ] \
      || fail "$label: techne --version reported '$got_version', pyproject declares '$EXPECTED_VERSION' (full output: $out)"

    echo "  + entry points (console_scripts, techne.sync_adapters)"
    "$venv/bin/python" "$REPO_ROOT/tests/test_entry_points.py"

    echo "  + bundled guides (package data)"
    "$venv/bin/techne" guide investigating >/dev/null \
      || fail "$label: 'techne guide investigating' failed — package data missing from the wheel?"
    "$venv/bin/techne" guide welcome >/dev/null \
      || fail "$label: 'techne guide welcome' failed — package data missing from the wheel?"

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
