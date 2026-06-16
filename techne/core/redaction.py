"""Sink-boundary credential redaction.

Pattern-match pass over free-text content before SQLite writes so credentials
never persist to disk. Each pattern maps to a tagged replacement token so
downstream consumers know what was stripped.
"""

import re

# (pattern, replacement_tag) pairs — order matters: more specific first.
_PATTERNS: list[tuple[str, str]] = [
    # Anthropic API keys (sk-ant-api03-... style)
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "sk-ant"),
    # OpenAI-style keys (sk-proj-... and plain sk-...)
    (r"sk-proj-[A-Za-z0-9_-]{20,}", "sk"),
    (r"sk-[A-Za-z0-9]{20,}", "sk"),
    # GitHub personal access tokens
    (r"ghp_[A-Za-z0-9]{20,}", "github"),
    # Slack bot tokens
    (r"xoxb-[A-Za-z0-9-]+", "slack"),
    # Slack user/app tokens (bonus)
    (r"xoxp-[A-Za-z0-9-]+", "slack"),
    (r"xoxa-[A-Za-z0-9-]+", "slack"),
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
