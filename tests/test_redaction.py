"""Tests for sink-boundary credential redaction (issue #17, #243, #244)."""

import pytest

from techne.core.database import init_db
from techne.core.knowledge import list_entries, store
from techne.core.redaction import redact_credentials, safe_cut
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


def test_redact_github_apps_token():
    text = "installation token ghs_abcdefghijklmnopqrstuvwxyz1234"
    result = redact_credentials(text)
    assert "ghs_" not in result
    assert "<redacted:github-apps>" in result


def test_redact_github_apps_token_short_no_match():
    text = "ghs_short"
    assert redact_credentials(text) == text


def test_redact_github_fine_grained_pat():
    text = "token github_pat_abcdefghijklmnopqrstuvwxyz_1234"
    result = redact_credentials(text)
    assert "github_pat_" not in result
    assert "<redacted:github-fine-grained>" in result


def test_redact_github_fine_grained_pat_short_no_match():
    text = "github_pat_short"
    assert redact_credentials(text) == text


def test_redact_stripe_live_key():
    # techne#134: real Stripe secret keys use underscores (sk_live_…), not hyphens.
    text = "key: sk_live_abcdefghijklmnopqrstuvwxyz1234"
    result = redact_credentials(text)
    assert "sk_live_abcdefghijklmnopqrstuvwxyz1234" not in result
    assert "<redacted:stripe>" in result


def test_redact_stripe_test_key():
    text = "key: sk_test_abcdefghijklmnopqrstuvwxyz1234"
    result = redact_credentials(text)
    assert "sk_test_abcdefghijklmnopqrstuvwxyz1234" not in result
    assert "<redacted:stripe>" in result


def test_redact_stripe_short_no_match():
    text = "sk_live_tooshort"
    assert redact_credentials(text) == text


def test_stripe_does_not_capture_anthropic_key():
    # sk-ant- must be caught by the sk-ant pattern, not the Stripe sk-live/test pattern
    text = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890"
    result = redact_credentials(text)
    assert "<redacted:sk-ant>" in result
    assert "<redacted:stripe>" not in result


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


def test_redact_aws_access_key_id():
    text = "access key AKIAIOSFODNN7EXAMPLE rotates monthly"
    result = redact_credentials(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "<redacted:aws-access-key>" in result


def test_redact_aws_sts_temporary_key():
    text = "temp creds ASIAY34FZKBOKMUTVV7A from sts"
    result = redact_credentials(text)
    assert "ASIAY34FZKBOKMUTVV7A" not in result
    assert "<redacted:aws-access-key>" in result


def test_redact_aws_access_key_short_no_match():
    # AKIA prefix but fewer than 16 trailing chars — not a key
    text = "AKIASHORT123"
    assert redact_credentials(text) == text


def test_redact_aws_secret_when_labelled():
    text = "secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY done"
    result = redact_credentials(text)
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result
    assert "<redacted:aws-secret>" in result


def test_aws_secret_unlabelled_40char_not_redacted():
    # A bare 40-char token with no secret label is too ambiguous (e.g. sha1
    # hex is 40 chars) — left alone by design to avoid false positives.
    text = "commit a1b2c3d4e5f60718293a4b5c6d7e8f9012345678 landed"
    assert redact_credentials(text) == text


def test_redact_aws_full_credential_pair():
    # The functional-scenario string that exposed issue #69.
    text = (
        "Test observation for redaction. AWS access key: AKIAIOSFODNN7EXAMPLE "
        "and secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    result = redact_credentials(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result
    assert "<redacted:aws-access-key>" in result
    assert "<redacted:aws-secret>" in result


def test_redact_aws_secret_json_sdk_form():
    # boto3/STS responses dump creds as JSON: a quote sits between the label
    # and the colon, so the separator isn't immediately after "Key".
    text = '{"AccessKeyId": "AKIAIOSFODNN7EXAMPLE", "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}'
    result = redact_credentials(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result
    assert "<redacted:aws-access-key>" in result
    assert "<redacted:aws-secret>" in result


def test_redact_aws_secret_credentials_file_form():
    # ~/.aws/credentials INI form: `aws_secret_access_key = <value>`
    text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    result = redact_credentials(text)
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result
    assert "<redacted:aws-secret>" in result


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


# ---------------------------------------------------------------------------
# #243 — new pattern unit tests
# ---------------------------------------------------------------------------


def test_redact_none_returns_none():
    """None guard: redact_credentials(None) must return None, not raise."""
    assert redact_credentials(None) is None


def test_redact_hf_token():
    text = "login with hf_abcdefghijklmnopqrstuvwx to access the model hub"
    result = redact_credentials(text)
    assert "hf_abcdefghijklmnopqrstuvwx" not in result
    assert "<redacted:hf-token>" in result


def test_redact_hf_token_short_no_match():
    # Under 20 alphanumeric chars after hf_ — should NOT be redacted
    text = "hf_tooshort"
    assert redact_credentials(text) == text


def test_hf_token_benign_prose_untouched():
    # A plain word that starts with "hf" but is not a real token
    text = "The heuristic framework (hf) paper was interesting."
    assert redact_credentials(text) == text


def test_redact_google_api_key():
    # 39 chars total: AIzaSy (6) + 33 alphanumeric chars
    # AIzaSy(6) + Abcdefghijklmnopqrstuvwxyz1234567(33) = 39
    key = "AIzaSyAbcdefghijklmnopqrstuvwxyz1234567"
    text = f"Calling Maps API with key {key}"
    result = redact_credentials(text)
    assert key not in result
    assert "<redacted:google-api-key>" in result


def test_google_api_key_short_no_match():
    # Fewer than 33 chars after AIzaSy — not a complete key
    text = "AIzaSyShortNotAKey"
    assert redact_credentials(text) == text


def test_google_api_key_benign_prefix_untouched():
    # "AIzaSy" without trailing chars — should not match
    text = "The prefix AIzaSy is interesting but this is not a key."
    assert redact_credentials(text) == text


def test_redact_pem_private_key():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA2a2rwplBQLzHPZe5TnjKEtMH\n"
        "-----END RSA PRIVATE KEY-----"
    )
    result = redact_credentials(pem)
    assert "MIIEowIBAAKCAQEA2a2rwplBQLzHPZe5TnjKEtMH" not in result
    assert "<redacted:pem-private-key>" in result


def test_redact_pem_ec_private_key():
    pem = (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHQCAQEEIONzuFbdhmEJCzMvDO5RzxwT2hJhNkAO\n"
        "-----END EC PRIVATE KEY-----"
    )
    result = redact_credentials(pem)
    assert "MHQCAQEEIONzuFbdhmEJCzMvDO5RzxwT2hJhNkAO" not in result
    assert "<redacted:pem-private-key>" in result


def test_redact_pem_plain_private_key():
    # PKCS8 / plain "PRIVATE KEY" (no algorithm prefix)
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj\n"
        "-----END PRIVATE KEY-----"
    )
    result = redact_credentials(pem)
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj" not in result
    assert "<redacted:pem-private-key>" in result


def test_pem_public_key_untouched():
    # PUBLIC KEY blocks should NOT be redacted — no credential value
    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKC\n"
        "-----END PUBLIC KEY-----"
    )
    assert redact_credentials(pem) == pem


def test_redact_bearer_token():
    # Bearer token that is NOT a JWT (opaque OAuth2 token)
    text = "Authorization: Bearer ya29.a0AfH6SMCexampleLongOpaqueToken1234567890"
    result = redact_credentials(text)
    assert "ya29.a0AfH6SMCexampleLongOpaqueToken1234567890" not in result
    assert "<redacted:bearer>" in result


def test_bearer_token_short_no_match():
    # Token under 20 chars — should NOT be redacted (placeholder / synthetic value)
    text = "Authorization: Bearer shorttoken"
    assert redact_credentials(text) == text


def test_bearer_benign_prose_untouched():
    # "Bearer" in ordinary prose, not an Authorization header
    text = "The bearer of this document is authorized."
    assert redact_credentials(text) == text


def test_redact_lowered_api_key_bare():
    text = "curl -H 'api_key=supersecretapikey1234'"
    result = redact_credentials(text)
    assert "supersecretapikey1234" not in result
    assert "<redacted:lowered-credential>" in result


def test_redact_lowered_password_bare():
    text = "connect --password=Hunter2!!x --host=db.example.com"
    result = redact_credentials(text)
    assert "Hunter2!!x" not in result
    assert "<redacted:lowered-credential>" in result


def test_redact_lowered_secret_bare():
    text = "export secret=my_super_long_secret_value_xyz"
    result = redact_credentials(text)
    assert "my_super_long_secret_value_xyz" not in result
    assert "<redacted:lowered-credential>" in result


def test_redact_lowered_api_key_json():
    text = '{"api_key": "supersecretapikey1234", "model": "gpt-4"}'
    result = redact_credentials(text)
    assert "supersecretapikey1234" not in result
    assert "<redacted:lowered-credential>" in result
    # Non-credential JSON field untouched
    assert "gpt-4" in result


def test_redact_lowered_password_json():
    text = '{"password": "Hunter2!!xYZ", "username": "alice"}'
    result = redact_credentials(text)
    assert "Hunter2!!xYZ" not in result
    assert "<redacted:lowered-credential>" in result
    assert "alice" in result


def test_lowered_credential_short_value_no_match():
    # Value under 8 chars — should NOT be redacted to avoid false positives
    text = "api_key=short"
    assert redact_credentials(text) == text


def test_lowered_credential_benign_json_field_untouched():
    # JSON fields with common names but short/non-secret values
    text = '{"secret": "no"}'
    assert redact_credentials(text) == text


def test_lowered_credential_spaced_long_value_untouched():
    """#244 (review): the bare pattern requires `=` with NO surrounding whitespace.
    A spaced `key = value` with a LONG (8+ char) value is prose/code, not a leaked
    secret — it must survive. This pins the space boundary the short-value negative
    tests can't (they only lock the length floor).
    """
    # Code snippet: the RHS is a call, not a credential.
    code = "password = get_password()"
    assert redact_credentials(code) == code
    # Prose: a spaced assignment describing behavior, long value.
    prose = "the api_key = whatever the user typed into the form"
    assert redact_credentials(prose) == prose
    secret_prose = "the secret = something the operator configures elsewhere"
    assert redact_credentials(secret_prose) == secret_prose


def test_pem_bounded_span_no_redos_on_unterminated_opener():
    """#243 (review): the PEM span is bounded to 8KB so an unterminated BEGIN
    opener (semi-external GitHub issue bodies) can't force an O(n²) rescan.
    A BEGIN with no matching END and a large body must NOT match, and must
    complete quickly (bounded work).
    """
    import time

    # 50 openers, each with no closer, followed by a large tail. Unbounded lazy
    # matching would rescan to EOF per opener; the {0,8192} bound caps it.
    junk = "-----BEGIN RSA PRIVATE KEY-----\n" + ("A" * 200 + "\n") * 50
    payload = junk * 50  # ~0.5 MB of opener-heavy, closer-free text
    start = time.perf_counter()
    result = redact_credentials(payload)
    elapsed = time.perf_counter() - start
    # No END marker anywhere → nothing to redact for this pattern.
    assert "<redacted:pem-private-key>" not in result
    # Bounded work: comfortably under a second even on slow CI.
    assert elapsed < 2.0, f"PEM matching took {elapsed:.3f}s — bound may be ineffective"


# ---------------------------------------------------------------------------
# techne#1245: shapes that used to pass through, and truncation that split a secret
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, secret",
    [
        ("export DB_PASSWORD=hunter2hunter2x", "hunter2hunter2x"),
        ("db_password=hunter2hunter2x", "hunter2hunter2x"),
        ("TOKEN=abcdefghijklmnopqrstuvwxyz0123", "abcdefghijklmnopqrstuvwxyz0123"),
        ("curl -u admin:s3cretpass http://x", "s3cretpass"),
        ("curl --user admin:s3cretpass http://x", "s3cretpass"),
        ("psql postgres://user:pw12345678@host/db", "pw12345678"),
        ("gh auth login --with-token gho_abcdefghijklmnopqrstuvwxyz0123456789", "gho_abcdef"),
        ("ghu_abcdefghijklmnopqrstuvwxyz0123", "ghu_abcdef"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("Authorization: token 0123456789abcdef", "0123456789abcdef"),
        ("mysql --password hunter2hunter2 db", "hunter2hunter2"),
        ("glpat-abcdefghijklmnopqrstuvwxyz", "glpat-abcdef"),
        ("npm_abcdefghijklmnopqrstuvwxyz0123456789", "npm_abcdef"),
        ("xoxs-1234-abcdef", "xoxs-1234"),
        ("sk-or-v1-abcdef0123456789abcdef0123", "abcdef0123456789"),
        ('{"github_token": "abcdefghijklmnop"}', "abcdefghijklmnop"),
    ],
)
def test_1245_shapes_are_redacted(text, secret):
    out = redact_credentials(text)
    assert secret not in out, out
    assert "<redacted:" in out


@pytest.mark.parametrize(
    "text",
    [
        "git push -u origin main",
        "date -u +%Y-%m-%dT%H:%M:%SZ",
        # Prudence round 1 on PR 1272: real text from this repo and from Docker.
        'unk_token="[UNK]"))',
        "tokenizer.pad_token=tokenizer.eos_token",
        "Client(token=self._token)",
        "docker run -u 1000:1000 img",
        "docker run --user 1000:1000 img",
        "docker exec -u root:root c",
        "docker exec -u root:0 c",
        "skopeo copy docker://nginx:1.25@sha256:" + "a" * 64,
        '"GH_TOKEN": "${{ github.token }}"',
        "docker build --secret id=npmrc,src=/home/u/.npmrc .",
        'sed -e "s#://#://x-access-token:$GH_TOKEN@#" .git/config',
        "max_tokens=4096",
        "tokens_out=12345678",
        "ssh://git@host:22/repo",
        "export GITHUB_TOKEN_FILE=/etc/x",
        "PASSWORD_FILE=/etc/x",
        "password = get_password()",
        "tool --token $GITHUB_TOKEN",
        "token=$GH_TOKEN",
        "see https://example.com/a:b@c",
        "next_page_token=CAESBggDEAEYAQ",
        '{"nextPageToken": "CAESBggDEAEYAQ"}',
        "pip install tiktoken==0.12.0",
        "OLDPWD=/Users/someone/working",
    ],
)
def test_1245_near_misses_are_left_alone(text):
    assert redact_credentials(text) == text


def test_1245_flag_names_stay_readable():
    out = redact_credentials("gh auth login --with-token gho_abcdefghijklmnopqrstuvwxyz0123456789")
    assert out == "gh auth login --with-token <redacted:github-oauth>"
    assert redact_credentials("curl -u admin:s3cretpass x") == "curl -u <redacted:basic-auth> x"


def test_safe_cut_never_splits_a_secret():
    from techne.core.redaction import safe_cut

    key = "sk-ant-api03-" + "A" * 40
    text = "a" * 80 + key + " tail"
    cut = safe_cut(text, 100)
    assert cut == "a" * 80
    assert redact_credentials(cut) == cut  # nothing partial left to slip through
    assert safe_cut("short", 100) == "short"
    assert safe_cut("b" * 200, 100) == "b" * 100  # no secret: plain cut


def test_safe_start_skips_past_a_straddled_secret():
    from techne.core.redaction import safe_start

    key = "ghp_" + "Z" * 36
    text = "x" * 10 + key + "\nrest"
    assert safe_start(text, 20) == 10 + len(key)
    assert safe_start(text, 5) == 5


def test_summarize_does_not_store_a_cut_secret():
    """Edit/Write previews are truncated before the sink redacts; a key cut
    mid-token used to survive both (techne#1245)."""
    from techne.hooks.post_tool_use import summarize

    key = "sk-ant-api03-" + "Q" * 40
    edit = summarize(
        "Edit", {"file_path": "f", "old_string": "a" * 80 + key, "new_string": "b"}, ""
    )
    write = summarize(
        "Write", {"file_path": "f", "content": "y" * 480 + "DB_PASSWORD=hunter2hunter2"}, ""
    )
    for s in (edit, write):
        stored = redact_credentials(s)
        assert "QQQQ" not in stored and "hunter2" not in stored
        assert "sk-ant-api03" not in stored and "DB_PASSW" not in stored


def test_a_long_underscore_run_is_linear():
    """Prudence round 1 on PR 1272: the prefixed-name pattern could start after
    every `_` and rescan the word, 17.5s on 20,000 underscores."""
    import time

    t = time.perf_counter()
    redact_credentials("_" * 20000)
    assert time.perf_counter() - t < 1.0


def test_safe_cut_on_the_at_sign_keeps_no_url_password():
    """Prudence round 1 on PR 1272: a cut exactly on the `@` kept the whole
    password and dropped the `@` that url-userinfo needs, at one offset per URL."""
    s = "git clone https://alice:hunter2pass@github.com/x/y " + "x" * 200
    for n in range(len(s)):
        assert "hunter2pass" not in redact_credentials(safe_cut(s, n)), n


def test_safe_cut_pulls_back_past_an_outer_match():
    s = "https://alice:hunter2pass_ghp_" + "A" * 36 + "@github.com"
    for n in range(len(s)):
        assert "hunter2pass" not in redact_credentials(safe_cut(s, n)), n


@pytest.mark.parametrize(
    "text",
    [
        "curl -u root:S3cr3tPassw0rd https://x",
        "curl --user root:S3cr3tPassw0rd https://x",
        "wget --user=root:S3cr3tPassw0rd https://x",
    ],
)
def test_a_root_password_is_still_redacted(text):
    """Prudence round 2 on PR 1272: `(?!root:)` skipped every root user, so
    `curl -u root:<password>` was stored in the clear."""
    assert "S3cr3tPassw0rd" not in redact_credentials(text)


def test_safe_cut_repeats_until_no_match_straddles():
    """Prudence round 2 on PR 1272: picking the earliest match is not enough on
    its own: when two matches overlap, one pull-back can land inside the other,
    and only repeating clears it."""
    # The `x_token=` match starts inside the password and runs past the `@`, so
    # a cut after the `@` pulls back to its start, still inside the password.
    s = "see https://alice:Hunter2pass.x_token=Zq81mXv0Lp4tR7wK@github.com/x " + "y" * 200
    for n in range(len(s)):
        assert "Hunter2pass" not in redact_credentials(safe_cut(s, n)), n

