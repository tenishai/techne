"""Unit tests for techne/core/_resident.py — shared JSON extraction and prompt-hash utilities."""

import hashlib
import json

from techne.core._resident import extract_json_array, extract_json_object, prompt_hash

# ---------------------------------------------------------------------------
# extract_json_array
# ---------------------------------------------------------------------------


class TestExtractJsonArray:
    def test_clean_array(self):
        raw = json.dumps([{"a": 1}, {"b": 2}])
        assert extract_json_array(raw) == [{"a": 1}, {"b": 2}]

    def test_json_fenced(self):
        raw = "```json\n" + json.dumps([{"x": 1}]) + "\n```"
        assert extract_json_array(raw) == [{"x": 1}]

    def test_plain_fenced(self):
        raw = "```\n" + json.dumps([{"x": 1}]) + "\n```"
        assert extract_json_array(raw) == [{"x": 1}]

    def test_leading_prose_with_array(self):
        raw = "Here is the result:\n[1, 2, 3]"
        assert extract_json_array(raw) == [1, 2, 3]

    def test_garbage_returns_empty(self):
        assert extract_json_array("not json at all") == []

    def test_object_not_array_returns_empty(self):
        assert extract_json_array('{"key": "value"}') == []

    def test_empty_string_returns_empty(self):
        assert extract_json_array("") == []

    def test_none_returns_empty(self):
        # raw or "" guard — defensively handles None even though the type hint is str
        assert extract_json_array(None) == []  # type: ignore[arg-type]

    def test_invalid_json_inside_brackets_returns_empty(self):
        assert extract_json_array("[not valid json}") == []

    def test_trailing_prose_after_array(self):
        raw = '[{"a": 1}] some trailing text'
        assert extract_json_array(raw) == [{"a": 1}]

    def test_empty_array(self):
        assert extract_json_array("[]") == []


# ---------------------------------------------------------------------------
# extract_json_object
# ---------------------------------------------------------------------------


class TestExtractJsonObject:
    def test_object(self):
        raw = json.dumps({"verdict": "merge", "content": "x"})
        assert extract_json_object(raw) == {"verdict": "merge", "content": "x"}

    def test_json_fenced(self):
        raw = "```json\n" + json.dumps({"verdict": "keep"}) + "\n```"
        assert extract_json_object(raw) == {"verdict": "keep"}

    def test_plain_fenced(self):
        raw = "```\n" + json.dumps({"a": 1}) + "\n```"
        assert extract_json_object(raw) == {"a": 1}

    def test_array_not_object_returns_empty(self):
        assert extract_json_object("[1, 2, 3]") == {}

    def test_garbage_returns_empty(self):
        assert extract_json_object("not json at all") == {}

    def test_empty_string_returns_empty(self):
        assert extract_json_object("") == {}

    def test_invalid_json_inside_braces_returns_empty(self):
        assert extract_json_object("{not valid json}") == {}


# ---------------------------------------------------------------------------
# prompt_hash
# ---------------------------------------------------------------------------


class TestPromptHash:
    def test_known_value(self):
        expected = hashlib.sha256(b"hello").hexdigest()[:16]
        assert prompt_hash("hello") == expected

    def test_stability(self):
        text = "some prompt text"
        assert prompt_hash(text) == prompt_hash(text)

    def test_equals_old_idiom_encode_default(self):
        """prompt_hash(x) must equal hashlib.sha256(x.encode()).hexdigest()[:16]."""
        for s in ["", "hello", "a prompt with UTF-8: café", "x" * 1000]:
            old = hashlib.sha256(s.encode()).hexdigest()[:16]
            assert prompt_hash(s) == old, f"mismatch for {s!r}"

    def test_equals_old_idiom_encode_utf8_explicit(self):
        """Also verifies equality with encode('utf-8') as used in reflect.py."""
        for s in ["test", "hello world", "café résumé"]:
            old_explicit = hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
            assert prompt_hash(s) == old_explicit, f"mismatch for {s!r}"

    def test_returns_16_hex_chars(self):
        h = prompt_hash("anything")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_inputs_differ(self):
        assert prompt_hash("prompt A") != prompt_hash("prompt B")
