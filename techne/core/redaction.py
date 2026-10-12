"""Sink-boundary credential redaction.

Best-effort credential filter applied at SQLite write boundaries. Patterns
cover well-formed credential tokens in free-text content (LLM/tool output).
Short structured fields (source, tags, target) are outside the threat model —
caller-supplied and low-risk by convention. This is NOT a confidentiality
guarantee; new free-text columns MUST call redact_credentials before
INSERT/UPDATE. Pattern ordering matters: more-specific prefixes first so
substrings don't capture broader matches.

Intentionally NOT redacted (by design, not oversight):
  - the architect `patterns` store — operator-curated architectural notes, not
    a credential sink; redaction there would corrupt deliberately-authored text.
  - the codex code-graph store — repo symbols/spans extracted from source the
    operator already controls, not captured tool/LLM output.
Both are opt-in domain stores queried deliberately, never fed verbatim tool
output the way observations / summaries / cards / documents are.
"""

import re

# (pattern, replacement_tag) pairs — order matters: more specific first.
_PATTERNS: list[tuple[str, str]] = [
    # Anthropic API keys (sk-ant-api03-... style)
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "sk-ant"),
    # OpenAI-style keys (sk-proj-... and plain sk-...)
    (r"sk-proj-[A-Za-z0-9_-]{20,}", "sk"),
    # OpenRouter (sk-or-v1-…): the hyphens defeat the plain sk- pattern below
    (r"sk-or-[A-Za-z0-9_-]{20,}", "sk"),
    (r"sk-[A-Za-z0-9]{20,}", "sk"),
    # GitHub personal access tokens (classic)
    (r"ghp_[A-Za-z0-9]{20,}", "github"),
    # GitHub Apps installation tokens
    (r"ghs_[A-Za-z0-9]{20,}", "github-apps"),
    # GitHub fine-grained PATs
    (r"github_pat_[A-Za-z0-9_]{20,}", "github-fine-grained"),
    # GitHub OAuth (gho_, what `gh auth token` prints), user-to-server (ghu_) and
    # refresh (ghr_) tokens (techne#1245)
    (r"gh[our]_[A-Za-z0-9]{20,}", "github-oauth"),
    # GitLab personal access tokens and npm automation tokens (techne#1245)
    (r"glpat-[A-Za-z0-9_-]{20,}", "gitlab"),
    (r"npm_[A-Za-z0-9]{30,}", "npm"),
    # Stripe secret keys use UNDERSCORES: sk_live_… / sk_test_… (techne#134 — the
    # old hyphen shape `sk-live-` never matched a real key). Distinct `sk_` prefix,
    # so no conflict with the plain `sk-` pattern above.
    (r"sk_(?:live|test)_[A-Za-z0-9]{20,}", "stripe"),
    # Slack bot tokens
    (r"xoxb-[A-Za-z0-9-]+", "slack"),
    # Slack user/app/session/refresh tokens
    (r"xox[pasr]-[A-Za-z0-9-]+", "slack"),
    # AWS access key IDs (AKIA…) and temporary/STS keys (ASIA…)
    (r"A(?:KIA|SIA)[0-9A-Z]{16}", "aws-access-key"),
    # AWS secret access keys: 40-char base64. The bare shape is too common
    # (sha1 hex, other tokens) to redact unconditionally, so anchor on a
    # "secret[ access][ key]: <value>" label immediately preceding it. The
    # optional quote before the separator covers the JSON/SDK form
    # `"SecretAccessKey": "wJalr…"` (boto3/STS dumps), not just `key: value`.
    (
        r"(?i)secret(?:[ _-]?access)?(?:[ _-]?key)?['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}",
        "aws-secret",
    ),
    # JWT shape: three base64url segments
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "jwt"),
    # HuggingFace API tokens (hf_…) — techne#243
    (r"hf_[A-Za-z0-9]{20,}", "hf-token"),
    # Google API keys (AIzaSy… prefix, 39 chars total: 6-char prefix + 33 chars)
    # — techne#243
    (r"AIzaSy[A-Za-z0-9_-]{33}", "google-api-key"),
    # PEM private key blocks (techne#243). [\s\S]{0,8192}? matches the base64
    # body across newlines without requiring re.DOTALL. Covers RSA, EC,
    # ENCRYPTED, and plain PRIVATE KEY headers. The span is BOUNDED to 8KB
    # (a real PEM key body is well under that) so a BEGIN opener with no END
    # closer can't force an O(n²) rescan-to-EOF per opener — redaction now runs
    # on semi-external, unbounded GitHub issue bodies (github_issues.py).
    (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z ]*PRIVATE KEY-----",
        "pem-private-key",
    ),
    # HTTP Authorization header: Bearer token (techne#243). Requires ≥20 non-
    # whitespace chars after "Bearer " to avoid false positives on short
    # placeholder values. JWTs are already caught by the jwt pattern above, so
    # this primarily catches opaque OAuth2 / API gateway tokens.
    (r"(?i)Authorization:\s*Bearer\s+\S{20,}", "bearer"),
    # Other Authorization schemes (techne#1245). Basic is base64(user:password),
    # so it needs no length floor beyond a non-placeholder 8 chars.
    (r"(?i)Authorization:\s*(?:Basic|Token|Digest)\s+\S{8,}", "http-auth"),
    # Credentials in a URL: scheme://user:password@host (psql, git remotes,
    # redis, pip index URLs). Only the userinfo is replaced; the host stays
    # readable (techne#1245).
    (r"(?<=://)[^/\s:@]+:[^@\s/]+(?=@)", "url-userinfo"),
    # curl/wget basic auth: -u user:pass / --user user:pass (techne#1245). The
    # colon is required, so `git push -u origin main` is untouched.
    (r"(?<=\s-u )[^\s:$]+:\S+", "basic-auth"),
    (r"(?<=--user[ =])[^\s:$]+:\S+", "basic-auth"),
    # Env-style (ahead of the lowercase forms so an UPPER_CASE name keeps the
    # `env` tag): SOME_KEY=value, SOME_TOKEN=value, SOME_SECRET=value,
    # SOME_PASSWORD=value (PASSWORD added in techne#1245)
    (r"[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)=\S+", "env"),
    # Lowercase / JSON credential assignments (techne#243). The env pattern above
    # only covers UPPERCASE names; these fill the gap for JSON output and shell
    # assignments. Require ≥8 non-whitespace chars for the value to stay above
    # the false-positive threshold for short placeholder/test strings.
    #   bare form:  api_key=longvalue123  or  password=Hunter2!x
    # The `=` must have NO surrounding whitespace — that's the real shell/env/CLI
    # shape (`export secret=…`, `--password=…`). Allowing spaces (`key = value`)
    # over-redacts prose and code (`password = get_password()`), which now flows
    # through the LLM-prose sinks this pass wired up (decision_reason, summaries).
    #
    # The name may carry a prefix (`db_password=`, `DB_PASSWORD=`, `GITHUB_TOKEN=`):
    # `\b` before `password` used to fail after `_`, which let every prefixed
    # name through (techne#1245). Bare `token=` and `passwd=` were missing. Not
    # matched: pagination cursors (`page_token=`, `nextPageToken`), `==` version
    # pins (`tiktoken==0.7.0`), and `PWD`, which is a directory, not a password.
    (
        r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_]*(?<!page)(?<!page_)(?:api_key|password|passwd|secret|token)"
        r"=(?![=$]|<redacted:)\S{8,}",
        "lowered-credential",
    ),
    #   JSON form:  "api_key": "longvalue123"  (spaces around ':' are normal JSON)
    (
        r'(?i)"[A-Za-z0-9_]*(?<!page)(?<!page_)(?:api_key|password|passwd|secret|token)"'
        r'\s*:\s*"[^"]{8,}"',
        "lowered-credential",
    ),
    # Space-separated secret flags: --password VALUE, --token VALUE,
    # --with-token VALUE (techne#1245). A `$VAR` reference is left alone: it
    # names where the secret lives, not the secret.
    # One fixed-width lookbehind per flag keeps the flag name readable. A value
    # an earlier pattern already replaced is left as it is.
    (
        r"(?i)(?:(?<=--password[ =])|(?<=--passwd[ =])|(?<=--token[ =])"
        r"|(?<=--with-token[ =])|(?<=--api-key[ =])|(?<=--secret[ =]))"
        r"(?!\$|<redacted:)\S{8,}",
        "flag-credential",
    ),
]

_COMPILED: list[tuple[re.Pattern[str], str]] = [(re.compile(pat), tag) for pat, tag in _PATTERNS]

#: Matches a token this module WROTE. Derived from the same tag set the writer
#: interpolates, so the two cannot drift — a reader that hardcoded the shape
#: would be a second copy of a format only this file owns.
#:
#: Consumers need it because redaction happens BEFORE storage and CHANGES
#: LENGTH: any inference from a stored string's size has to know whether this
#: ran (techne#441).
REDACTION_MARKER: re.Pattern[str] = re.compile(
    "<redacted:(?:%s)>" % "|".join(sorted({re.escape(tag) for _, tag in _PATTERNS}))
)


def redact_credentials(text: str) -> str:
    """Replace credential-shaped strings in *text* with <redacted:type> tokens.

    Applies all patterns in order; each replacement is a single pass so
    previously-replaced tokens are not re-scanned. Returns *text* unchanged
    if it is None (None guard — callers may pass optional free-text columns
    directly without a prior null-check).
    """
    if text is None:
        return text
    for pattern, tag in _COMPILED:
        text = pattern.sub(f"<redacted:{tag}>", text)
    return text


#: How far either side of a cut point to look for a credential that straddles
#: it. The longest bounded pattern is the 8KB PEM body; the rest are far shorter.
_CUT_WINDOW = 9000


def _straddling(text: str, pos: int) -> tuple[int, int] | None:
    """The credential match that contains ``pos`` strictly inside it, if any."""
    lo = max(0, pos - _CUT_WINDOW)
    window = text[lo : pos + _CUT_WINDOW]
    for pattern, _ in _COMPILED:
        for m in pattern.finditer(window):
            a, b = m.start() + lo, m.end() + lo
            if a < pos < b:
                return a, b
    return None


def safe_cut(text: str, n: int) -> str:
    """``text[:n]``, pulled back so the cut never lands inside a credential.

    Truncating BEFORE redaction can cut a secret mid-token, and the fragment no
    longer matches any pattern, so the sink-boundary redaction lets it through
    (techne#1245: Edit/Write previews and tool-output heads). Cutting at the
    start of the straddling match drops the secret instead; the full-text
    redaction downstream then sees no partial secret and the content_hash
    invariant of add_observation is unchanged."""
    if len(text) <= n:
        return text
    hit = _straddling(text, n)
    return text[: hit[0]] if hit else text[:n]


def safe_start(text: str, i: int) -> int:
    """A start offset at or after ``i`` that does not begin inside a credential.

    The tail counterpart of :func:`safe_cut`: a tail that starts mid-secret keeps
    the secret's end, which also matches no pattern."""
    hit = _straddling(text, i)
    return hit[1] if hit else i
