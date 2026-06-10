"""Identity registry — structured key-value store for agent identity."""

import re
import sys
from pathlib import Path

import yaml

from techne.core.database import connect, like_escape


def get(db_path: Path, key: str) -> str | None:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM identity WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_value(db_path: Path, key: str, value: str, source: str = "config"):
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO identity (key, value, source, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   source = excluded.source,
                   updated_at = CURRENT_TIMESTAMP""",
            (key, value, source),
        )
        conn.commit()
    finally:
        conn.close()


def list_entries(db_path: Path, prefix: str = "") -> list[dict]:
    conn = connect(db_path)
    try:
        if prefix:
            rows = conn.execute(
                "SELECT key, value, source, updated_at FROM identity WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                (f"{like_escape(prefix)}%",),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, value, source, updated_at FROM identity ORDER BY key"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete(db_path: Path, key: str) -> bool:
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM identity WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def sync_from_markdown(db_path: Path, identity_dir: Path):
    """Import identity entries from markdown grounding files.

    Reads known file types:
    - contacts.md: parsed into identity.contacts.* keys
    - invariants.md: stored as identity.invariants (full text)
    - Any *.md: stored as identity.<filename> (full text)
    - Any *.yaml: parsed as structured key-value pairs
    """
    if not identity_dir.exists():
        return

    for md_file in sorted(identity_dir.glob("*.md")):
        key_base = md_file.stem.replace("-", "_")

        if md_file.name == "contacts.md":
            _import_contacts(db_path, md_file)
        else:
            content = md_file.read_text().strip()
            if content:
                set_value(db_path, f"identity.{key_base}", content, source="file")

    for yaml_file in sorted(identity_dir.glob("*.yaml")):
        _import_yaml(db_path, yaml_file)


# Match: optional leading "- ", then "**key**:" then value
# Tolerates extra whitespace around components.
_CONTACT_FIELD_RE = re.compile(
    r"^\s*(?:-\s+)?\*\*\s*(?P<key>[^*]+?)\s*\*\*\s*:\s*(?P<val>.+?)\s*$"
)


def _import_contacts(db_path: Path, contacts_file: Path):
    """Parse contacts.md and create structured identity entries."""
    content = contacts_file.read_text()
    set_value(db_path, "identity.contacts", content, source="file")

    current_entity = None
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_entity = stripped[3:].strip().lower().replace(" ", "_")
            continue
        if not current_entity:
            continue
        m = _CONTACT_FIELD_RE.match(line)
        if not m:
            continue
        key = m.group("key").strip().lower().replace(" ", "_")
        val = m.group("val").strip().strip("`")
        if key and val:
            set_value(
                db_path,
                f"identity.contacts.{current_entity}.{key}",
                val,
                source="file",
            )


def _import_yaml(db_path: Path, yaml_file: Path):
    """Import a YAML file as structured identity entries."""
    try:
        data = yaml.safe_load(yaml_file.read_text())
    except yaml.YAMLError as e:
        print(
            f"techne: failed to parse {yaml_file} ({type(e).__name__}: {e}); skipping",
            file=sys.stderr,
        )
        return

    if not isinstance(data, dict):
        return

    key_base = yaml_file.stem.replace("-", "_")
    _flatten_dict(db_path, data, prefix=f"identity.{key_base}")


def _flatten_dict(db_path: Path, data: dict, prefix: str):
    for k, v in data.items():
        key = f"{prefix}.{k}"
        if isinstance(v, dict):
            _flatten_dict(db_path, v, key)
        elif isinstance(v, list):
            set_value(db_path, key, ",".join(str(x) for x in v), source="file")
        else:
            set_value(db_path, key, str(v), source="file")
