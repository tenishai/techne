#!/usr/bin/env bash
# Refuse a commit (or CI run) that adds a long comment block. techne#998: code
# commentary is terse — facts, issue refs, the tradeoff in a line or two.
# Reasoning goes in the issue and the PR, which are dated and can go stale
# harmlessly.
#
# Counts `#` and `//` runs, and `/* */` in .ts/.js/.go. NOT docstrings — see tenishai/agents-archive#88.
# Refuses rather than warns; a warning is what scrolls past.
# Escape deliberately:  ALLOW_LONG_COMMENT=1 git commit ...
#
# ONE ARG, OPTIONAL (techne#1071): a base ref/sha to diff HEAD against, for CI
# (`--cached` sees nothing in a checkout). No arg keeps the pre-commit shape,
# diffing the INDEX. TWO-DOT: CI's shallow fetch can leave no common ancestor
# (see test.yml's `origin/main` fetch step), and two-dot needs none — it
# compares trees directly ("what does HEAD add"). Known, unfixed edge cases:
# `workflow_dispatch` on a branch BEHIND main can flag a block main already
# trimmed, and the merge ref can move between checkout and fetch (a race).
# Both rare; the ref-resolution check below turns either into a hard error,
# never a silent pass.
set -uo pipefail

MAX="${COMMENT_BLOCK_MAX:-12}"
[[ -n "${ALLOW_LONG_COMMENT:-}" ]] && exit 0

if [[ $# -ge 1 ]]; then
  # FAIL CLOSED ON A BASE REF THAT DOESN'T RESOLVE (Prudence, PR #1120 review).
  # Without this, a bad/absent ref (e.g. the fetch step above it deleted, or
  # `origin/main` simply not fetched) made every `git diff` below print
  # `fatal: ambiguous argument` to stderr and return nothing — an empty file
  # list, rc=0, green, having checked nothing. That is the exact silent no-op
  # this whole issue is about, one door further in than the no-arg case the
  # tests already covered.
  git rev-parse --verify --quiet "$1^{commit}" >/dev/null || {
    echo "check_comment_blocks: base ref '$1' does not resolve" >&2
    exit 2
  }
  diff_range=("$1" "HEAD")
else
  diff_range=("--cached")
fi

rc=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  # /* */ is a comment only in the C-like languages; in .sh/.py it is a glob or text.
  case "$f" in *.ts|*.js|*.go) cstyle=1 ;; *) cstyle=0 ;; esac
  n=$(git diff "${diff_range[@]}" -U0 -- "$f" | awk -v cstyle="$cstyle" '
    /^\+\+\+/ { next }
    /^@@/ { run = 0; inblk = 0; next }
    /^\+/ {
      line = substr($0, 2); s = line
      sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s)
      is_c = 0
      if (inblk)            { is_c = 1; if (index(s, "*/")) inblk = 0 }
      else if (cstyle && s ~ /^\/\*/) { is_c = 1; if (!index(s, "*/")) inblk = 1 }
      else if (s ~ /^(#|\/\/)/) is_c = 1
      if (is_c) { run++; if (run > worst) worst = run } else run = 0
      next
    }
    { run = 0 }
    END { print worst + 0 }')
  if [[ "$n" -gt "$MAX" ]]; then
    printf '  %s: a %d-line comment block (max %d)\n' "$f" "$n" "$MAX" >&2
    rc=1
  fi
done < <(git diff "${diff_range[@]}" --name-only --diff-filter=ACM -z | tr '\0' '\n' | grep -E '\.(sh|py|ts|js|go)$' || true)

if [[ $rc -ne 0 ]]; then
  cat >&2 <<'MSG'

check_comment_blocks: this commit adds a comment block longer than the limit.

techne#998: code commentary is terse — facts, issue refs, the tradeoff in a
line or two. The reasoning belongs in the ISSUE and the PR body. A comment
reads as current forever; a PR body is dated and can go stale harmlessly.

Move the reasoning to the PR, leave a one-line pointer, and commit again.
Deliberate exception:  ALLOW_LONG_COMMENT=1 git commit ...
MSG
fi
exit $rc
