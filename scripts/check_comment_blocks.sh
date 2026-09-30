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
# ONE ARG, OPTIONAL (techne#1071): a base ref/sha to diff HEAD against, for CI —
# there is nothing staged in a checkout, so `--cached` sees nothing and the
# gate would silently pass every PR. No arg keeps the original pre-commit
# shape, diffing the INDEX, unchanged. TWO-DOT, not three-dot: CI's shallow,
# single-branch fetches can leave the base and head histories with no common
# ancestor locally (see test.yml's `origin/main` fetch step for the same
# caveat), and `git diff A...B` needs a merge base to exist. `git diff A B`
# does not — it compares the two trees directly, which is also the right
# question here ("what does HEAD add that the base does not have").
set -uo pipefail

MAX="${COMMENT_BLOCK_MAX:-12}"
[[ -n "${ALLOW_LONG_COMMENT:-}" ]] && exit 0

if [[ $# -ge 1 ]]; then
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
