#!/usr/bin/env bash
# Run the CI workflow's checks locally, without spending GitHub Actions minutes.
#
# techne is a PRIVATE repo, so Actions minutes are metered — and as of 2026-07-25
# the org's jobs stopped starting entirely on a billing failure (since resolved;
# CI now runs on the self-hosted runner, #424). This script is the fast inner loop:
# it mirrors .github/workflows/test.yml step for step.
#
# "GREEN HERE" DOES NOT MEAN "GREEN THERE". The step sequence matches; the
# ENVIRONMENT deliberately does not, in three known ways. Two are below under WHAT
# THIS DOES NOT COVER; the third is the interpreter source, noted at the bottom.
#
#   ./scripts/ci-local.sh          # test job on the full 3.12/3.13 matrix
#   ./scripts/ci-local.sh 3.13     # one interpreter
#   ./scripts/ci-local.sh --vec    # add the eval-vec job (3.14 + model2vec)
#   ./scripts/ci-local.sh --fresh  # rebuild the venvs from scratch (CI-equivalent)
#
# WHAT THIS DOES NOT COVER — this is a PRE-PUSH loop, not a substitute for CI.
# It runs on YOUR machine; the workflow runs on Linux. That gap is not theoretical:
# tests/test_coord.py passed here and failed in CI for three days because low PIDs
# exist as root-owned daemons on macOS (os.kill -> PermissionError -> "alive") but
# not in a Linux container (ProcessLookupError -> "dead"). This script correctly
# reported ALL GREEN throughout. Local green is not CI green — push and let the
# self-hosted runner have the last word.
#
# A REUSED VENV CAN STAY GREEN WHERE CI GOES RED. `uv pip install -e` adds and
# upgrades, but does NOT uninstall a package that has since been REMOVED from
# pyproject.toml — so a stale .venv-ci-$PY keeps satisfying an import that CI's
# always-fresh venv no longer provides. Use --fresh before trusting a green run
# that follows a dependency change.
#
# Interpreters come from `uv python`, which DOWNLOADS a missing version rather than
# failing — the point is to test the versions CI gates, not the one that happens to
# be installed. NOTE THIS IS THE OPPOSITE OF CI, which pins UV_PYTHON_PREFERENCE=
# only-system and UV_PYTHON_DOWNLOADS=never (test.yml:42-43) precisely so it cannot
# silently run an interpreter nobody chose. The divergence is deliberate — a dev box
# should not need 3.14 pre-baked — but it means the interpreter you test on here is
# not provably the interpreter CI used.

set -uo pipefail
cd "$(dirname "$0")/.."

MATRIX=(3.12 3.13)   # keep in sync with test.yml's strategy.matrix.python-version
RUN_VEC=0
FRESH=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --vec)   RUN_VEC=1 ;;
    --fresh) FRESH=1 ;;
    3.*)     ARGS+=("$a") ;;
    *)       echo "usage: $0 [3.12|3.13 ...] [--vec] [--fresh]" >&2; exit 2 ;;
  esac
done
[ ${#ARGS[@]} -gt 0 ] && MATRIX=("${ARGS[@]}")

command -v uv >/dev/null || { echo "FATAL: uv not found (brew install uv)" >&2; exit 1; }

FAILED=()
step() { printf '\n\033[1m── %s\033[0m\n' "$*"; }

# --- job: test (matrix 3.12, 3.13) -------------------------------------------
for PY in "${MATRIX[@]}"; do
  step "job: test — Python $PY"
  VENV=".venv-ci-$PY"
  # Reuse an existing venv rather than recreating it — `uv venv` ERRORS on a
  # populated target, which on any second run made this script bail here and skip
  # the tests entirely (found by deliberately breaking a test and watching it
  # report "venv" instead). The reinstall below is idempotent and fast, so a reused
  # venv still converges on the declared deps; `--fresh` forces a clean rebuild.
  if [ "$FRESH" = 1 ] || [ ! -x "$VENV/bin/python" ]; then
    uv venv --python "$PY" "$VENV" --clear --quiet || { FAILED+=("$PY: venv"); continue; }
  fi
  # Mirrors the workflow's `pip install -e ".[dev,codex]"` — the codex extra brings
  # the tree-sitter grammars so codex extraction tests RUN rather than skip.
  uv pip install --python "$VENV/bin/python" -e ".[dev,codex]" --quiet \
    || { FAILED+=("$PY: install"); continue; }

  # WHICH BUILD DID uv ACTUALLY RESOLVE? (techne#773) Not cosmetic: `uv venv
  # --python 3.13` silently downloads a python-build-standalone interpreter when
  # no system 3.13 exists, and that build OMITS `time.tzset`. Two tests then
  # failed here with AttributeError while GitHub Actions stayed green — a red CI
  # cannot reproduce is the one thing a local runner does not survive. Printing
  # base_prefix turns "why is 3.13 red" into a glance.
  "$VENV/bin/python" -c "import sys; print(f'  interpreter: {sys.version.split()[0]}  ({sys.base_prefix})')"

  # Advisory in CI (`|| true`), so advisory here — reported, never fatal.
  "$VENV/bin/python" -m ruff check techne tests || echo "  (ruff advisory — non-blocking, same as CI)"

  # Parallel, mirroring the workflow (#463) — a serial local run would not
  # exercise the execution model CI now uses.
  #
  # The WORKER COUNT deliberately differs: CI pins `-n 4` because it must be
  # deterministic and because 4 is what was measured on the runner; a dev box
  # should use what it has. (NOT because it composes with replicas — it does
  # not: replicas oversubscribe at any -n.) That is safe to diverge on because worker count provably does
  # not change OUTCOMES — the #424/#463 review sharded all 2312 node ids four
  # ways, ran the shards serially and then concurrently, and got byte-identical
  # pass/fail/skip counts. Keep the FLAG in sync with test.yml; the number is
  # free.
  # `-rs` MIRRORS test.yml:360 AND IS NOT OPTIONAL HERE. The comment above says
  # to keep the flag in sync and that only the worker count is free; this line
  # had drifted off it. test.yml's own note explains the cost: "a count is not a
  # report", and it names LOCAL runs as the side that skips more (18 vs 5). The
  # tz capability skips (techne#773) fire on THIS runner and nowhere else, so
  # without `-rs` the only place they occur is the only place they are invisible
  # -- folded anonymously into a skip total under an "ALL GREEN" banner.
  "$VENV/bin/python" -m pytest -q -rs -n auto || FAILED+=("$PY: pytest")

  # CLI -m overrides pyproject's addopts exclusion. Easy to forget locally, which
  # is exactly why it's here: these gates are excluded from a bare `pytest` run.
  "$VENV/bin/python" -m pytest -m "eval or eval_gate" -q || FAILED+=("$PY: eval gates")
done

# --- job: eval-vec (3.14 + model2vec) ----------------------------------------
if [ "$RUN_VEC" = 1 ]; then
  step "job: eval-vec — Python 3.14"
  VENV=".venv-ci-vec"
  # GUARD THE VENV STEP, exactly like the matrix loop above. Without this, a failed
  # `uv venv --python 3.14` fell straight through into `uv pip install --python
  # .venv-ci-vec/bin/python`, which failed too and got recorded as "vec: install" —
  # the WRONG DIAGNOSIS — and then ran two more doomed steps. The exit code was
  # still correctly non-zero; only the reported cause was wrong, which is the more
  # expensive kind of wrong because it sends you to the wrong file.
  # The matrix loop can say `continue`; this block is not a loop, so each step is
  # gated on the previous one having worked.
  vec_ok=1
  if [ "$FRESH" = 1 ] || [ ! -x "$VENV/bin/python" ]; then
    uv venv --python 3.14 "$VENV" --clear --quiet || { FAILED+=("vec: venv"); vec_ok=0; }
  fi
  [ "$vec_ok" = 1 ] && { uv pip install --python "$VENV/bin/python" -e ".[dev,codex,vectors]" --quiet \
    || { FAILED+=("vec: install"); vec_ok=0; }; }
  # The eval_vec fixture SKIPS unless the pinned model is already cached (it probes
  # with allow_download=False), so fetch it first or the gate silently no-ops.
  [ "$vec_ok" = 1 ] && { "$VENV/bin/python" -c "from model2vec import StaticModel; StaticModel.from_pretrained('minishlab/potion-base-8M', force_download=False)" \
    || { FAILED+=("vec: model fetch"); vec_ok=0; }; }
  [ "$vec_ok" = 1 ] && { "$VENV/bin/python" -m pytest -m eval_vec -s || FAILED+=("vec: eval_vec gate"); }
fi

# --- verdict ------------------------------------------------------------------
echo
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '\033[32mALL GREEN\033[0m — %s\n' "${MATRIX[*]}$([ "$RUN_VEC" = 1 ] && echo ' + eval-vec')"
else
  printf '\033[31mFAILED:\033[0m %s\n' "${FAILED[*]}"
  exit 1
fi
