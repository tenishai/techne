#!/usr/bin/env bash
# Refuse a commit that adds a long comment block. techne#998: code commentary is
# terse — facts, issue refs, the tradeoff in a line or two. Reasoning goes in the
# issue and the PR, which are dated and can go stale harmlessly.
#
# Refuses rather than warns; a warning is what scrolls past.
# Escape deliberately:  ALLOW_LONG_COMMENT=1 git commit ...
# Why it exists, and the measurement behind it: tenishai/agents-archive#88 and agents README,
# "Where the reasoning goes".

set -uo pipefail

MAX="${COMMENT_BLOCK_MAX:-12}"
[[ -n "${ALLOW_LONG_COMMENT:-}" ]] && exit 0

# Added lines only, per file, from the staged diff.
files=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(sh|py|ts|js|go)$' || true)
[[ -z "$files" ]] && exit 0

rc=0
for f in $files; do
  # Walk the added-line stream, counting contiguous comment runs.
  git diff --cached -U0 -- "$f" | awk -v file="$f" -v max="$MAX" '
    /^\+/ && !/^\+\+\+/ {
      line = substr($0, 2)
      if (line ~ /^[[:space:]]*(#|\/\/)/) { run++; if (run > max && run > worst) worst = run; next }
      run = 0; next
    }
    { run = 0 }
    END { if (worst > 0) printf "  %s: a %d-line comment block (max %d)\n", file, worst, max }
  '
  n=$(git diff --cached -U0 -- "$f" | awk '
    /^\+/ && !/^\+\+\+/ { line=substr($0,2); if (line ~ /^[[:space:]]*(#|\/\/)/) {run++; if(run>worst) worst=run; next} run=0; next }
    { run=0 } END { print worst+0 }')
  [[ "$n" -gt "$MAX" ]] && rc=1
done

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
