"""Tests for techne core modules."""

import pytest

from techne.config import Config
from techne.core.database import connect, init_db
from techne.core.identity import get as identity_get
from techne.core.identity import list_entries as identity_list
from techne.core.identity import set_value
from techne.core.knowledge import content_fingerprint, get, list_entries, store, supersede
from techne.core.state import (
    add_observation,
    db_stats,
    end_session,
    format_resume_context,
    get_active_session,
    get_latest_checkpoint,
    get_observations,
    get_resume_context,
    get_session,
    observation_count,
    platform_breakdown,
    provenance_tag,
    save_checkpoint,
    set_session_summary,
    start_session,
)
from techne.core.threads import (
    Thread,
    _parse_threads,
    _summarize_single,
    detect_and_summarize,
    format_combined_summary,
    link_threads,
    save_threads,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.close()
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


class TestDatabase:
    def test_connect_initializes_lazily(self, tmp_path):
        """connect() (via a core function) initializes schema without explicit init_db."""
        from techne.core.knowledge import get, store

        fresh_db = tmp_path / "fresh.db"
        # No init_db call — lazy init must handle this
        entry_id = store(fresh_db, "test fact", type="fact")
        entry = get(fresh_db, entry_id)
        assert entry is not None
        assert entry["content"] == "test fact"

    def test_connect_is_idempotent(self, tmp_path):
        """connect() can be called multiple times on the same path without errors."""
        from techne.core.database import connect
        fresh_db = tmp_path / "idempotent.db"
        conn = connect(fresh_db)
        conn.close()
        conn = connect(fresh_db)
        row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        conn.close()
        assert row[0] == 1

    def test_init_creates_tables(self, db_path):
        from techne.core.database import connect
        conn = connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        conn.close()
        assert "sessions" in tables
        assert "threads" in tables
        assert "observations" in tables
        assert "knowledge" in tables
        assert "identity" in tables
        assert "outbox" in tables
        assert "checkpoints" in tables
        assert "schema_version" in tables

    def test_schema_version(self, db_path):
        from techne.core.database import SCHEMA_VERSION, connect

        conn = connect(db_path)
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        conn.close()
        assert row[0] == SCHEMA_VERSION

    def test_idempotent_init(self, db_path):
        conn = init_db(db_path)
        conn.close()
        conn = init_db(db_path)
        conn.close()


class TestSessions:
    def test_start_and_get(self, db_path):
        sid = start_session(db_path, project_path="/tmp/test", agent_identity="tester")
        session = get_session(db_path, sid)
        assert session is not None
        assert session["project_path"] == "/tmp/test"
        assert session["agent_identity"] == "tester"
        assert session["ended_at"] is None

    def test_end_session(self, db_path):
        sid = start_session(db_path)
        end_session(db_path, sid, summary="Test complete")
        session = get_session(db_path, sid)
        assert session["ended_at"] is not None
        assert session["summary"] == "Test complete"

    def test_end_session_summary_redacted(self, db_path):
        """#244: end_session must redact credentials in the summary column (techne#244)."""
        sid = start_session(db_path)
        # HuggingFace token in the LLM-generated session summary — uses the hf_
        # pattern added in #243.
        end_session(
            db_path,
            sid,
            summary="Uploaded model; token hf_abcdefghijklmnopqrstuvwx was used",
        )
        session = get_session(db_path, sid)
        assert "hf_abcdefghijklmnopqrstuvwx" not in session["summary"]
        assert "<redacted:hf-token>" in session["summary"]

    def test_set_session_summary_redacted(self, db_path):
        """#244: set_session_summary must redact credentials in the summary column."""
        sid = start_session(db_path)
        # Google API key in the compaction summary — uses the AIzaSy pattern added in #243.
        set_session_summary(
            db_path,
            sid,
            "Compaction: called Maps API with AIzaSyAbcdefghijklmnopqrstuvwxyz1234567",
            source="llm",
            summary_llm_source="compaction",
        )
        session = get_session(db_path, sid)
        assert "AIzaSyAbcdefghijklmnopqrstuvwxyz1234567" not in session["summary_llm"]
        assert "<redacted:google-api-key>" in session["summary_llm"]

    def test_active_session(self, db_path):
        sid = start_session(db_path)
        active = get_active_session(db_path)
        assert active is not None
        assert active["id"] == sid

        end_session(db_path, sid)
        active = get_active_session(db_path)
        assert active is None


class TestObservations:
    def test_add_and_get(self, db_path):
        sid = start_session(db_path)
        add_observation(db_path, sid, "Edit", "Edited: foo.py")
        add_observation(db_path, sid, "Bash", "Ran: ls")

        obs = get_observations(db_path, sid)
        assert len(obs) == 2
        assert obs[0]["tool_name"] == "Edit"
        assert obs[1]["tool_name"] == "Bash"

    def test_observation_count(self, db_path):
        sid = start_session(db_path)
        assert observation_count(db_path, sid) == 0
        add_observation(db_path, sid, "Test", "test")
        assert observation_count(db_path, sid) == 1

    def test_observations_au_fts_trigger(self, tmp_path):
        db = tmp_path / "au_test.db"
        conn = init_db(db)
        conn.close()

        conn = connect(db)
        # Insert a session to satisfy the foreign key on observations.session_id
        conn.execute(
            "INSERT INTO sessions (id, started_at) VALUES ('s1', CURRENT_TIMESTAMP)"
        )
        # Insert an observation with the old content
        conn.execute(
            "INSERT INTO observations (session_id, content) VALUES ('s1', 'old content only')"
        )
        obs_id = conn.execute("SELECT id FROM observations WHERE session_id='s1'").fetchone()[0]
        conn.commit()

        # Update the observation's content
        conn.execute(
            "UPDATE observations SET content='new content only' WHERE id=?",
            (obs_id,),
        )
        conn.commit()

        # FTS should find the NEW content
        rows = conn.execute(
            "SELECT rowid FROM observations_fts WHERE observations_fts MATCH 'new content only'"
        ).fetchall()
        assert len(rows) > 0, "FTS should find the updated (new) content"

        # FTS should NOT find the OLD content
        rows = conn.execute(
            "SELECT rowid FROM observations_fts WHERE observations_fts MATCH 'old content only'"
        ).fetchall()
        assert len(rows) == 0, "FTS should NOT find the stale (old) content after update"

        conn.close()


class TestKnowledge:
    def test_store_and_list(self, db_path):
        store(db_path, "Archivist uses AlloyDB", type="fact", tags="infra,db", confidence="high")
        store(db_path, "Chose Python for aggregator", type="decision", tags="lang")

        entries = list_entries(db_path)
        assert len(entries) == 2

        facts = list_entries(db_path, type="fact")
        assert len(facts) == 1
        assert "AlloyDB" in facts[0]["content"]

    def test_supersede(self, db_path):
        old_id = store(db_path, "Archivist uses ClickHouse", type="fact")
        new_id = supersede(db_path, old_id, "Archivist uses AlloyDB")

        old = get(db_path, old_id)
        assert old["superseded_by"] == new_id

        current = list_entries(db_path, include_superseded=False)
        assert len(current) == 1
        assert current[0]["id"] == new_id

    def test_supersede_preserves_confidence_numeric(self, db_path):
        """techne#131: superseding must carry confidence_numeric, not reset it to
        the 0.6 schema default. A `high` fact stays at 0.9 after supersession."""
        from techne.core.database import connect

        old_id = store(db_path, "high-confidence fact", type="fact", confidence="high")
        new_id = supersede(db_path, old_id, "high-confidence fact, revised")

        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT confidence, confidence_numeric FROM knowledge WHERE id = ?",
                (new_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["confidence"] == "high"
        assert row["confidence_numeric"] == 0.9  # not the 0.6 default

    def test_supersede_second_attempt_raises_and_stays_single_current(self, db_path):
        """techne#390: superseding an already-superseded row fails loudly and never
        creates a second live 'current' row (the concurrent double-supersede race)."""
        old_id = store(db_path, "Archivist uses ClickHouse", type="fact")
        winner = supersede(db_path, old_id, "Archivist uses AlloyDB")

        with pytest.raises(ValueError, match="superseded"):
            supersede(db_path, old_id, "Archivist uses Postgres")  # loses

        # Exactly one live revision descends from old_id — the first winner; the
        # losing attempt left no orphan and did not repoint old_id.
        current = list_entries(db_path, include_superseded=False)
        assert [e["id"] for e in current] == [winner]
        assert get(db_path, old_id)["superseded_by"] == winner

    def test_supersede_sql_guard_fires_when_row_retired_mid_transaction(self, db_path, monkeypatch):
        """techne#390 white-box: exercise the SQL guard's own rowcount==0 → raise
        path — the backstop the Python pre-check can't provide for an _in_conn
        caller not holding BEGIN IMMEDIATE. We retire old_id in the exact window
        between _supersede_in_conn's liveness read and its guarded UPDATE (via a
        hook during the INSERT). The guarded UPDATE then matches 0 rows and raises
        'concurrently superseded' instead of silently creating a second live row.

        (This white-box path is what test_supersede_second_attempt... can't cover:
        there the Python check fires first, so the SQL guard is never reached.)
        """
        import techne.core.knowledge as kn

        old_id = store(db_path, "original fact", type="fact")
        winner_id = store(db_path, "winner revision", type="fact")  # real FK target

        real_redact = kn.redact_credentials
        state = {"fired": False}

        def hook_redact(text):
            # Runs during the outer supersede's INSERT — after its liveness read,
            # before its guarded UPDATE. Simulate a concurrent winner retiring
            # old_id in that window (same conn, so no lock dance).
            if not state["fired"]:
                state["fired"] = True
                conn.execute(
                    "UPDATE knowledge SET superseded_by = ? "
                    "WHERE id = ? AND superseded_by IS NULL",
                    (winner_id, old_id),
                )
            return real_redact(text)

        monkeypatch.setattr(kn, "redact_credentials", hook_redact)

        conn = connect(db_path)
        try:
            with pytest.raises(ValueError, match="concurrently superseded"):
                kn._supersede_in_conn(conn, old_id, "loser revision", "sessionA")
            conn.rollback()
        finally:
            conn.close()

    def test_invalid_type(self, db_path):
        with pytest.raises(ValueError):
            store(db_path, "test", type="invalid")

    def test_store_rejects_team_domain_pattern_type(self, db_path):
        """techne#137: `pattern` is a team-domain type (architect.db); writing it to
        personal techne.db re-leaks team patterns into recall. store() must reject it
        with a pointer to the right store — the boundary is a write-time invariant."""
        with pytest.raises(ValueError, match="architect"):
            store(db_path, "a lifted pattern", type="pattern")
        # And it's no longer an accepted personal-memory type at all.
        from techne.core.knowledge import VALID_TYPES

        assert "pattern" not in VALID_TYPES

    def test_content_fingerprint(self):
        h1 = content_fingerprint("test content")
        h2 = content_fingerprint("test content")
        h3 = content_fingerprint("different content")
        assert h1 == h2
        assert h1 != h3

    def test_tag_filter_escapes_like_wildcards(self, db_path):
        """A tag containing `%` or `_` must match literally, not as a LIKE
        wildcard. Without escaping, the `%` in the tag string would expand
        and return rows whose tags happen to share a prefix or substring."""
        store(db_path, "literal percent tag", type="fact", tags="50%-cpu,monitoring")
        store(db_path, "unrelated", type="fact", tags="monitoring,alerts")
        store(db_path, "underscore tag", type="fact", tags="rate_limit")

        # Without escaping, "50%" would match anything starting with "50".
        # With escaping, it only matches a tag literally containing "50%".
        hits = list_entries(db_path, tags="50%")
        assert len(hits) == 1
        assert "literal percent" in hits[0]["content"]

        # Underscore is also a LIKE wildcard (single-char). Should not
        # accidentally match across other tags.
        hits = list_entries(db_path, tags="rate_limit")
        assert len(hits) == 1
        assert "underscore" in hits[0]["content"]


class TestIdentity:
    def test_set_and_get(self, db_path):
        set_value(db_path, "name", "Rook")
        assert identity_get(db_path, "name") == "Rook"

    def test_upsert(self, db_path):
        set_value(db_path, "role", "SRE AI")
        set_value(db_path, "role", "Infrastructure AI")
        assert identity_get(db_path, "role") == "Infrastructure AI"

    def test_list_with_prefix(self, db_path):
        set_value(db_path, "contacts.alice.slack", "U123")
        set_value(db_path, "contacts.bob.slack", "U456")
        set_value(db_path, "name", "Rook")

        contacts = identity_list(db_path, prefix="contacts.")
        assert len(contacts) == 2


class TestCheckpoints:
    def test_save_and_get(self, db_path):
        sid = start_session(db_path)
        save_checkpoint(
            db_path, sid, "auto", {"files_modified": ["foo.py"], "last_task": "Fix bug"}
        )

        cp = get_latest_checkpoint(db_path, sid)
        assert cp is not None
        assert cp["trigger"] == "auto"
        assert cp["data"]["last_task"] == "Fix bug"

    def test_save_checkpoint_auto_creates_session_row(self, db_path):
        """save_checkpoint must not raise when the session_id doesn't exist yet.

        The pre_compact hook receives Claude Code's own session UUID (which was
        never registered via start_session), so save_checkpoint auto-creates the
        session row rather than failing the FK constraint.
        """
        foreign_session_id = "cc-session-deadbeef-1234-5678-abcd-ef0123456789"
        # Must not raise sqlite3.IntegrityError
        save_checkpoint(db_path, foreign_session_id, "compact", {"last_task": "x"})
        cp = get_latest_checkpoint(db_path, foreign_session_id)
        assert cp is not None
        assert cp["trigger"] == "compact"

    def test_save_checkpoint_multiple_for_unknown_session(self, db_path):
        """Multiple checkpoints for the same auto-created session row work."""
        sid = "cc-session-unknown-99"
        save_checkpoint(db_path, sid, "compact", {"n": 1})
        save_checkpoint(db_path, sid, "compact", {"n": 2})
        cp = get_latest_checkpoint(db_path, sid)
        assert cp["data"]["n"] == 2

    def test_save_checkpoint_recent_commands_redacted(self, db_path):
        """#244: recent_commands in checkpoint data must be redacted at write time (techne#244).

        Bearer token in a curl command is the highest-value sink — uses the bearer
        pattern added in #243.
        """
        sid = start_session(db_path)
        cmd_with_secret = "curl -H 'Authorization: Bearer ya29.longOpaqueBearerToken1234567890x' https://api.example.com"
        save_checkpoint(
            db_path,
            sid,
            "compact",
            {
                "recent_commands": [cmd_with_secret, "ls -la"],
                "last_task": "fetch data from API",
            },
        )
        cp = get_latest_checkpoint(db_path, sid)
        assert cp is not None
        commands = cp["data"]["recent_commands"]
        # The token must not appear in persisted data
        assert "ya29.longOpaqueBearerToken1234567890x" not in commands[0]
        assert "<redacted:bearer>" in commands[0]
        # Non-sensitive command and other keys must be unchanged
        assert commands[1] == "ls -la"
        assert cp["data"]["last_task"] == "fetch data from API"

    def test_save_checkpoint_no_recent_commands_key(self, db_path):
        """save_checkpoint with no recent_commands key works without error (no mutation)."""
        sid = start_session(db_path)
        save_checkpoint(db_path, sid, "auto", {"last_task": "just work", "files_modified": []})
        cp = get_latest_checkpoint(db_path, sid)
        assert cp["data"]["last_task"] == "just work"

    def test_save_checkpoint_last_task_redacted(self, db_path):
        """#244 (review): last_task is the last raw USER MESSAGE verbatim — users
        paste secrets into chat. The serialization-boundary walk must redact it
        (and any future string field), not just recent_commands.
        """
        sid = start_session(db_path)
        # A user message pasting an Anthropic key into chat, verbatim.
        save_checkpoint(
            db_path,
            sid,
            "compact",
            {
                "last_task": "here's my key sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890 configure it",
                "git_branch": "main",
                "files_modified": ["/repo/foo.py"],
            },
        )
        cp = get_latest_checkpoint(db_path, sid)
        assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890" not in cp["data"]["last_task"]
        assert "<redacted:sk-ant>" in cp["data"]["last_task"]
        # Structured / non-credential fields survive untouched.
        assert cp["data"]["git_branch"] == "main"
        assert cp["data"]["files_modified"] == ["/repo/foo.py"]


class TestResumeContext:
    def test_empty_db(self, db_path):
        ctx = get_resume_context(db_path)
        assert ctx["active_threads"] == []
        assert ctx["recent_threads"] == []
        assert ctx["knowledge"] == []

    def test_get_resume_context_with_data(self, db_path):
        """get_resume_context returns threads with resume_points and high-confidence knowledge."""
        # Create a session and threads with resume points
        sid = start_session(db_path, project_path="/proj/test")
        threads = [
            Thread(
                thread_num=1, label="Database migration",
                summary="Ran Alembic migrations", resume_points="- Verify prod deployment",
            ),
            Thread(
                thread_num=2, label="API refactor",
                summary="Refactored user endpoints", resume_points="- Add integration tests",
            ),
        ]
        save_threads(threads, sid, db_path)

        # Store some high-confidence knowledge entries
        store(db_path, "Uses AlloyDB in prod", type="fact", confidence="high")
        store(db_path, "Python is primary language", type="decision", confidence="high")
        # Low confidence entry should NOT appear
        store(db_path, "Maybe use Redis later", type="fact", confidence="low")

        ctx = get_resume_context(db_path)

        # Both threads have resume_points so both should appear as active threads
        assert len(ctx["active_threads"]) == 2
        labels = {t["label"] for t in ctx["active_threads"]}
        assert "Database migration" in labels
        assert "API refactor" in labels

        # Resume points should be populated
        for t in ctx["active_threads"]:
            assert t["resume_points"] is not None
            assert len(t["resume_points"]) > 0

        # Recent threads: both threads were just inserted so they should appear
        assert len(ctx["recent_threads"]) >= 2

        # Knowledge: only high-confidence entries appear
        assert len(ctx["knowledge"]) == 2
        contents = {k["content"] for k in ctx["knowledge"]}
        assert "Uses AlloyDB in prod" in contents
        assert "Python is primary language" in contents
        assert not any("Redis" in k["content"] for k in ctx["knowledge"])


class TestThreadParsing:
    def test_parse_single_thread(self):
        raw = """## Thread 1: Kodama OAuth setup
Observations: 0,1,2,3,4,5
Summary: Set up OAuth for Kodama. Configured endpoints and tested token flow.
Resume: None"""
        threads = _parse_threads(raw)
        assert len(threads) == 1
        assert threads[0].label == "Kodama OAuth setup"
        assert "OAuth" in threads[0].summary
        assert threads[0].resume_points == ""

    def test_parse_multiple_threads(self):
        raw = """## Thread 1: DNS cleanup
Observations: 0,1,2
Summary: Deleted stale DNS A records.
Resume: None

## Thread 2: Morning briefing fix
Observations: 3,4,5,6
Summary: Fixed Slack emoji rendering in morning briefing.
Resume: - Need to deploy to prod"""
        threads = _parse_threads(raw)
        assert len(threads) == 2
        assert threads[0].label == "DNS cleanup"
        assert threads[1].label == "Morning briefing fix"
        assert "deploy" in threads[1].resume_points

    def test_format_combined_single(self):
        threads = [Thread(thread_num=1, label="session", summary="Did stuff")]
        result = format_combined_summary(threads)
        assert "Did stuff" in result

    def test_format_combined_multi(self):
        threads = [
            Thread(thread_num=1, label="DNS", summary="Fixed DNS"),
            Thread(thread_num=2, label="OAuth", summary="Setup OAuth", resume_points="- Test prod"),
        ]
        result = format_combined_summary(threads)
        assert "DNS" in result
        assert "OAuth" in result
        assert "Resume Points" in result


class TestDbStats:
    def test_stats(self, db_path):
        sid = start_session(db_path)
        add_observation(db_path, sid, "Test", "test")
        store(db_path, "fact1", type="fact")

        stats = db_stats(db_path)
        assert stats["sessions"] == 1
        assert stats["observations"] == 1
        assert stats["knowledge"] == 1


class TestLinkThreshold:
    def test_link_threshold_blocks_weak_match(self, db_path, config):
        """With a very strict threshold (0.99), a weakly matching candidate is not linked."""
        # Save a thread in session A with a distinct topic; need a real session row for FK
        session_a = start_session(db_path)
        threads_a = [Thread(
            thread_num=1,
            label="Database schema migration",
            summary="Ran Alembic migrations to add new columns to the users table in PostgreSQL",
        )]
        save_threads(threads_a, session_a, db_path)

        # In session B, try to link a thread on a completely different topic
        config.threads.link_threshold = 0.99
        session_b = start_session(db_path)
        threads_b = [Thread(
            thread_num=1,
            label="Frontend CSS styling",
            summary="Updated Tailwind CSS classes on login page buttons for mobile responsiveness",
        )]

        links = link_threads(threads_b, session_b, db_path, config)

        assert len(links) == 1
        thread_id, parent_id = links[0]
        assert thread_id == f"{session_b}-1"
        assert parent_id is None, f"Expected no parent, got {parent_id}"

    def test_link_threshold_allows_strong_match(self, db_path, config):
        """With a very lenient threshold (0.05), a closely matching candidate IS linked."""
        # Save a thread in session A; need a real session row for FK
        session_a = start_session(db_path)
        threads_a = [Thread(
            thread_num=1,
            label="Kubernetes deployment rollout",
            summary="Deployed container image to Kubernetes cluster via rolling update",
        )]
        save_threads(threads_a, session_a, db_path)

        # In session B, search with nearly identical label/summary to ensure FTS match
        config.threads.link_threshold = 0.05
        session_b = start_session(db_path)
        threads_b = [Thread(
            thread_num=1,
            label="Kubernetes deployment rollout",
            summary="Deployed container image to Kubernetes cluster via rolling update",
        )]

        links = link_threads(threads_b, session_b, db_path, config)

        assert len(links) == 1
        thread_id, parent_id = links[0]
        assert thread_id == f"{session_b}-1"
        assert parent_id == f"{session_a}-1", f"Expected parent {session_a}-1, got {parent_id}"


def test_make_generate_fn_uses_litellm(monkeypatch):
    pytest.importorskip("litellm")
    from techne.config import Config
    from techne.core.threads import _make_generate_fn

    captured = {}

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeChoice:
        def __init__(self, content):
            self.message = FakeMessage(content)

    class FakeResponse:
        def __init__(self, content):
            self.choices = [FakeChoice(content)]

    def fake_completion(**kwargs):
        captured["kwargs"] = kwargs
        return FakeResponse("hello from litellm\n")

    import litellm
    monkeypatch.setattr(litellm, "completion", fake_completion)

    config = Config()
    config.model = "claude-haiku-4-5-20251001"

    generate = _make_generate_fn(config)
    result = generate("test prompt")

    assert result == "hello from litellm"  # .strip() removed the trailing \n
    assert captured["kwargs"]["model"] == "claude-haiku-4-5-20251001"
    assert captured["kwargs"]["max_tokens"] == 4096
    assert captured["kwargs"]["messages"] == [
        {"role": "user", "content": "test prompt"}
    ]


class TestErrorVisibility:
    def test_thread_detection_logs_errors(self):
        """detect_and_summarize logs a warning on LLM failure and still returns threads."""
        from structlog.testing import capture_logs

        config = Config()
        config.threads.min_observations = 5
        # This test asserts LLM-path-specific error logging.
        config.threads.detector = "llm"
        config.threads.summarizer = "llm"

        def failing_generate(prompt: str) -> str:
            raise RuntimeError("simulated LLM failure")

        observations = [
            {"tool_name": "Bash", "content": f"observation {i}", "timestamp": "2026-01-01T00:00:00"}
            for i in range(20)
        ]

        with capture_logs() as cap_logs:
            threads = detect_and_summarize(observations, config, generate_fn=failing_generate)

        # Fallback should still produce at least one Thread
        assert len(threads) >= 1
        assert isinstance(threads[0], Thread)

        # Error should have been logged via structlog
        assert any(e["event"] == "thread_inference_llm_failed" for e in cap_logs)
        assert any("simulated LLM failure" in e.get("error", "") for e in cap_logs)

    def test_summarize_single_logs_errors(self):
        """_summarize_single logs a warning on LLM failure and returns a bare summary."""
        from structlog.testing import capture_logs

        def failing_generate(prompt: str) -> str:
            raise RuntimeError("simulated LLM failure")

        observations = [
            {"tool_name": "Edit", "content": f"edited file {i}", "timestamp": "2026-01-01T00:00:00"}
            for i in range(3)
        ]

        with capture_logs() as cap_logs:
            result = _summarize_single(observations, failing_generate)

        # Fallback bare summary should be a non-empty string
        assert isinstance(result, str)
        assert len(result) > 0

        # Error should have been logged via structlog
        assert any(e["event"] == "thread_single_summary_llm_failed" for e in cap_logs)
        assert any("simulated LLM failure" in e.get("error", "") for e in cap_logs)


class TestDetectorRouting:
    """Tests for the heuristic / LLM routing in detect_and_summarize."""

    def _observations(self, n=8, gap_minutes=120):
        """Two clusters of n observations separated by a gap."""
        from datetime import datetime, timedelta

        base = datetime(2026, 1, 1, 12, 0)
        obs = []
        for i in range(n):
            obs.append({
                "tool_name": "Edit",
                "content": f"edit /proj/a.py line {i}",
                "timestamp": (base + timedelta(minutes=i)).isoformat(),
            })
        for i in range(n):
            obs.append({
                "tool_name": "Bash",
                "content": f"echo step {i}",
                "timestamp": (base + timedelta(minutes=gap_minutes + i)).isoformat(),
            })
        return obs

    def test_default_uses_heuristic_no_llm_call(self):
        """Default config should never invoke generate_fn."""
        from techne.core.threads import detect_and_summarize

        called = []

        def fake_generate(prompt):
            called.append(prompt)
            return ""

        config = Config()  # heuristic + heuristic
        threads = detect_and_summarize(
            self._observations(), config, generate_fn=fake_generate
        )
        assert called == [], "heuristic detector must not call the LLM"
        assert len(threads) >= 1
        # No LLM-style label leakage; heuristic labels are predictable
        for t in threads:
            assert t.summary, "every thread should have a fallback summary"

    def test_heuristic_path_splits_on_gap(self):
        from techne.core.threads import detect_and_summarize

        config = Config()
        threads = detect_and_summarize(self._observations(), config)
        # 2-hour gap + family change → expect 2 threads
        assert len(threads) == 2

    def test_metadata_path_returns_scores(self):
        """detect_summarize_with_metadata yields scores under the heuristic detector."""
        from techne.core.threads import detect_summarize_with_metadata

        config = Config()
        threads, scores = detect_summarize_with_metadata(self._observations(), config)
        assert len(threads) >= 1
        assert len(scores) == len(self._observations()) - 1  # one per pair

    def test_metadata_path_empty_scores_under_llm(self):
        """LLM detector path returns empty scores list."""
        from techne.core.threads import detect_summarize_with_metadata

        config = Config()
        config.threads.detector = "llm"
        config.threads.summarizer = "llm"
        config.threads.min_observations = 5

        def fake_generate(_prompt):
            return (
                "## Thread 1: work\n"
                "Observations: 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15\n"
                "Summary: did stuff\n"
                "Resume: None\n"
            )

        threads, scores = detect_summarize_with_metadata(
            self._observations(), config, generate_fn=fake_generate
        )
        assert scores == []
        assert len(threads) >= 1

    def test_heuristic_threads_have_resume_points(self):
        from techne.core.threads import detect_and_summarize

        config = Config()
        threads = detect_and_summarize(self._observations(), config)
        for t in threads:
            # build_resume_point should populate at least the action line
            assert "Last action:" in t.resume_points

    def test_heuristic_with_llm_summarizer_calls_generate_per_thread(self):
        from techne.core.threads import detect_and_summarize

        config = Config()
        config.threads.summarizer = "llm"
        called = []

        def fake_generate(prompt):
            called.append(prompt)
            return "LLM summary for this thread."

        threads = detect_and_summarize(
            self._observations(), config, generate_fn=fake_generate
        )
        assert len(called) == len(threads), \
            "expected one LLM call per thread when summarizer=llm"
        for t in threads:
            assert t.summary == "LLM summary for this thread."

    def test_heuristic_llm_summarizer_falls_back_when_failing(self):
        from structlog.testing import capture_logs
        from techne.core.threads import detect_and_summarize

        config = Config()
        config.threads.summarizer = "llm"

        def failing_generate(_prompt):
            raise RuntimeError("simulated LLM failure")

        with capture_logs() as cap_logs:
            threads = detect_and_summarize(
                self._observations(), config, generate_fn=failing_generate
            )
        # Threads still produced via heuristic fallback summary
        for t in threads:
            assert t.summary
        # Error should have been logged via structlog
        assert any(e["event"] == "thread_summary_llm_failed" for e in cap_logs)
        assert any("simulated LLM failure" in e.get("error", "") for e in cap_logs)


# ---------------------------------------------------------------------------
# Phase 3 — supersede guard against re-superseding
# ---------------------------------------------------------------------------

class TestSupersedeGuard:
    @pytest.fixture
    def db_path(self, tmp_path):
        from techne.core.database import init_db
        path = tmp_path / "test_guard.db"
        conn = init_db(path)
        conn.close()
        return path

    def test_supersede_twice_raises(self, db_path):
        """Superseding an entry that is already superseded must raise ValueError."""
        old_id = store(db_path, "original fact", type="fact", source="test")
        _b_id = supersede(db_path, old_id, "first revision")
        with pytest.raises(ValueError, match="already superseded"):
            supersede(db_path, old_id, "second revision")

    def test_supersede_chain_is_valid(self, db_path):
        """Chaining supersede calls A→B→C must work (supersede the latest)."""
        a_id = store(db_path, "A", type="fact", source="test")
        b_id = supersede(db_path, a_id, "B")
        c_id = supersede(db_path, b_id, "C")
        assert c_id != b_id
        entries = list_entries(db_path, include_superseded=False)
        contents = {e["content"] for e in entries}
        assert "C" in contents
        assert "B" not in contents
        assert "A" not in contents


# ---------------------------------------------------------------------------
# techne#81 — sessions.platform read path
# ---------------------------------------------------------------------------


class TestPlatformBreakdown:
    """platform_breakdown() returns per-platform session + observation counts."""

    def test_empty_db(self, db_path):
        result = platform_breakdown(db_path)
        assert result == {}

    def test_counts_sessions_by_platform(self, db_path):
        start_session(db_path, platform="claude-code")
        start_session(db_path, platform="claude-code")
        start_session(db_path, platform="opencode")
        result = platform_breakdown(db_path)
        assert result["claude-code"]["sessions"] == 2
        assert result["opencode"]["sessions"] == 1

    def test_counts_observations_by_platform(self, db_path):
        sid_cc = start_session(db_path, platform="claude-code")
        sid_oc = start_session(db_path, platform="opencode")
        add_observation(db_path, sid_cc, "Edit", "cc edit 1")
        add_observation(db_path, sid_cc, "Bash", "cc bash")
        add_observation(db_path, sid_oc, "Edit", "oc edit 1")
        result = platform_breakdown(db_path)
        assert result["claude-code"]["observations"] == 2
        assert result["opencode"]["observations"] == 1

    def test_null_platform_coalesced_to_unknown(self, db_path):
        from techne.core.database import connect as db_connect

        # Insert a session with NULL platform directly (bypassing start_session default).
        conn = db_connect(db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (id, platform) VALUES ('null-plat', NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        result = platform_breakdown(db_path)
        assert "unknown" in result
        assert result["unknown"]["sessions"] >= 1

    def test_empty_string_platform_coalesced_to_unknown(self, db_path):
        from techne.core.database import connect as db_connect

        conn = db_connect(db_path)
        try:
            conn.execute(
                "INSERT INTO sessions (id, platform) VALUES ('empty-plat', '')"
            )
            conn.commit()
        finally:
            conn.close()
        result = platform_breakdown(db_path)
        assert "unknown" in result

    def test_sorted_by_sessions_desc(self, db_path):
        # 3 claude-code, 1 opencode → claude-code first
        for _ in range(3):
            start_session(db_path, platform="claude-code")
        start_session(db_path, platform="opencode")
        result = platform_breakdown(db_path)
        platforms = list(result.keys())
        assert platforms[0] == "claude-code"


class TestResumeContextProvenance:
    """get_resume_context returns platform + agent_identity on thread dicts (#81)."""

    def test_active_threads_include_platform(self, db_path):
        sid = start_session(db_path, project_path="/proj", platform="claude-code")
        threads = [
            Thread(thread_num=1, label="work", summary="did stuff", resume_points="- next step")
        ]
        save_threads(threads, sid, db_path)
        ctx = get_resume_context(db_path, project_path="/proj")
        assert len(ctx["active_threads"]) == 1
        t = ctx["active_threads"][0]
        assert t["platform"] == "claude-code"
        assert "agent_identity" in t

    def test_active_threads_no_project_include_platform(self, db_path):
        sid = start_session(db_path, platform="opencode")
        threads = [
            Thread(thread_num=1, label="oc work", summary="oc stuff", resume_points="- oc next")
        ]
        save_threads(threads, sid, db_path)
        ctx = get_resume_context(db_path)
        assert len(ctx["active_threads"]) == 1
        assert ctx["active_threads"][0]["platform"] == "opencode"

    def test_recent_threads_include_platform(self, db_path):
        sid = start_session(db_path, platform="opencode")
        threads = [Thread(thread_num=1, label="recent", summary="recent work")]
        save_threads(threads, sid, db_path)
        ctx = get_resume_context(db_path)
        assert len(ctx["recent_threads"]) >= 1
        assert ctx["recent_threads"][0]["platform"] == "opencode"

    def test_cross_platform_both_returned(self, db_path):
        """Cross-platform fixture: one cc thread + one opencode thread in the same
        project → both returned (proving no platform filter)."""
        sid_cc = start_session(db_path, project_path="/myproj", platform="claude-code")
        sid_oc = start_session(db_path, project_path="/myproj", platform="opencode")
        save_threads(
            [Thread(thread_num=1, label="cc task", summary="cc", resume_points="- cc resume")],
            sid_cc, db_path,
        )
        save_threads(
            [Thread(thread_num=1, label="oc task", summary="oc", resume_points="- oc resume")],
            sid_oc, db_path,
        )
        ctx = get_resume_context(db_path, project_path="/myproj")
        platforms = {t["platform"] for t in ctx["active_threads"]}
        assert "claude-code" in platforms
        assert "opencode" in platforms


class TestFormatResumeContextProvenance:
    """format_resume_context annotates thread lines with [platform] tags (#81)."""

    def _make_thread(self, label, resume, platform, agent_identity=""):
        return {
            "label": label,
            "resume_points": resume,
            "summary": "",
            "platform": platform,
            "agent_identity": agent_identity,
        }

    def test_platform_tag_appended(self):
        t = self._make_thread("my task", "- do the thing", "claude-code")
        out = format_resume_context({"active_threads": [t], "knowledge": []})
        assert "[claude-code]" in out

    def test_agent_identity_included_when_present(self):
        t = self._make_thread("my task", "- do the thing", "claude-code", agent_identity="rook-v2")
        out = format_resume_context({"active_threads": [t], "knowledge": []})
        assert "[claude-code:rook-v2]" in out

    def test_agent_identity_absent_when_empty(self):
        t = self._make_thread("my task", "- do the thing", "opencode", agent_identity="")
        out = format_resume_context({"active_threads": [t], "knowledge": []})
        assert "[opencode]" in out
        assert "opencode:" not in out

    def test_both_cc_and_opencode_rendered(self):
        """Both a claude-code and an opencode thread are rendered (no platform filtering)."""
        cc = self._make_thread("cc task", "- cc resume", "claude-code")
        oc = self._make_thread("oc task", "- oc resume", "opencode")
        out = format_resume_context({"active_threads": [cc, oc], "knowledge": []})
        assert "[claude-code]" in out
        assert "[opencode]" in out
        assert "cc task" in out
        assert "oc task" in out

    def test_unknown_platform_coalesced(self):
        t = self._make_thread("task", "- step", platform=None)
        out = format_resume_context({"active_threads": [t], "knowledge": []})
        assert "[unknown]" in out

    def test_closing_tag_in_content_neutralised(self):
        """Content containing </techne-memory> must not prematurely close the block (techne#83-8).

        The neutralised output must have exactly one opening and one closing tag,
        so the block stays well-formed even when stored content embeds the literal
        closing tag string.
        """
        t = self._make_thread(
            "injection test",
            "- safe</techne-memory><script>alert(1)</script>",
            "claude-code",
        )
        out = format_resume_context({"active_threads": [t], "knowledge": []})
        # Block must open and close exactly once.
        assert out.count("<techne-memory>") == 1
        assert out.count("</techne-memory>") == 1
        # The literal closing tag from the content is neutralised (not the wrapper).
        assert "</techne-memory>" in out  # wrapper is still there
        # The dangerous raw sequence does not appear verbatim in the injected content.
        assert "safe</techne-memory><script>" not in out


class TestProvenanceTag:
    """provenance_tag() — the shared [platform(:agent)] formatter (#81)."""

    def test_platform_only(self):
        assert provenance_tag({"platform": "opencode"}) == "[opencode]"

    def test_platform_and_agent(self):
        tag = provenance_tag({"platform": "claude-code", "agent_identity": "rook"})
        assert tag == "[claude-code:rook]"

    def test_empty_agent_omitted(self):
        assert provenance_tag({"platform": "opencode", "agent_identity": ""}) == "[opencode]"

    def test_none_platform_unknown(self):
        assert provenance_tag({"platform": None}) == "[unknown]"

    def test_empty_platform_unknown(self):
        assert provenance_tag({"platform": "", "agent_identity": ""}) == "[unknown]"

    def test_missing_keys_unknown(self):
        assert provenance_tag({}) == "[unknown]"
