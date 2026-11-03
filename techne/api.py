"""Recall and publish for a runtime that embeds techne (issue #1335).

An agent process that uses techne as memory gets the same tiered recall as the
`techne_recall` MCP tool, installed recall sources included, and publishes tagged
knowledge the way session end does. Both run the code those paths run, so their
behaviour can't drift apart.
"""

from __future__ import annotations

from techne._lazylog import get_logger
from techne.config import Config

log = get_logger(__name__)


def recall(topic: str, *, limit: int = 10, config: Config | None = None) -> list[dict]:
    """Rows for `topic` from memory and every installed recall source, in the order
    `techne_recall` returns them. A row from an installed source names it in
    `recall_source` (`liminal_archivist` for the team pool) and is marked `external`;
    a row from this store has `store == "local"`."""
    from techne.mcp_server import _techne_recall

    out = _techne_recall({"topic": topic}, config or Config.load())
    return out.get("search_results", [])[:limit]


def publish(*, config: Config | None = None) -> dict:
    """Enqueue live knowledge carrying an `archivist_publish_tags` tag, then deliver
    pending outbox rows through the registered sinks, as session end does.

    Returns `{"enqueued", "synced", "failed"}` counts, plus `"error"` when a step
    failed. As at session end, a failed enqueue doesn't stop the flush, so rows
    already pending for any sink still go out. Never raises: a broken publish lane
    must not fail the caller's task.
    """
    result: dict = {"enqueued": 0, "synced": 0, "failed": 0}
    errors = []
    try:
        config = config or Config.load()
    except Exception as e:  # noqa: BLE001 — see docstring
        log.warning("publish: config failed to load", exc_info=True)
        return {**result, "error": f"{type(e).__name__}: {e}"}
    try:
        from techne.core.archivist import publish_tagged_knowledge

        result["enqueued"] = publish_tagged_knowledge(config)
    except Exception as e:  # noqa: BLE001
        log.warning("publish: enqueue failed", exc_info=True)
        errors.append(f"{type(e).__name__}: {e}")
    try:
        from techne.sync.outbox import flush_sinks
        from techne.sync.sinks import ensure_sinks_registered

        ensure_sinks_registered(config)
        summary = flush_sinks(config.db_path, budget_s=config.sync.flush_budget_s or None)
    except Exception as e:  # noqa: BLE001
        log.warning("publish: flush failed", exc_info=True)
        errors.append(f"{type(e).__name__}: {e}")
        summary = {}
    if errors:
        result["error"] = "; ".join(errors)
    result["synced"] = sum(s for s, _ in summary.values())
    result["failed"] = sum(f for _, f in summary.values())
    log.debug("publish: %s", result)
    return result
