"""No-op sync adapter — standalone mode, no team sync."""

from techne.sync.adapter import KnowledgeEntry, OutboxEntry, PushResult, SyncQuery


class NoopAdapter:
    def push(self, entries: list[OutboxEntry]) -> PushResult:
        return PushResult(synced=len(entries), failed=0)

    def pull(self, query: SyncQuery) -> list[KnowledgeEntry]:
        return []

    def health(self) -> bool:
        return True
