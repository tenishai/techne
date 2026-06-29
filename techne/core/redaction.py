"""Sink-boundary credential redaction.

Best-effort credential filter applied at SQLite write boundaries. Patterns
cover well-formed credential tokens in free-text content (LLM/tool output).
Short structured fields (source, tags, target) are outside the threat model —
caller-supplied and low-risk by convention. This is NOT a confidentiality
guarantee; new free-text columns MUST call redact_credentials before
INSERT/UPDATE. Pattern ordering matters: more-specific prefixes first so
substrings don't capture broader matches.
"""

import re

# (pattern, replacement_tag) pairs — order matters: more specific first.
_PATTERNS: list[tuple[str, str]] = [
    # Anthropic API keys (sk-ant-api03-... style)
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "sk-ant"),
    # OpenAI-style keys (sk-proj-... and plain sk-...)
    (r"sk-proj-[A-Za-z0-9_-]{20,}", "sk"),
    (r"sk-[A-Za-z0-9]{20,}", "sk"),
    # GitHub personal access tokens (classic)
    (r"ghp_[A-Za-z0-9]{20,}", "github"),
    # GitHub Apps installation tokens
    (r"ghs_[A-Za-z0-9]{20,}", "github-apps"),
    # GitHub fine-grained PATs
    (r"github_pat_[A-Za-z0-9_]{20,}", "github-fine-grained"),
    # Stripe secret keys use UNDERSCORES: sk_live_… / sk_test_… (techne#134 — the
    # old hyphen shape `sk-live-` never matched a real key). Distinct `sk_` prefix,
    # so no conflict with the plain `sk-` pattern above.
    (r"sk_(?:live|test)_[A-Za-z0-9]{20,}", "stripe"),
    # Slack bot tokens
    (r"xoxb-[A-Za-z0-9-]+", "slack"),
    # Slack user/app tokens (bonus)
    (r"xoxp-[A-Za-z0-9-]+", "slack"),
    (r"xoxa-[A-Za-z0-9-]+", "slack"),
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
    # Env-style: SOME_KEY=value, SOME_TOKEN=value, SOME_SECRET=value
    (r"[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET)=\S+", "env"),
]

_COMPILED: list[tuple[re.Pattern[str], str]] = [(re.compile(pat), tag) for pat, tag in _PATTERNS]


def redact_credentials(text: str) -> str:
    """Replace credential-shaped strings in *text* with <redacted:type> tokens.

    Applies all patterns in order; each replacement is a single pass so
    previously-replaced tokens are not re-scanned.
    """
    for pattern, tag in _COMPILED:
        text = pattern.sub(f"<redacted:{tag}>", text)
    return text
