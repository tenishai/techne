"""Tests for OutboxSink registry and sink-based outbox delivery."""

from dataclasses import dataclass

import pytest

from techne.core.database import init_db
from techne.sync.outbox import (
    flush_sinks,
    prune_orphans,
    stats,
)
from techne.sync.sinks import (
    DeliveryResult,
    InvalidPayloadError,
    OutboxSink,
    UnknownSinkError,
    get_sink,
    list_sinks,
    register_sink,
    unregister_sink,
    use_registry,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.close()
    return path


@dataclass
class SimplePayload:
    content: str
    tag: str = ""


def _ok_handler(data: dict) -> DeliveryResult:
    return DeliveryResult(ok=True)


def _fail_handler(data: dict) -> DeliveryResult:
    return DeliveryResult(ok=False, error="deliberate failure")


def _raise_handler(data: dict) -> DeliveryResult:
    raise RuntimeError("handler exploded")


_simple_sink = OutboxSink(
    name="sink:simple",
    payload_schema=SimplePayload,
    handler=_ok_handler,
    description="test sink",
)


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_get(self):
        with use_registry({}):
            register_sink(_simple_sink)
            assert get_sink("sink:simple") is _simple_sink

    def test_list_sinks_sorted(self):
        b_sink = OutboxSink(name="sink:beta", payload_schema=SimplePayload, handler=_ok_handler)
        a_sink = OutboxSink(name="sink:alpha", payload_schema=SimplePayload, handler=_ok_handler)
        with use_registry({}):
            register_sink(b_sink)
            register_sink(a_sink)
            names = [s.name for s in list_sinks()]
            assert names == ["sink:alpha", "sink:beta"]

    def test_unregister(self):
        with use_registry({}):
            register_sink(_simple_sink)
            unregister_sink("sink:simple")
            assert get_sink("sink:simple") is None

    def test_get_unknown_returns_none(self):
        with use_registry({}):
            assert get_sink("sink:nonexistent") is None

    def test_idempotent_reregister(self):
        """Re-registering with identical schema/handler is a no-op."""
        with use_registry({}):
            register_sink(_simple_sink)
            register_sink(_simple_sink)  # should not raise
            assert get_sink("sink:simple") is _simple_sink

    def test_conflict_raises(self):
        """Re-registering with a different handler raises ValueError."""
        alt_sink = OutboxSink(
            name="sink:simple", payload_schema=SimplePayload, handler=_fail_handler
        )
        with use_registry({}):
            register_sink(_simple_sink)
            with pytest.raises(ValueError, match="already registered"):
                register_sink(alt_sink)


# ---------------------------------------------------------------------------
# enqueue validation tests
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_and_immediate_delivery(self, db_path):
        """Happy path: enqueue + sync-after-enqueue marks the row synced."""
        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            enqueue(db_path, "sink:simple", {"content": "hello"})

        s = stats(db_path)
        assert s["synced"] == 1
        assert s["pending"] == 0

    def test_enqueue_unknown_target_raises(self, db_path):
        from techne.sync.outbox import enqueue

        with use_registry({}):
            with pytest.raises(UnknownSinkError, match="sink:simple"):
                enqueue(db_path, "sink:simple", {"content": "hi"})
        assert stats(db_path)["total"] == 0

    def test_enqueue_invalid_payload_raises(self, db_path):
        """Missing required field → InvalidPayloadError, no row inserted."""
        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            with pytest.raises(InvalidPayloadError, match="sink:simple"):
                enqueue(db_path, "sink:simple", {"wrong_field": "oops"})
        assert stats(db_path)["total"] == 0

    def test_enqueue_deliver_false_leaves_pending(self, db_path):
        """deliver=False skips immediate delivery; row stays pending."""
        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            enqueue(db_path, "sink:simple", {"content": "queued"}, deliver=False)

        s = stats(db_path)
        assert s["pending"] == 1
        assert s["synced"] == 0

    def test_enqueue_failing_handler_leaves_pending(self, db_path):
        """Handler returns ok=False → row stays pending with error_message."""
        from techne.sync.outbox import enqueue

        fail_sink = OutboxSink(
            name="sink:fail", payload_schema=SimplePayload, handler=_fail_handler
        )
        with use_registry({}):
            register_sink(fail_sink)
            enqueue(db_path, "sink:fail", {"content": "will fail"})

        s = stats(db_path)
        assert s["pending"] == 1
        assert s["synced"] == 0

    def test_enqueue_raising_handler_leaves_pending(self, db_path):
        """Handler raises exception → caught; row stays pending."""
        from techne.sync.outbox import enqueue

        raise_sink = OutboxSink(
            name="sink:raiser", payload_schema=SimplePayload, handler=_raise_handler
        )
        with use_registry({}):
            register_sink(raise_sink)
            enqueue(db_path, "sink:raiser", {"content": "boom"})

        s = stats(db_path)
        assert s["pending"] == 1


# ---------------------------------------------------------------------------
# flush_sinks tests
# ---------------------------------------------------------------------------


class TestFlushSinks:
    def test_flush_delivers_pending(self, db_path):
        """flush_sinks picks up rows queued with deliver=False."""
        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            enqueue(db_path, "sink:simple", {"content": "a"}, deliver=False)
            enqueue(db_path, "sink:simple", {"content": "b"}, deliver=False)
            summary = flush_sinks(db_path)

        assert summary["sink:simple"] == (2, 0)
        assert stats(db_path)["synced"] == 2

    def test_flush_multiplexes_two_sinks(self, db_path):
        """Two sinks, two batches — each row goes to the correct handler."""
        from techne.sync.outbox import enqueue

        deliveries: dict[str, list] = {"sink:alpha": [], "sink:beta": []}

        def alpha_handler(data):
            deliveries["sink:alpha"].append(data)
            return DeliveryResult(ok=True)

        def beta_handler(data):
            deliveries["sink:beta"].append(data)
            return DeliveryResult(ok=True)

        alpha_sink = OutboxSink(
            name="sink:alpha", payload_schema=SimplePayload, handler=alpha_handler
        )
        beta_sink = OutboxSink(name="sink:beta", payload_schema=SimplePayload, handler=beta_handler)

        with use_registry({}):
            register_sink(alpha_sink)
            register_sink(beta_sink)
            enqueue(db_path, "sink:alpha", {"content": "a1"}, deliver=False)
            enqueue(db_path, "sink:alpha", {"content": "a2"}, deliver=False)
            enqueue(db_path, "sink:beta", {"content": "b1"}, deliver=False)
            summary = flush_sinks(db_path)

        assert summary["sink:alpha"] == (2, 0)
        assert summary["sink:beta"] == (1, 0)
        assert len(deliveries["sink:alpha"]) == 2
        assert len(deliveries["sink:beta"]) == 1

    def test_flush_unregistered_target_skips(self, db_path):
        """Rows for an unregistered target are left pending (not errored)."""
        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            enqueue(db_path, "sink:simple", {"content": "queued"}, deliver=False)
            # Now flush with registry that has no sink for "sink:simple"
            with use_registry({}):
                summary = flush_sinks(db_path)

        assert summary.get("sink:simple") == (0, 0)
        assert stats(db_path)["pending"] == 1

    def test_flush_handler_failure_stays_pending(self, db_path):
        """ok=False result keeps the row pending for retry."""
        from techne.sync.outbox import enqueue

        fail_sink = OutboxSink(
            name="sink:fail", payload_schema=SimplePayload, handler=_fail_handler
        )
        with use_registry({}):
            register_sink(fail_sink)
            enqueue(db_path, "sink:fail", {"content": "x"}, deliver=False)
            summary = flush_sinks(db_path)

        assert summary["sink:fail"] == (0, 1)
        assert stats(db_path)["pending"] == 1

    def test_flush_handler_exception_stays_pending(self, db_path):
        """Handler exception is caught; row stays pending."""
        from techne.sync.outbox import enqueue

        raise_sink = OutboxSink(
            name="sink:raiser", payload_schema=SimplePayload, handler=_raise_handler
        )
        with use_registry({}):
            register_sink(raise_sink)
            enqueue(db_path, "sink:raiser", {"content": "boom"}, deliver=False)
            summary = flush_sinks(db_path)

        assert summary["sink:raiser"] == (0, 1)
        assert stats(db_path)["pending"] == 1


# ---------------------------------------------------------------------------
# prune_orphans tests
# ---------------------------------------------------------------------------


class TestPruneOrphans:
    def test_prune_only_unregistered(self, db_path):
        """prune_orphans kills orphan rows but leaves registered-target rows alone."""
        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            enqueue(db_path, "sink:simple", {"content": "keeper"}, deliver=False)

        # Enqueue a row directly — bypass sink validation by patching the registry
        # temporarily to add a "sink:ghost" target, then remove it before prune.
        ghost_payload = SimplePayload  # reuse schema

        ghost_sink = OutboxSink(
            name="sink:ghost", payload_schema=ghost_payload, handler=_ok_handler
        )
        with use_registry({}):
            register_sink(_simple_sink)
            register_sink(ghost_sink)
            enqueue(db_path, "sink:ghost", {"content": "orphan"}, deliver=False)

        # Now prune with only "sink:simple" registered
        with use_registry({_simple_sink.name: _simple_sink}):
            pruned = prune_orphans(db_path)

        assert pruned == 1
        s = stats(db_path)
        assert s["pending"] == 1  # "sink:simple" row untouched
        assert s["dead"] == 1  # "sink:ghost" row pruned

    def test_prune_nothing_to_prune(self, db_path):
        """prune_orphans returns 0 when all pending rows have registered sinks."""
        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            enqueue(db_path, "sink:simple", {"content": "fine"}, deliver=False)
            pruned = prune_orphans(db_path)

        assert pruned == 0
        assert stats(db_path)["pending"] == 1


# ---------------------------------------------------------------------------
# _try_deliver_now logging tests
# ---------------------------------------------------------------------------


class TestTryDeliverNowLogging:
    def test_warning_on_handler_ok_false(self, db_path, caplog):
        """logger.warning fires when the handler returns ok=False."""
        import logging

        from techne.sync.outbox import enqueue

        fail_sink = OutboxSink(
            name="sink:fail", payload_schema=SimplePayload, handler=_fail_handler
        )
        with use_registry({}):
            register_sink(fail_sink)
            with caplog.at_level(logging.WARNING, logger="techne.sync.outbox"):
                enqueue(db_path, "sink:fail", {"content": "will fail"})

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "expected a WARNING log record"
        assert "sink:fail" in warning_records[0].message
        assert "deliberate failure" in warning_records[0].message

    def test_error_on_update_row_status_raises(self, db_path, caplog):
        """logger.exception (ERROR) fires when _update_row_status raises."""
        import logging
        from unittest.mock import patch

        from techne.sync.outbox import enqueue

        with use_registry({}):
            register_sink(_simple_sink)
            with patch(
                "techne.sync.outbox._update_row_status",
                side_effect=OSError("disk full"),
            ):
                with caplog.at_level(logging.ERROR, logger="techne.sync.outbox"):
                    enqueue(db_path, "sink:simple", {"content": "hello"})

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected an ERROR log record"
        assert "sink:simple" in error_records[0].message
