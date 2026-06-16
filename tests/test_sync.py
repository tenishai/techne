"""Tests for techne sync layer."""

from dataclasses import dataclass

import pytest

from techne.core.database import init_db
from techne.sync.adapter import OutboxEntry, PushResult, SyncQuery, load_adapter
from techne.sync.filesystem import FilesystemAdapter
from techne.sync.noop import NoopAdapter
from techne.sync.outbox import dead_count, enqueue, flush, list_dead, pending_count, stats
from techne.sync.sinks import DeliveryResult, OutboxSink, use_registry


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.close()
    return path


@dataclass
class _EmptySchema:
    """Accepts any keyword arguments — used as a permissive test schema."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    # Allow arbitrary kwargs via __init__ so existing tests can pass any data.
    def __class_getitem__(cls, item):
        return cls


# A minimal dataclass schema that accepts the fields used in TestOutbox calls.
@dataclass
class _OpenPayload:
    content: str = ""
    x: int = 0
    y: int = 0


def _noop_handler(data: dict) -> DeliveryResult:
    return DeliveryResult(ok=True)


def _make_sink(name: str) -> OutboxSink:
    return OutboxSink(name=name, payload_schema=_OpenPayload, handler=_noop_handler)


@pytest.fixture
def registered_targets():
    """Register sink stubs for the targets used in TestOutbox, isolated per test."""
    registry = {name: _make_sink(name) for name in ("knowledge", "a", "b")}
    with use_registry(registry):
        yield


class TestNoopAdapter:
    def test_push_reports_zero_synced(self):
        """noop didn't sync — reporting synced=len(entries) would let flush()
        mark every outbox row 'synced' and silently destroy the backlog."""
        adapter = NoopAdapter()
        entries = [OutboxEntry(id=1, target="test", data={"key": "val"}, content_hash="abc")]
        result = adapter.push(entries)
        assert result.synced == 0
        assert result.failed == 0

    def test_health(self):
        assert NoopAdapter().health() is True


class TestFilesystemAdapter:
    def test_push(self, tmp_path):
        adapter = FilesystemAdapter(path=str(tmp_path / "sync"))
        entries = [
            OutboxEntry(id=1, target="knowledge", data={"content": "test"}, content_hash="abc")
        ]
        result = adapter.push(entries)
        assert result.synced == 1

        files = list((tmp_path / "sync" / "knowledge").glob("*.json"))
        assert len(files) == 1

    def test_health_no_path(self):
        adapter = FilesystemAdapter()
        assert adapter.health() is False

    def test_filesystem_pull_returns_pushed_records(self, tmp_path):
        """pull() returns records that were previously pushed with target='knowledge'."""
        adapter = FilesystemAdapter(path=str(tmp_path / "sync"))
        entries = [
            OutboxEntry(
                id=1,
                target="knowledge",
                data={"content": "fact one", "type": "fact", "tags": "infra", "confidence": "high"},
                content_hash="h1",
            ),
            OutboxEntry(
                id=2,
                target="knowledge",
                data={
                    "content": "fact two",
                    "type": "decision",
                    "tags": "arch",
                    "confidence": "medium",
                },
                content_hash="h2",
            ),
        ]
        push_result = adapter.push(entries)
        assert push_result.synced == 2

        results = adapter.pull(SyncQuery())
        assert len(results) == 2
        contents = {r.content for r in results}
        assert "fact one" in contents
        assert "fact two" in contents

    def test_filesystem_pull_empty_directory(self, tmp_path):
        """pull() against a fresh adapter directory returns an empty list without error."""
        adapter = FilesystemAdapter(path=str(tmp_path / "empty_sync"))
        results = adapter.pull(SyncQuery())
        assert results == []

    def test_filesystem_pull_returns_all_targets(self, tmp_path):
        """pull() returns records from all target subdirectories, not just 'knowledge'."""
        adapter = FilesystemAdapter(path=str(tmp_path / "sync"))
        entries = [
            OutboxEntry(
                id=1,
                target="knowledge",
                data={
                    "content": "knowledge fact",
                    "type": "fact",
                    "tags": "infra",
                    "confidence": "high",
                },
                content_hash="h1",
            ),
            OutboxEntry(
                id=2,
                target="threads",
                data={
                    "content": "thread entry",
                    "type": "thread",
                    "tags": "chat",
                    "confidence": "low",
                },
                content_hash="h2",
            ),
        ]
        push_result = adapter.push(entries)
        assert push_result.synced == 2

        results = adapter.pull(SyncQuery())
        assert len(results) == 2
        contents = {r.content for r in results}
        assert "knowledge fact" in contents
        assert "thread entry" in contents
        targets = {r.target for r in results}
        assert "knowledge" in targets
        assert "threads" in targets

    def test_filesystem_pull_skips_non_directory_entries(self, tmp_path):
        """pull() silently skips stray files directly in base_path, no crash."""
        adapter = FilesystemAdapter(path=str(tmp_path / "sync"))
        entries = [
            OutboxEntry(
                id=1,
                target="knowledge",
                data={"content": "real fact", "type": "fact", "tags": "", "confidence": "high"},
                content_hash="h1",
            ),
        ]
        adapter.push(entries)

        # Create a stray file directly in base_path (not inside a subdir)
        stray = tmp_path / "sync" / "stray.json"
        stray.write_text('{"stray": true}')

        results = adapter.pull(SyncQuery())
        assert len(results) == 1
        assert results[0].content == "real fact"


class TestOutbox:
    def test_enqueue_and_count(self, db_path, registered_targets):
        assert pending_count(db_path) == 0
        # deliver=False: skip immediate delivery so the row stays pending to count
        enqueue(db_path, "knowledge", {"content": "test fact"}, deliver=False)
        assert pending_count(db_path) == 1

    def test_dedup(self, db_path, registered_targets):
        enqueue(db_path, "knowledge", {"content": "same"}, content_hash="dedupkey", deliver=False)
        enqueue(db_path, "knowledge", {"content": "same"}, content_hash="dedupkey", deliver=False)
        assert pending_count(db_path) == 1

    def test_flush_with_noop_preserves_backlog(self, db_path, registered_targets):
        """With noop adapter, flush() must not mark entries synced — they need
        to remain pending so a later real adapter can pick them up."""
        enqueue(db_path, "knowledge", {"content": "test"}, deliver=False)
        adapter = NoopAdapter()
        synced, failed = flush(db_path, adapter)
        assert synced == 0
        assert failed == 0
        assert pending_count(db_path) == 1

    def test_stats(self, db_path, registered_targets):
        enqueue(db_path, "a", {"x": 1}, deliver=False)
        enqueue(db_path, "b", {"y": 2}, deliver=False)
        s = stats(db_path)
        assert s["total"] == 2
        assert s["pending"] == 2
        assert s["synced"] == 0
        assert s["dead"] == 0

    def test_dead_letter_flow(self, db_path, registered_targets):
        """Entries that fail permanently are marked dead with error_message set."""

        class FailingAdapter:
            def health(self):
                return True

            def push(self, entries):
                errors = [f"connection refused for entry {e.id}" for e in entries]
                return PushResult(synced=0, failed=len(entries), errors=errors)

            def pull(self, query):
                return []

        enqueue(db_path, "knowledge", {"content": "item1"}, content_hash="hash1", deliver=False)
        enqueue(db_path, "knowledge", {"content": "item2"}, content_hash="hash2", deliver=False)

        adapter = FailingAdapter()
        synced, failed = flush(db_path, adapter)

        assert synced == 0
        assert failed == 2
        assert dead_count(db_path) == 2
        assert pending_count(db_path) == 0

        dead_entries = list_dead(db_path)
        assert len(dead_entries) == 2
        for e in dead_entries:
            assert e["error_message"] is not None
            assert "connection refused" in e["error_message"]

    def test_enqueue_unknown_target_raises(self, db_path):
        """After tightening, enqueue with no registered sink raises UnknownSinkError."""
        from techne.sync.sinks import UnknownSinkError, use_registry

        with use_registry({}):
            with pytest.raises(UnknownSinkError):
                enqueue(db_path, "any-old-string", {})


class TestAdapterLoader:
    def test_load_noop(self):
        adapter = load_adapter("noop")
        assert isinstance(adapter, NoopAdapter)

    def test_load_noop_drops_unset_kwargs(self):
        # Regression: real callers pass url=..., path=... from config.sync.
        # SyncConfig defaults both to "" (not None), and NoopAdapter takes no
        # kwargs — so the loader must drop None *and* empty-string values.
        assert isinstance(load_adapter("noop", url=None, path=None), NoopAdapter)
        assert isinstance(load_adapter("noop", url="", path=""), NoopAdapter)

    def test_load_filesystem(self, tmp_path):
        adapter = load_adapter("filesystem", path=str(tmp_path))
        assert isinstance(adapter, FilesystemAdapter)

    def test_load_unknown(self):
        with pytest.raises(ValueError):
            load_adapter("nonexistent_adapter")
