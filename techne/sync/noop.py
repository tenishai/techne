"""No-op sync adapter — standalone mode, no team sync."""

from techne.sync.adapter import KnowledgeEntry, OutboxEntry, PushResult, SyncQuery


class NoopAdapter:
    def push(self, entries: list[OutboxEntry]) -> PushResult:
        # No-op: we didn't actually sync anything. Returning synced=len(entries)
        # would mark every outbox row 'synced' in the DB, silently destroying the
        # backlog that should still be waiting for a real adapter to take it.
        return PushResult(synced=0, failed=0)

    def pull(self, query: SyncQuery) -> list[KnowledgeEntry]:
        return []

    def health(self) -> bool:
        return True
