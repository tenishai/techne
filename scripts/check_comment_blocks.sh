#!/usr/bin/env bash
# Refuse a commit that adds a long comment block. techne#998: code commentary is
# terse — facts, issue refs, the tradeoff in a line or two. Reasoning goes in the
# issue and the PR, which are dated and can go stale harmlessly.
#
# Counts `#`, `//` and `/* */` runs. NOT docstrings — measured, see tenishai/agents-archive#88.
# Refuses rather than warns; a warning is what scrolls past.
# Escape deliberately:  ALLOW_LONG_COMMENT=1 git commit ...
set -uo pipefail

MAX="${COMMENT_BLOCK_MAX:-12}"
[[ -n "${ALLOW_LONG_COMMENT:-}" ]] && exit 0

rc=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  n=$(git diff --cached -U0 -- "$f" | awk '
    /^\+\+\+/ { next }
    /^\+/ {
      line = substr($0, 2); s = line
      sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s)
      is_c = 0
      if (inblk)            { is_c = 1; if (index(s, "*/")) inblk = 0 }
      else if (s ~ /^\/\*/) { is_c = 1; if (!index(s, "*/")) inblk = 1 }
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
done < <(git diff --cached --name-only --diff-filter=ACM -z | tr '\0' '\n' | grep -E '\.(sh|py|ts|js|go)$' || true)

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
