"""Tests for sink-boundary credential redaction (issue #17)."""


import pytest

from techne.core.database import init_db
from techne.core.knowledge import list_entries, store
from techne.core.redaction import redact_credentials
from techne.core.state import add_observation, get_observations, start_session

# ---------------------------------------------------------------------------
# Unit tests — one per pattern
# ---------------------------------------------------------------------------


def test_redact_sk_key():
    text = "API key is sk-abcdefghijklmnopqrstuvwxyz12345 and nothing else"
    result = redact_credentials(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz12345" not in result
    assert "<redacted:sk>" in result


def test_redact_sk_key_short_no_match():
    # Under 20 chars after prefix — should NOT be redacted
    text = "sk-tooshort"
    assert redact_credentials(text) == text


def test_redact_anthropic_key():
    text = "Using sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 here"
    result = redact_credentials(text)
    assert "sk-ant-api03" not in result
    assert "<redacted:sk-ant>" in result


def test_redact_github_pat():
    text = "token ghp_abcdefghijklmnopqrstuvwxyz123456"
    result = redact_credentials(text)
    assert "ghp_" not in result
    assert "<redacted:github>" in result


def test_redact_github_pat_short_no_match():
    text = "ghp_short"
    assert redact_credentials(text) == text


def test_redact_slack_bot_token():
    text = "slack token xoxb-1234567890-abcdefghijklmno used here"
    result = redact_credentials(text)
    assert "xoxb-" not in result
    assert "<redacted:slack>" in result


def test_redact_slack_user_token():
    text = "xoxp-1234567890-abcdefghijklmno"
    result = redact_credentials(text)
    assert "xoxp-" not in result
    assert "<redacted:slack>" in result


def test_redact_jwt():
    token = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    result = redact_credentials(token)
    assert token not in result
    assert "<redacted:jwt>" in result


def test_redact_jwt_only_two_dots_no_match():
    # Two-segment token — not JWT shape
    text = "eyJhbGci.eyJzdWIi"
    assert redact_credentials(text) == text


def test_redact_env_key():
    text = "export AWS_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE"
    result = redact_credentials(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "<redacted:env>" in result


def test_redact_env_token():
    text = "GITHUB_TOKEN=ghp_xxxx STRIPE_SECRET=sk_live_yyyy"
    result = redact_credentials(text)
    assert "ghp_xxxx" not in result
    assert "sk_live_yyyy" not in result
    assert result.count("<redacted:env>") == 2


def test_redact_env_key_lowercase_no_match():
    # env pattern requires uppercase names
    text = "some_key=value"
    assert redact_credentials(text) == text


def test_no_false_positive_plain_text():
    text = "This is a normal sentence with no credentials in it."
    assert redact_credentials(text) == text


def test_multiple_patterns_in_one_string():
    text = "key=sk-abcdefghijklmnopqrstuvwxyz1 and xoxb-111-aaa and ghp_abcdefghijklmnopqrstuvwxyz"
    result = redact_credentials(text)
    assert "<redacted:sk>" in result
    assert "<redacted:slack>" in result
    assert "<redacted:github>" in result


# ---------------------------------------------------------------------------
# Integration tests — write with credential, read back redacted
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    return db


def test_observation_content_is_redacted_at_write(tmp_db):
    start_session(tmp_db, session_id="sess-001", project_path="/tmp/proj")
    add_observation(tmp_db, "sess-001", "Bash", "Found key sk-abcdefghijklmnopqrstuvwxyz12")
    obs = get_observations(tmp_db, "sess-001")
    assert len(obs) == 1
    assert "sk-abcdefghijklmnopqrstuvwxyz12" not in obs[0]["content"]
    assert "<redacted:sk>" in obs[0]["content"]


def test_knowledge_content_is_redacted_at_write(tmp_db):
    store(
        tmp_db,
        type="fact",
        content="The API key is ghp_abcdefghijklmnopqrstuvwxyz1234",
        source="session",
        tags="",
        confidence="medium",
    )
    entries = list_entries(tmp_db)
    assert len(entries) == 1
    assert "ghp_" not in entries[0]["content"]
    assert "<redacted:github>" in entries[0]["content"]


def test_env_var_redacted_in_observation(tmp_db):
    start_session(tmp_db, session_id="sess-002", project_path="/tmp/proj2")
    add_observation(tmp_db, "sess-002", "Read", "config: STRIPE_SECRET=sk_live_supersecret123")
    obs = get_observations(tmp_db, "sess-002")
    assert "sk_live_supersecret123" not in obs[0]["content"]
    assert "<redacted:env>" in obs[0]["content"]
