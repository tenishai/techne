"""Tests for techne sync layer."""

from dataclasses import dataclass

import pytest

from techne.core.database import init_db
from techne.sync.adapter import OutboxEntry, PushResult, SyncQuery, load_adapter
from techne.sync.filesystem import FilesystemAdapter
from techne.sync.noop import NoopAdapter
from techne.sync.outbox import (
    dead_count,
    enqueue,
    flush,
    list_dead,
    pending_count,
    purge_pending,
    stats,
)
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
    registry = {name: _make_sink(name) for name in ("sink:knowledge", "sink:a", "sink:b")}
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
        enqueue(db_path, "sink:knowledge", {"content": "test fact"}, deliver=False)
        assert pending_count(db_path) == 1

    def test_dedup(self, db_path, registered_targets):
        enqueue(
            db_path, "sink:knowledge", {"content": "same"}, content_hash="dedupkey", deliver=False
        )
        enqueue(
            db_path, "sink:knowledge", {"content": "same"}, content_hash="dedupkey", deliver=False
        )
        assert pending_count(db_path) == 1

    def test_flush_with_noop_preserves_backlog(self, db_path, registered_targets):
        """With noop adapter, flush() must not mark entries synced — they need
        to remain pending so a later real adapter can pick them up."""
        enqueue(db_path, "sync:knowledge", {"content": "test"}, deliver=False)
        adapter = NoopAdapter()
        synced, failed = flush(db_path, adapter)
        assert synced == 0
        assert failed == 0
        assert pending_count(db_path) == 1

    def test_stats(self, db_path, registered_targets):
        enqueue(db_path, "sink:a", {"x": 1}, deliver=False)
        enqueue(db_path, "sink:b", {"y": 2}, deliver=False)
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

        enqueue(
            db_path, "sync:knowledge", {"content": "item1"}, content_hash="hash1", deliver=False
        )
        enqueue(
            db_path, "sync:knowledge", {"content": "item2"}, content_hash="hash2", deliver=False
        )

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

    def test_purge_pending_all(self, db_path, registered_targets):
        enqueue(db_path, "sink:a", {"x": 1}, deliver=False)
        enqueue(db_path, "sink:b", {"y": 2}, deliver=False)
        assert pending_count(db_path) == 2
        deleted = purge_pending(db_path)
        assert deleted == 2
        assert pending_count(db_path) == 0

    def test_purge_pending_by_target(self, db_path, registered_targets):
        enqueue(db_path, "sink:a", {"x": 1}, deliver=False)
        enqueue(db_path, "sink:b", {"y": 2}, deliver=False)
        deleted = purge_pending(db_path, target="sink:a")
        assert deleted == 1
        assert pending_count(db_path) == 1
        # the 'b' row is untouched
        assert stats(db_path)["pending"] == 1

    def test_purge_pending_leaves_synced_and_dead(self, db_path, registered_targets):
        # 'knowledge' sink delivers ok=True → synced; purge must not touch it
        enqueue(db_path, "sink:knowledge", {"content": "delivered"})
        # pending row that purge_pending should delete
        enqueue(db_path, "sink:a", {"x": 1}, deliver=False)
        # dead row: enqueue a sync: entry, then flush with a failing adapter
        enqueue(db_path, "sync:doomed", {"x": 99}, deliver=False)

        class _FailAdapter:
            def health(self):
                return True

            def push(self, entries):
                return PushResult(
                    synced=0,
                    failed=len(entries),
                    errors=["forced dead for test"],
                )

            def pull(self, query):
                return []

        flush(db_path, _FailAdapter())

        assert stats(db_path)["synced"] == 1
        assert stats(db_path)["dead"] == 1

        deleted = purge_pending(db_path)
        assert deleted == 1  # only the pending sink:a row
        s = stats(db_path)
        assert s["synced"] == 1
        assert s["dead"] == 1  # dead row untouched
        assert s["pending"] == 0

    def test_enqueue_unnamespaced_target_raises(self, db_path):
        """An un-namespaced target is rejected — every row must declare its lane (#51)."""
        with pytest.raises(ValueError, match="must be namespaced"):
            enqueue(db_path, "thread_summary", {})
        assert stats(db_path)["total"] == 0

    def test_enqueue_sink_target_without_sink_raises(self, db_path):
        """A `sink:` target with no registered sink raises UnknownSinkError."""
        from techne.sync.sinks import UnknownSinkError, use_registry

        with use_registry({}):
            with pytest.raises(UnknownSinkError):
                enqueue(db_path, "sink:nonexistent", {})
        assert stats(db_path)["total"] == 0

    def test_enqueue_sync_target_needs_no_sink(self, db_path):
        """A `sync:` target inserts with no sink registered — it's adapter-bound."""
        from techne.sync.sinks import use_registry

        with use_registry({}):
            enqueue(db_path, "sync:thread_summary", {"anything": 1}, deliver=False)
        assert stats(db_path)["pending"] == 1

    def test_flush_and_flush_sinks_partition_by_lane(self, db_path):
        """The #51 guarantee: with both a real adapter AND a real sink configured,
        flush(adapter) drains ONLY sync: rows and flush_sinks drains ONLY sink:
        rows — neither steals the other's. The target prefix decides ownership,
        so registry state at flush time can't misroute a row."""
        from techne.sync.outbox import flush, flush_sinks
        from techne.sync.sinks import OutboxSink, use_registry

        @dataclass
        class _P:
            content: str = ""

        delivered: list = []
        archivist = OutboxSink(
            name="sink:archivist",
            payload_schema=_P,
            handler=lambda d: (delivered.append(d), DeliveryResult(ok=True))[1],
        )

        class CountingAdapter:
            def __init__(self):
                self.pushed = []

            def health(self):
                return True

            def push(self, entries):
                self.pushed.extend(entries)
                return PushResult(synced=len(entries), failed=0)

            def pull(self, query):
                return []

        with use_registry({"sink:archivist": archivist}):
            enqueue(db_path, "sync:thread_summary", {"content": "for-adapter"}, deliver=False)
            enqueue(db_path, "sink:archivist", {"content": "for-sink"}, deliver=False)

            adapter = CountingAdapter()
            synced, _ = flush(db_path, adapter)  # adapter sees only sync: rows
            summary = flush_sinks(db_path)  # sinks see only sink: rows

        # adapter got exactly the sync row; sink got exactly the sink row
        assert synced == 1
        assert [e.target for e in adapter.pushed] == ["sync:thread_summary"]
        assert summary["sink:archivist"] == (1, 0)
        assert [d["content"] for d in delivered] == ["for-sink"]
        # everything delivered, nothing left, nothing double-handled
        assert stats(db_path)["synced"] == 2


class TestUnlistedAdapterIsRefusedByDefault:
    """techne#389 — default-deny on adapters techne does not ship.

    THE ISSUE ASKED FOR SOMETHING THAT HAS NO TARGET, and the audit is why this
    looks different from its sketch. #389 wanted a flag refusing to POST to
    non-local destinations; measured 2026-08-17, techne contains ZERO
    network-capable imports (no requests/httpx/aiohttp/urllib/http/socket), both
    built-in adapters are local, and the one outbound path (litellm) is already
    governed by EGRESS_KEYS. There is no POST to gate.

    The real hole is `load_adapter` itself: it will import ANY class registered
    under the `techne.sync_adapters` entry-point group and hand it every outbox
    entry — full session and memory content.
    """

    def _fake_ep(self, monkeypatch, loaded):
        """Register one unlisted entry point named 'remote'."""
        from techne.sync import adapter as adapter_mod

        class _EP:
            name = "remote"

            def load(self):
                loaded.append(True)
                return lambda **kw: object()

        monkeypatch.setattr(adapter_mod, "entry_points", lambda group: [_EP()])

    def test_unlisted_adapter_is_refused_and_never_imported(self, monkeypatch):
        """THE REFUSAL MUST PRECEDE `ep.load()`. `load()` imports the module, and
        import-time side effects are code execution — a gate that fires after the
        class is in hand is cosmetic."""
        from techne.sync.adapter import load_adapter

        loaded: list = []
        self._fake_ep(monkeypatch, loaded)

        with pytest.raises(ValueError, match="not one of techne's built-ins"):
            load_adapter("remote")

        assert loaded == [], "the module must NOT be imported before the refusal"

    def test_the_opt_in_actually_opens_it(self, monkeypatch):
        """Otherwise the assertion above would also pass with the loader broken."""
        from techne.sync.adapter import load_adapter

        loaded: list = []
        self._fake_ep(monkeypatch, loaded)

        load_adapter("remote", allow_unlisted=True)

        assert loaded == [True]

    def test_builtins_are_unaffected(self, monkeypatch):
        """Default-deny must not cost the shipped adapters — noop is the default
        for every install, so breaking it would break everyone."""
        from techne.sync.adapter import load_adapter

        loaded: list = []
        self._fake_ep(monkeypatch, loaded)

        assert load_adapter("noop") is not None
        assert load_adapter("filesystem", path="/tmp/x") is not None

    def test_every_call_site_passes_the_flag(self):
        """A GUARD WHOSE CALLERS DO NOT WIRE IT IS INERT, which is techne#596's
        defect exactly — the rail was enabled, registered, and never fired because
        its precondition was never met. `load_adapter` defaults `allow_unlisted`
        to False, so a caller that forgets it fails CLOSED rather than open; this
        pins the opposite risk, that someone "fixes" a refusal by hardcoding True
        at a call site instead of setting the config key.
        """
        import pathlib
        import re

        offenders = []
        for rel in (
            "techne/cli.py",
            "techne/hooks/session_start.py",
            "techne/hooks/session_end.py",
        ):
            src = pathlib.Path(rel).read_text()
            for call in re.findall(r"load_adapter\((?:[^()]|\([^()]*\))*\)", src):
                if "allow_unlisted=config.sync.allow_unlisted_adapter" not in call:
                    offenders.append(f"{rel}: {' '.join(call.split())}")

        assert not offenders, (
            "load_adapter call sites must pass the CONFIG value, so the operator "
            f"layer governs it: {offenders}"
        )


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
