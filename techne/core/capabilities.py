"""What can techne do on THIS install, and what is switched off? (techne#638)

The problem this solves is not documentation. Shaz and Kyle are two versions
behind; they will cross six migrations, and experience almost nothing new,
because most of what we built is opt-in and defaults OFF. A changelog asks them
to read prose, map it to flag names, and hand-edit config.yaml. Nobody does that.

So the answer techne gives is the one only techne CAN give: a report grounded in
THEIR OWN STORE. "You have 12,400 observations and 380 knowledge entries.
per_turn_recall is off, so none of that reaches you while you work." That is an
argument no document can make, because only techne can see their ledger.

THREE THINGS PER CAPABILITY, and all three are required for the report to be
actionable rather than decorative:
  1. is it on
  2. what it buys, in plain language
  3. the exact line to put in config.yaml

AND WHAT IS NOT INSTALLED AT ALL. A report that reads only config flags will
tell someone their install is perfectly configured while the most valuable
operational tooling — the hygiene pass — is absent from their disk. Config
cannot see that; only a filesystem check can, which is why `skills_status` is
consulted here rather than inferred.

THIS MODULE RENDERS DATA, NOT PROSE. The agent narrates. That is deliberate
(techne#499): the agent-facing surface stays vendor-neutral, works in any MCP
client, and turning structured data into a conversation is what the model is
for. It also keeps the honest-risk in check — see `_evidence_for`, where every
claim is a count from the store rather than a sales line.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from techne.config import Config

#: extension_state key holding the last version we invited this install to ask
#: about. One-shot per version change — see `invitation`.
INVITE_STATE_KEY = "capabilities:last_invited_version"


@dataclass
class Capability:
    """One switchable thing, described so a non-author can act on it."""

    #: Attribute on config.hooks. The report reads it; nothing here writes config.
    flag: str
    title: str
    #: Plain language, present tense, what the USER gets. Not what the code does.
    buys: str
    #: The exact YAML to add. Rendered as a path so the reader can find the block.
    enable: str
    #: Which store counts make this capability's case. See `_evidence_for`.
    evidence_key: str = ""


#: The switchable surface, in the order a person should hear about it — highest
#: felt-difference first. per_turn_recall leads because it is the one that turns
#: a passive archive into something that reaches you unprompted, and it is off.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        flag="per_turn_recall",
        title="Memory that reaches you while you work",
        buys=(
            "Every prompt you send is matched against your durable knowledge, and "
            "the most relevant facts are put in front of the agent automatically. "
            "Without it, everything techne knows sits there until you think to ask."
        ),
        enable="hooks:\n  per_turn_recall: true",
        evidence_key="knowledge",
    ),
    Capability(
        flag="per_turn_seed",
        title="Capturing decisions as they happen",
        buys=(
            "Nudges the agent to write down a durable fact or decision the moment "
            "it emerges, instead of relying on the end-of-session pass to "
            "reconstruct it from the transcript."
        ),
        enable="hooks:\n  per_turn_seed: true",
        evidence_key="knowledge_recent",
    ),
    Capability(
        flag="capture_bash",
        title="Remembering what you ran",
        buys=(
            "Records the commands you run as observations, so 'how did we do this "
            "last time' has an answer. Off by default because it is chatty."
        ),
        enable="hooks:\n  capture_bash: true",
        evidence_key="observations",
    ),
    Capability(
        flag="capture_read",
        title="Remembering what you read",
        buys=(
            "Records which files were read. Broadest coverage of what a session "
            "actually touched, and the highest row count — that trade is the "
            "reason it is off."
        ),
        enable="hooks:\n  capture_read: true",
        evidence_key="observations",
    ),
    Capability(
        flag="codex_grep_assist",
        title="Your searches, answered from the code graph",
        buys=(
            "When the agent greps, techne also looks up matching symbols in the "
            "indexed code graph and hands them over — so a search returns "
            "structure, not just lines."
        ),
        enable="hooks:\n  codex_grep_assist: true",
        evidence_key="codex",
    ),
    Capability(
        flag="codex_edit_rail",
        title="Knowing what an edit might break",
        buys=(
            "After you edit a function, techne surfaces what transitively depends "
            "on it — the 'what breaks if I change this' set — but only when there "
            "is enough of it to be worth saying."
        ),
        enable="hooks:\n  codex_edit_rail: true",
        evidence_key="codex",
    ),
    Capability(
        flag="codex_reindex_on_edit",
        title="A code graph that stays current",
        buys=(
            "Re-indexes just the file you edited, so code-graph answers do not go "
            "stale between sessions. Without it the index is only as fresh as the "
            "last manual `techne codex index`."
        ),
        enable="hooks:\n  codex_reindex_on_edit: true",
        evidence_key="codex",
    ),
    Capability(
        flag="startup_directive",
        title="A first-prompt orientation",
        buys=(
            "On your first prompt of a session, the agent is told how to use "
            "techne's tools — once, then silent."
        ),
        enable="hooks:\n  startup_directive: true",
        evidence_key="",
    ),
)


@dataclass
class StoreFacts:
    """The numbers the narration is grounded in. Every one is a live count."""

    observations: int = 0
    knowledge: int = 0
    knowledge_recent: int = 0
    sessions: int = 0
    threads: int = 0
    identity: int = 0
    codex_repos: int = 0
    codex_symbols: int = 0
    vectors: str = "unknown"
    extras: dict[str, Any] = field(default_factory=dict)


def _count(conn: sqlite3.Connection, sql: str) -> int:
    """Count, treating a missing table as zero rather than an error.

    An older install is exactly the audience for this report, and it may predate
    a table this query names. Failing the whole report because one count is
    unavailable would deny the reader the other twenty facts that ARE knowable —
    so a missing table reads as 0, and the report says what it could not see via
    `extras['unreadable']` rather than pretending.
    """
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return -1


def store_facts(config: Config) -> StoreFacts:
    """Live counts from this install's own store."""
    facts = StoreFacts()
    unreadable: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        facts.extras["error"] = f"store unreadable: {exc}"
        return facts
    try:
        pairs = {
            "observations": "SELECT count(*) FROM observations",
            "knowledge": "SELECT count(*) FROM knowledge WHERE superseded_by IS NULL",
            "knowledge_recent": (
                "SELECT count(*) FROM knowledge WHERE superseded_by IS NULL "
                "AND created_at >= datetime('now', '-30 days')"
            ),
            "sessions": "SELECT count(*) FROM sessions",
            "threads": "SELECT count(*) FROM threads",
            "identity": "SELECT count(*) FROM identity",
        }
        for name, sql in pairs.items():
            value = _count(conn, sql)
            if value < 0:
                unreadable.append(name)
                value = 0
            setattr(facts, name, value)
    finally:
        conn.close()

    facts.codex_repos, facts.codex_symbols = _codex_facts(config)
    facts.vectors = _vector_state(config)
    if unreadable:
        facts.extras["unreadable"] = unreadable
    return facts


def _codex_facts(config: Config) -> tuple[int, int]:
    """How much code is indexed, across every per-repo db (db-per-repo, #209)."""
    try:
        codex_dir = config.data_dir / "codex"
        if not codex_dir.is_dir():
            return (0, 0)
        repos = sorted(codex_dir.glob("*.db"))
        symbols = 0
        for db in repos:
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            except sqlite3.Error:
                continue
            try:
                symbols += max(0, _count(conn, "SELECT count(*) FROM symbols"))
            finally:
                conn.close()
        return (len(repos), symbols)
    except OSError:
        return (0, 0)


def _vector_state(config: Config) -> str:
    """FTS-only or hybrid? A fresh install ships FTS-only and the embedding model
    downloads lazily, so a new user's first impression is keyword search with
    hybrid arriving later, unannounced. Saying so is the whole point."""
    try:
        from techne.core.checks import check_vector_coverage

        result = check_vector_coverage(config)
        verdict = getattr(result.verdict, "value", result.verdict)
        return f"{verdict}: {result.detail}" if result.detail else str(verdict)
    except Exception as exc:  # noqa: BLE001 — a report must not die on one probe
        return f"unknown ({exc})"


def _evidence_for(key: str, facts: StoreFacts) -> str:
    """Ground a capability's case in a count from THIS store, or say nothing.

    Returning "" rather than a generic line is deliberate: an empty store has no
    argument to make, and inventing one ("imagine the possibilities") is exactly
    the clippy failure the issue warns about.
    """
    if key == "knowledge" and facts.knowledge:
        return (
            f"{facts.knowledge} durable entries are in your store and none of them "
            "reach you unprompted right now."
        )
    if key == "knowledge_recent" and facts.knowledge_recent:
        return f"{facts.knowledge_recent} entries were written in the last 30 days."
    if key == "observations" and facts.observations:
        return f"{facts.observations} observations across {facts.sessions} sessions so far."
    if key == "codex":
        if facts.codex_symbols:
            return (
                f"{facts.codex_symbols} symbols indexed across {facts.codex_repos} "
                f"repo(s) — already built, currently unused by this rail."
            )
        return "no repo is indexed yet — run `techne codex index <repo>` first."
    return ""


def workflow_state(config: Config) -> list[dict]:
    """Are the bundled procedures actually on disk? (techne#638)

    Read through `skills_status`, not re-derived, because it already answers the
    filesystem question AND distinguishes a user-edited copy from an outdated
    one — a distinction that matters, since techne must never clobber the former.
    """
    try:
        from techne.platforms.claude_code.skills import skills_status

        return [
            {"name": r.name, "state": r.action, "path": str(r.path), "detail": r.detail}
            for r in skills_status(config)
        ]
    except Exception as exc:  # noqa: BLE001
        return [{"name": "?", "state": "unknown", "path": "", "detail": str(exc)}]


def capability_report(config: Config) -> dict:
    """The whole picture, as data. The agent turns this into a conversation."""
    from techne import __version__

    facts = store_facts(config)
    hooks = config.hooks
    caps: list[dict] = []
    for cap in CAPABILITIES:
        on = bool(getattr(hooks, cap.flag, False))
        row = asdict(cap)
        row["on"] = on
        row["evidence"] = "" if on else _evidence_for(cap.evidence_key, facts)
        caps.append(row)

    off = [c for c in caps if not c["on"]]
    workflows = workflow_state(config)
    missing = [w for w in workflows if w["state"] in ("absent", "outdated")]

    return {
        "version": __version__,
        "store": asdict(facts),
        "capabilities": caps,
        "off_count": len(off),
        "workflows": workflows,
        "workflows_missing": [w["name"] for w in missing],
        "install_hint": (
            "Run `techne init --platform claude-code` to install the missing procedures."
            if missing
            else ""
        ),
        "narration_guidance": (
            "Turn this into a short conversation, not a feature list. Lead with what "
            "is OFF and what it would buy THEM, using the evidence counts — those are "
            "real numbers from their store, so quote them. Name the config lines "
            "exactly. If workflows_missing is non-empty say so plainly; a config flag "
            "cannot reveal a procedure that is absent from disk. Do not oversell: if "
            "a capability's evidence is empty, its case is weak on this install, and "
            "saying less is better than inventing a reason."
        ),
    }


def invitation(config: Config, conn: sqlite3.Connection) -> str | None:
    """One line at SessionStart, ONCE per version change, then silent forever.

    THE DISCIPLINE IS THE POINT, and it is the mitigation for this feature's real
    risk. techne#562 measured that 74.5% of SessionStart's all-time budget went
    to sessions that produced zero observations; a recurring banner is precisely
    how a useful signal gets trained into noise. So this fires on a version
    change and not otherwise — an upgrade is the moment the news is true, and it
    is the only moment it is news.

    Returns None when already invited for this version, or when there is nothing
    to invite about (everything on, everything installed) — because a banner that
    announces "you are fully configured" is pure cost.
    """
    from techne.core.extension_state import get_state, set_state
    from techne import __version__

    state = get_state(conn, INVITE_STATE_KEY) or {}
    if state.get("version") == __version__:
        return None

    off = [c for c in CAPABILITIES if not getattr(config.hooks, c.flag, False)]
    missing = [w for w in workflow_state(config) if w["state"] == "absent"]
    # Record the version REGARDLESS of whether we speak, so an install that is
    # fully configured today is not re-evaluated (and potentially nagged) on
    # every session for the rest of this version's life.
    set_state(conn, INVITE_STATE_KEY, {"version": __version__})
    if not off and not missing:
        return None

    parts = []
    if off:
        parts.append(f"{len(off)} capabilities are off on this install")
    if missing:
        parts.append(f"{len(missing)} bundled workflow(s) are not installed")
    return f"techne {__version__} — {', '.join(parts)}. Ask me what techne can do."
