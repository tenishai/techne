"""Filesystem sync adapter — share knowledge via JSON files in a directory or git repo."""

import json
from datetime import datetime, timezone
from pathlib import Path

from techne.sync.adapter import KnowledgeEntry, OutboxEntry, PushResult, SyncQuery


class FilesystemAdapter:
    def __init__(self, path: str = "", **kwargs):
        self.base_path = Path(path).expanduser() if path else None

    def push(self, entries: list[OutboxEntry]) -> PushResult:
        if not self.base_path:
            return PushResult(synced=0, failed=len(entries), errors=["No path configured"])

        self.base_path.mkdir(parents=True, exist_ok=True)
        synced = 0
        errors = []

        for entry in entries:
            try:
                filename = f"{entry.target}_{entry.content_hash or entry.id}.json"
                out_path = self.base_path / entry.target
                out_path.mkdir(parents=True, exist_ok=True)

                payload = {
                    "target": entry.target,
                    "data": entry.data,
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }
                (out_path / filename).write_text(json.dumps(payload, indent=2))
                synced += 1
            except Exception as e:
                errors.append(str(e))

        return PushResult(synced=synced, failed=len(entries) - synced, errors=errors or None)

    def pull(self, query: SyncQuery) -> list[KnowledgeEntry]:
        if not self.base_path or not self.base_path.exists():
            return []

        results = []
        for subdir in sorted(self.base_path.iterdir()):
            if not subdir.is_dir():
                continue
            target_name = subdir.name
            for f in sorted(subdir.glob("*.json"), reverse=True):
                try:
                    data = json.loads(f.read_text())
                    entry_data = data.get("data", {})
                    content = entry_data.get("content", "")

                    if query.query and query.query.lower() not in content.lower():
                        continue

                    results.append(
                        KnowledgeEntry(
                            content=content,
                            type=entry_data.get("type", "fact"),
                            tags=entry_data.get("tags", ""),
                            confidence=entry_data.get("confidence", "medium"),
                            source="filesystem",
                            target=target_name,
                        )
                    )

                    if len(results) >= query.limit:
                        return results
                except (json.JSONDecodeError, KeyError):
                    continue

        return results

    def health(self) -> bool:
        if not self.base_path:
            return False
        return self.base_path.exists() or self.base_path.parent.exists()
