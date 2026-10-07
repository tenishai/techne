"""Agent self-lessons (techne#1195) — what an agent has learned about its own behaviour.

Three knowledge types, each with a ``lesson_meta`` row beside it:

- ``lesson`` with tier ``principle``: a one-line rule, injected at session start AND
  after compact or /clear, inside its own char budget. The one block that survives a
  compaction, because compaction is where a lesson's "why" is lost and the agent
  re-learns it the hard way in the same session.
- ``lesson`` with tier ``entry``: the incident, mechanism and rule, recalled by topic
  like other knowledge. An entry names its family through ``parent_id``. The newest
  few entries are injected under their principle as recognition aids.
- ``grant`` (standing permission) and ``charter`` (trust boundary): operator-written,
  injected with the principles, and never written, refined or retired by the agent.

Lifecycle, as designed with the operator on issue #1195:

- A principle's recurrence count and evidence are its family entries, never a
  counter kept by hand. An operator correction that matches a principle is a
  recurrence: evidence the rule is still needed, never a sign it is healthy.
- An entry that names the session it happened in records whether its principle was
  injected in that session (``loaded_when_failed``). A principle that keeps failing
  while loaded wants enforcement (a hook, guard or test), not another clause.
- Refining a principle supersedes it; the meta row and the family move to the new
  revision (``knowledge._carry_lesson_meta``).
- Nothing here auto-retires. ``review`` reports what is over budget, cold, long or
  failing while loaded, and the operator decides.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from techne._log import get_logger
from techne.core.database import connect
from techne.core.knowledge import CONFIDENCE_NUMERIC, OPERATOR_TYPES
from techne.core.redaction import redact_credentials

log = get_logger(__name__)

TIERS = ("principle", "entry")

#: `context_injections.source` for the principle band, so `loaded_when_failed` can ask
#: "was this principle in front of the agent in that session?" from the existing log.
INJECTION_SOURCE = "lesson_principles"

#: A principle longer than this is flagged by `review`: the band's budget is in
#: characters, so an accreted paragraph costs several principles' worth.
LONG_RULE_CHARS = 200

#: Days without a reaffirm or a new family entry before `review` lists a principle as
#: cold. Cold means "show the operator", never "probably dead": a rule that works and a
#: rule whose situation never came up look the same.
DEFAULT_COLD_DAYS = 30

#: Family entries that failed while their principle was loaded before `review`
#: suggests enforcement instead of another clause.
ENFORCE_AFTER = 2

_LIVE = "k.superseded_by IS NULL AND k.status != 'archived'"


def agent_for(config) -> str | None:
    """The stable agent id lessons are filed under: `instance_name`, or None.

    None means every agent on this store. It is an id, not a display name, so a
    persona rename never orphans a lesson."""
    name = (getattr(config, "instance_name", "") or "").strip()
    return name or None


def _content(rule: str, body: str) -> str:
    rule = rule.strip()
    body = (body or "").strip()
    return f"{rule}\n\n{body}" if body else rule


def _insert_in_conn(
    conn,
    *,
    ktype: str,
    rule: str,
    body: str,
    tier: str | None,
    agent_id: str | None,
    parent_id: int | None,
    enforced_by: str | None,
    evidence_session: str | None,
    tags: str,
    source: str,
    event_at: str | None = None,
) -> int:
    rule = redact_credentials(rule.strip())
    if not rule:
        raise ValueError("a lesson needs a non-empty rule")
    loaded = None
    if parent_id is not None:
        _require_live(conn, parent_id, ktype="lesson", tier="principle")
        if evidence_session:
            loaded = _was_injected(conn, parent_id, evidence_session)
    cur = conn.execute(
        """INSERT INTO knowledge
             (type, content, source, tags, confidence, confidence_numeric,
              event_at, last_confirmed_at)
           VALUES (?, ?, ?, ?, 'high', ?, ?, CURRENT_TIMESTAMP)""",
        (
            ktype,
            redact_credentials(_content(rule, body)),
            source,
            tags,
            CONFIDENCE_NUMERIC["high"],
            event_at,
        ),
    )
    new_id = cur.lastrowid
    conn.execute(
        """INSERT INTO lesson_meta
             (knowledge_id, agent_id, tier, rule, parent_id, enforced_by,
              evidence_session, loaded_when_failed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, agent_id, tier, rule, parent_id, enforced_by, evidence_session, loaded),
    )
    return new_id


def _require_live(conn, knowledge_id: int, *, ktype: str | None = None, tier: str | None = None):
    row = conn.execute(
        """SELECT k.type, k.superseded_by, k.status, m.tier
           FROM knowledge k LEFT JOIN lesson_meta m ON m.knowledge_id = k.id
           WHERE k.id = ?""",
        (knowledge_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"knowledge entry {knowledge_id} not found")
    if row["superseded_by"] is not None:
        raise ValueError(
            f"knowledge entry {knowledge_id} is superseded by {row['superseded_by']}; "
            "use the latest revision"
        )
    if row["status"] == "archived":
        raise ValueError(f"knowledge entry {knowledge_id} is retired (archived)")
    if ktype is not None and row["type"] != ktype:
        raise ValueError(f"knowledge entry {knowledge_id} is a {row['type']}, not a {ktype}")
    if tier is not None and row["tier"] != tier:
        raise ValueError(f"knowledge entry {knowledge_id} is not a {tier}")
    return row


def _revision_ids(conn, head_id: int) -> list[int]:
    """*head_id* and every earlier revision that was superseded into it."""
    rows = conn.execute(
        """WITH RECURSIVE chain(id) AS (
               SELECT ?
               UNION
               SELECT k.id FROM knowledge k JOIN chain c ON k.superseded_by = c.id
           )
           SELECT id FROM chain""",
        (head_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _was_injected(conn, principle_id: int, session_id: str) -> int | None:
    """1 if *principle_id* (any revision of it) was injected in *session_id*, 0 if the
    band ran there without it, None if there is no record of the band in that session.

    None is not 0: a session with no `lesson_principles` row may predate this feature,
    have the band disabled, or have had its log pruned, and none of those says the
    rule was absent."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_injections'"
    ).fetchone():
        return None
    rows = conn.execute(
        "SELECT hit_ids FROM context_injections WHERE source = ? AND session_id = ?",
        (INJECTION_SOURCE, session_id),
    ).fetchall()
    if not rows:
        return None
    wanted = set(_revision_ids(conn, principle_id))
    for r in rows:
        try:
            ids = {int(i) for i in json.loads(r["hit_ids"] or "[]")}
        except (TypeError, ValueError):
            continue
        if ids & wanted:
            return 1
    return 0


def add(
    db_path: Path,
    *,
    rule: str,
    tier: str,
    body: str = "",
    agent_id: str | None = None,
    parent_id: int | None = None,
    evidence_session: str | None = None,
    enforced_by: str | None = None,
    tags: str = "",
    source: str = "agent",
) -> int:
    """Store a lesson (principle or entry). Returns the knowledge id."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
    if parent_id is not None and tier != "entry":
        raise ValueError("only an entry has a parent principle")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        new_id = _insert_in_conn(
            conn,
            ktype="lesson",
            rule=rule,
            body=body,
            tier=tier,
            agent_id=agent_id,
            parent_id=parent_id,
            enforced_by=enforced_by,
            evidence_session=evidence_session,
            tags=tags,
            source=source,
        )
        conn.commit()
        log.debug("lesson %s stored: tier=%s agent=%s parent=%s", new_id, tier, agent_id, parent_id)
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_operator(
    db_path: Path, *, kind: str, rule: str, body: str = "", agent_id: str | None = None
) -> int:
    """Store an operator grant or charter. Only `techne lesson` calls this."""
    if kind not in OPERATOR_TYPES:
        raise ValueError(f"kind must be one of {OPERATOR_TYPES}, got {kind!r}")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        new_id = _insert_in_conn(
            conn,
            ktype=kind,
            rule=rule,
            body=body,
            tier=None,
            agent_id=agent_id,
            parent_id=None,
            enforced_by=None,
            evidence_session=None,
            tags="",
            source="operator",
        )
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def refine(
    db_path: Path,
    knowledge_id: int,
    rule: str,
    *,
    body: str | None = None,
    operator: bool = False,
    source: str = "agent",
) -> int:
    """Supersede a principle (or, with *operator*, a grant or charter) with a new rule.

    The new revision keeps the meta row and the family; *body* defaults to the old
    body. Returns the new knowledge id."""
    from techne.core.knowledge import _supersede_in_conn

    rule = rule.strip()
    if not rule:
        raise ValueError("a refined rule must be non-empty")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _require_live(conn, knowledge_id)
        meta = conn.execute(
            "SELECT tier, rule FROM lesson_meta WHERE knowledge_id = ?", (knowledge_id,)
        ).fetchone()
        if meta is None:
            raise ValueError(f"knowledge entry {knowledge_id} is not a lesson, grant or charter")
        if row["type"] == "lesson" and meta["tier"] != "principle":
            raise ValueError("only a principle is refined; add a new entry instead")
        if body is None:
            old = conn.execute(
                "SELECT content FROM knowledge WHERE id = ?", (knowledge_id,)
            ).fetchone()["content"]
            body = old[len(meta["rule"]) :].strip() if old.startswith(meta["rule"]) else ""
        new_id = _supersede_in_conn(
            conn, knowledge_id, _content(rule, body), source, operator=operator
        )
        conn.execute(
            "UPDATE lesson_meta SET rule = ? WHERE knowledge_id = ?",
            (redact_credentials(rule), new_id),
        )
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def link(db_path: Path, entry_id: int, principle_id: int) -> None:
    """File an entry under a principle's family (e.g. after an import)."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_live(conn, entry_id, ktype="lesson", tier="entry")
        _require_live(conn, principle_id, ktype="lesson", tier="principle")
        session = conn.execute(
            "SELECT evidence_session FROM lesson_meta WHERE knowledge_id = ?", (entry_id,)
        ).fetchone()["evidence_session"]
        loaded = _was_injected(conn, principle_id, session) if session else None
        conn.execute(
            "UPDATE lesson_meta SET parent_id = ?, loaded_when_failed = ? WHERE knowledge_id = ?",
            (principle_id, loaded, entry_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_enforced_by(db_path: Path, knowledge_id: int, enforced_by: str | None) -> None:
    """Record the hook, test or guard that now makes a principle's mistake loud."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE lesson_meta SET enforced_by = ? WHERE knowledge_id = ?",
            ((enforced_by or "").strip() or None, knowledge_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"knowledge entry {knowledge_id} is not a lesson")
        conn.commit()
    finally:
        conn.close()


def retire(db_path: Path, knowledge_id: int) -> None:
    """Archive a principle, grant or charter: it stops being injected and stays
    searchable. Operator-only — the CLI is the one caller."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_live(conn, knowledge_id)
        if not conn.execute(
            "SELECT 1 FROM lesson_meta WHERE knowledge_id = ?", (knowledge_id,)
        ).fetchone():
            raise ValueError(f"knowledge entry {knowledge_id} is not a lesson, grant or charter")
        conn.execute(
            "UPDATE knowledge SET status = 'archived', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (knowledge_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---- reading ----------------------------------------------------------------


def _agent_clause(agent_id: str | None) -> tuple[str, tuple]:
    if agent_id is None:
        return "m.agent_id IS NULL", ()
    return "(m.agent_id IS NULL OR m.agent_id = ?)", (agent_id,)


def list_lessons(
    db_path: Path, *, agent_id: str | None = None, tier: str | None = None, kind: str | None = None
) -> list[dict]:
    """Live lessons, grants and charters for *agent_id*, with family stats."""
    where, params = _agent_clause(agent_id)
    sql = f"""SELECT k.id, k.type, k.content, k.last_confirmed_at, k.created_at, k.event_at,
                     m.agent_id, m.tier, m.rule, m.parent_id, m.enforced_by,
                     m.evidence_session, m.loaded_when_failed
              FROM knowledge k JOIN lesson_meta m ON m.knowledge_id = k.id
              WHERE {_LIVE} AND {where}"""
    if tier:
        sql += " AND m.tier = ?"
        params += (tier,)
    if kind:
        sql += " AND k.type = ?"
        params += (kind,)
    sql += " ORDER BY k.id"
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(sql, params)]
        family = _family_stats(conn)
    finally:
        conn.close()
    for r in rows:
        r.update(
            family.get(r["id"], {"recurrences": 0, "loaded_failures": 0, "last_entry_at": None})
        )
    return rows


def _family_stats(conn) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in conn.execute(
        f"""SELECT m.parent_id AS pid, COUNT(*) AS n,
                   SUM(CASE WHEN m.loaded_when_failed = 1 THEN 1 ELSE 0 END) AS failed,
                   MAX(COALESCE(k.event_at, k.created_at)) AS last_at
            FROM lesson_meta m JOIN knowledge k ON k.id = m.knowledge_id
            WHERE m.parent_id IS NOT NULL AND {_LIVE}
            GROUP BY m.parent_id"""
    ):
        out[r["pid"]] = {
            "recurrences": r["n"],
            "loaded_failures": r["failed"] or 0,
            "last_entry_at": r["last_at"],
        }
    return out


def _clauses(conn, principle_id: int, n: int) -> list[str]:
    if n <= 0:
        return []
    rows = conn.execute(
        f"""SELECT m.rule FROM lesson_meta m JOIN knowledge k ON k.id = m.knowledge_id
            WHERE m.parent_id = ? AND {_LIVE}
            ORDER BY COALESCE(k.event_at, k.created_at) DESC, k.id DESC LIMIT ?""",
        (principle_id, n),
    ).fetchall()
    return [r["rule"] for r in rows]


@dataclass
class PrincipleBlock:
    text: str = ""
    injected_ids: list[int] = field(default_factory=list)
    dropped_ids: list[int] = field(default_factory=list)
    chars: int = 0


_KIND_ORDER = {"charter": 0, "grant": 1, "lesson": 2}
_HEADINGS = {"charter": "Charter", "grant": "Standing grants", "lesson": "Principles"}


def select_principles(
    db_path: Path, *, agent_id: str | None, char_budget: int, clauses: int = 2
) -> PrincipleBlock:
    """Render the principle band: charter, then grants, then principles, each
    principle followed by its newest *clauses* family entries.

    Principles are ranked by recurrences (most-needed first), then newest. Whatever
    does not fit is DROPPED VISIBLY: the block says how many and points at
    `techne lesson review`, because a silently truncated rule set looks complete."""
    if char_budget <= 0:
        return PrincipleBlock()
    rows = [
        r
        for r in list_lessons(db_path, agent_id=agent_id)
        if r["type"] != "lesson" or r["tier"] == "principle"
    ]
    if not rows:
        return PrincipleBlock()
    rows.sort(key=lambda r: (_KIND_ORDER[r["type"]], -r["recurrences"], -r["id"]))

    conn = connect(db_path)
    try:
        rendered: list[tuple[dict, str]] = []
        for r in rows:
            line = f"- {r['rule']} ⟦{r['id']}⟧\n"
            if r["type"] == "lesson":
                for c in _clauses(conn, r["id"], clauses):
                    line += f"  - {c}\n"
            rendered.append((r, line))
    finally:
        conn.close()

    header = "## Your principles (techne lessons — always loaded)\n"
    footer_reserve = 120
    out = PrincipleBlock()
    used = len(header) + footer_reserve
    parts = [header]
    current_kind = None
    for r, line in rendered:
        sub = ""
        if r["type"] != current_kind:
            sub = f"**{_HEADINGS[r['type']]}**\n"
        if used + len(sub) + len(line) > char_budget:
            out.dropped_ids.append(r["id"])
            continue
        if sub:
            parts.append(sub)
            current_kind = r["type"]
        parts.append(line)
        used += len(sub) + len(line)
        out.injected_ids.append(r["id"])
    if out.dropped_ids:
        parts.append(
            f"> {len(out.dropped_ids)} more did not fit the {char_budget}-char principle "
            "budget. Run `techne lesson review`.\n"
        )
    out.text = "".join(parts) if (out.injected_ids or out.dropped_ids) else ""
    out.chars = len(out.text)
    return out


def review(
    db_path: Path,
    *,
    agent_id: str | None,
    char_budget: int,
    clauses: int = 2,
    cold_days: int = DEFAULT_COLD_DAYS,
) -> dict:
    """What the operator should look at. Read-only: nothing here retires anything."""
    from datetime import datetime, timedelta, timezone

    from techne.core.knowledge import _parse_timestamp

    block = select_principles(db_path, agent_id=agent_id, char_budget=char_budget, clauses=clauses)
    lessons = list_lessons(db_path, agent_id=agent_id)
    principles = [r for r in lessons if r["type"] == "lesson" and r["tier"] == "principle"]
    entries = [r for r in lessons if r["type"] == "lesson" and r["tier"] == "entry"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=cold_days)

    def _utc(value):
        # Stored stamps mix naive SQLite UTC and aware ISO forms; compare as aware UTC.
        dt = _parse_timestamp(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def last_activity(r):
        stamps = [s for s in (r["last_confirmed_at"], r["last_entry_at"]) if s]
        return max(_utc(s) for s in stamps) if stamps else None

    cold = []
    for r in principles:
        seen = last_activity(r)
        if not r["enforced_by"] and (seen is None or seen < cutoff):
            cold.append({"id": r["id"], "rule": r["rule"], "last_activity": str(seen)})
    return {
        "agent_id": agent_id,
        "budget": {
            "char_budget": char_budget,
            "chars_used": block.chars,
            "injected": len(block.injected_ids),
            "dropped": block.dropped_ids,
        },
        "counts": {
            "principles": len(principles),
            "entries": len(entries),
            "grants": sum(r["type"] == "grant" for r in lessons),
            "charter": sum(r["type"] == "charter" for r in lessons),
        },
        "enforce_candidates": [
            {"id": r["id"], "rule": r["rule"], "loaded_failures": r["loaded_failures"]}
            for r in principles
            if r["loaded_failures"] >= ENFORCE_AFTER and not r["enforced_by"]
        ],
        "enforced": [
            {"id": r["id"], "rule": r["rule"], "enforced_by": r["enforced_by"]}
            for r in principles
            if r["enforced_by"]
        ],
        "cold": cold,
        "long": [
            {"id": r["id"], "chars": len(r["rule"]), "rule": r["rule"]}
            for r in principles
            if len(r["rule"]) > LONG_RULE_CHARS
        ],
        "unlinked_entries": [
            {"id": r["id"], "rule": r["rule"]} for r in entries if r["parent_id"] is None
        ],
    }


# ---- import -----------------------------------------------------------------

_DATE_HEADING = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:[—–:-]+\s*)?(.*)$")


def _bullets(lines: list[str]) -> list[str]:
    """Top-level `- ` / `* ` bullets, each joined with its indented continuation."""
    items: list[str] = []
    for line in lines:
        if re.match(r"^[-*]\s+", line):
            items.append(re.sub(r"^[-*]\s+", "", line).strip())
        elif items and line.startswith((" ", "\t")) and line.strip():
            items[-1] += " " + line.strip()
    return items


def _plain(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()


def parse_markdown(text: str) -> dict:
    """Split a personal-observations file into principles, grants, charter and entries.

    Sections are `## ` headings. A heading naming principles, permissions/grants or
    trust/charter gives one row per top-level bullet. A heading that starts with an
    ISO date gives one entry (rule = the heading's title, body = the section). Any
    other section is skipped and reported, never guessed at."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    out: dict = {"principle": [], "grant": [], "charter": [], "entry": [], "skipped": []}
    for heading, body in sections:
        low = heading.lower()
        date = _DATE_HEADING.match(heading)
        if date:
            title = date.group(2).strip() or heading
            out["entry"].append(
                {"rule": _plain(title), "body": "\n".join(body).strip(), "date": date.group(1)}
            )
        elif "principle" in low:
            out["principle"] += [{"rule": _plain(b)} for b in _bullets(body)]
        elif "permission" in low or "grant" in low:
            out["grant"] += [{"rule": _plain(b)} for b in _bullets(body)]
        elif "trust" in low or "charter" in low:
            items = _bullets(body) or (["\n".join(body).strip()] if "".join(body).strip() else [])
            out["charter"] += [{"rule": _plain(b)} for b in items]
        else:
            out["skipped"].append(heading)
    return out


def import_markdown(
    db_path: Path, text: str, *, agent_id: str | None, dry_run: bool = False
) -> dict:
    """Import a parsed file. Idempotent: a row whose content already exists live with
    the same type and agent is skipped. Entries arrive unlinked — link them to a
    principle with `techne lesson link`; the parser does not guess families."""
    parsed = parse_markdown(text)
    counts = {k: 0 for k in ("principle", "grant", "charter", "entry")}
    counts["duplicate"] = 0
    if dry_run:
        return {
            "parsed": {k: len(v) for k, v in parsed.items() if k != "skipped"},
            "skipped": parsed["skipped"],
            "dry_run": True,
        }
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for key, ktype, tier in (
            ("charter", "charter", None),
            ("grant", "grant", None),
            ("principle", "lesson", "principle"),
            ("entry", "lesson", "entry"),
        ):
            for item in parsed[key]:
                content = _content(item["rule"], item.get("body", ""))
                dup = conn.execute(
                    f"""SELECT 1 FROM knowledge k JOIN lesson_meta m ON m.knowledge_id = k.id
                        WHERE k.type = ? AND k.content = ? AND {_LIVE}
                          AND m.agent_id IS ?""",
                    (ktype, redact_credentials(content), agent_id),
                ).fetchone()
                if dup:
                    counts["duplicate"] += 1
                    continue
                _insert_in_conn(
                    conn,
                    ktype=ktype,
                    rule=item["rule"],
                    body=item.get("body", ""),
                    tier=tier,
                    agent_id=agent_id,
                    parent_id=None,
                    enforced_by=None,
                    evidence_session=None,
                    tags="imported",
                    source="operator" if ktype in OPERATOR_TYPES else "import",
                    event_at=item.get("date"),
                )
                counts[key] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"imported": counts, "skipped": parsed["skipped"], "dry_run": False}
