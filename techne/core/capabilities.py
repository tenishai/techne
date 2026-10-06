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
from datetime import datetime, timezone
from typing import Any, Callable

from techne.config import CLAUDE_CODE, Config, techne_home

#: extension_state key holding the last version we invited this install to ask
#: about. One-shot per version change — see `invitation`.
INVITE_STATE_KEY = "capabilities:last_invited_version"

#: extension_state key holding the operator's deliberate opt-outs, as
#: {flag: {"at", "version", "reason"}} (techne#1022). See `acknowledge`.
ACK_STATE_KEY = "capabilities:acknowledged"


@dataclass
class Capability:
    """One switchable thing, described so a non-author can act on it."""

    #: Attribute on config.hooks, or for a probe entry just its id. The report
    #: reads it; nothing here writes config.
    flag: str
    title: str
    #: Plain language, present tense, what the USER gets. Not what the code does.
    buys: str
    #: The exact YAML to add, as the bare block. Never surfaced raw — the report
    #: frames it via `_enable_snippet` so it cannot be misread as current state.
    enable: str
    #: Which store counts make this capability's case. See `_evidence_for`.
    evidence_key: str = ""
    #: Set when the state is not a hooks flag but has to be LOOKED at — an
    #: installed package, a cached model, another product's setting (techne#1022).
    #: Names an entry in `_PROBES`. Its `enable` is a command or a settings edit,
    #: not a `hooks:` line, and may carry `{vectors_install}` / `{claude_settings}`.
    probe: str = ""


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
    # --- probe entries (techne#1022) ---------------------------------------
    # After the hook flags, not ranked among them: these are missing PIECES
    # rather than features to weigh, and each says plainly what it costs.
    Capability(
        flag="vectors_backend",
        title="Search by meaning, not only by keyword",
        buys=(
            "Recall matches on meaning as well as exact words, so a question worded "
            "differently from how a fact was stored still finds it. No embedding "
            "backend is installed, so every search is keyword-only. Costs a larger "
            "install and a one-time model download."
        ),
        enable="{vectors_install}\ntechne vectors backfill",
        probe="vectors_backend",
    ),
    Capability(
        flag="vectors_model",
        title="The embedding model, downloaded",
        buys=(
            "The embedding backend is installed but its model is not on disk, so "
            "search is still keyword-only. Session start never downloads it (a cold "
            "download could hang startup), so it stays this way until fetched once."
        ),
        enable="techne vectors backfill   # needs network once; downloads, then embeds",
        probe="vectors_model",
    ),
    Capability(
        flag="single_memory_layer",
        title="techne as the only memory layer",
        buys=(
            "Claude Code's own auto-memory is on beside techne. Both write and both "
            "inject, so the same fact can arrive twice and the two copies drift apart. "
            "Turning auto-memory off leaves techne as the one memory. Existing "
            "MEMORY.md files stay on disk, but nothing loads them into context after."
        ),
        enable=(
            'in {claude_settings} set:\n  "autoMemoryEnabled": false\n'
            "and leave CLAUDE_CODE_DISABLE_AUTO_MEMORY unset (a value of 0 forces it on)"
        ),
        probe="single_memory_layer",
    ),
)


def _direct_url() -> dict | None:
    """techne's PEP 610 `direct_url.json`, or None (the file `--version` reads too)."""
    import json

    from techne import _distribution

    try:
        raw = _distribution().read_text("direct_url.json")
        data = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — an install hint must never fail its caller
        return None
    return data if isinstance(data, dict) else None


def _editable_dir(direct_url: dict | None) -> str | None:
    """The checkout an editable install points at, or None if it is not editable."""
    from urllib.parse import unquote, urlparse

    if not direct_url or not (direct_url.get("dir_info") or {}).get("editable"):
        return None
    url = urlparse(str(direct_url.get("url", "")))
    return unquote(url.path) if url.scheme == "file" and url.path else None


def _installer() -> str | None:
    """The dist-info `INSTALLER` marker ("pip", "uv", ...), or None if unreadable.

    uv-created venvs (`uv venv`) have no pip at all, so an editable install's
    reinstall command needs `uv pip install`, not `-m pip install` (#1128).
    """
    from techne import _distribution

    try:
        raw = _distribution().read_text("INSTALLER")
    except Exception:  # noqa: BLE001 — an install hint must never fail its caller
        return None
    return raw.strip() if raw else None


def _install_context() -> dict:
    """How this techne was installed: the inputs `install_command_for` reads.

    A seam: tests pin it, because the answer is the host's (a developer's venv is
    editable, CI's is a uv venv, a user's is pipx).
    """
    import sys

    return {
        "direct_url": _direct_url(),
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "executable": sys.executable,
        "installer": _installer(),
    }


def vectors_install_command(version: str | None = None) -> str:
    """The one spelling of "install the embedding extra" (also used by `init`)."""
    if version is None:
        from techne import __version__ as version
    return install_command_for(version, **_install_context())


def install_command_for(
    version: str,
    *,
    direct_url: dict | None,
    prefix: str,
    base_prefix: str,
    executable: str,
    installer: str | None = None,
) -> str:
    """The install-the-extra command for one kind of install (#1124).

    The pipx form given to an editable install put a second, older, non-editable
    copy beside the checkout being developed.

    - editable (PEP 610 `dir_info.editable`): reinstall the checkout with the extra.
      A `uv`-created venv (dist-info `INSTALLER` == "uv") has no pip at all, so that
      case gets `uv pip install -e` instead (#1128).
    - pipx venv: the pipx form, as before.
    - uv tool venv: `uv tool install` (those venvs have no pip).
    - any other venv: that venv's pip, with the git URL. Never plain
      `tenish-techne[recommended]`: PyPI `techne` is an unrelated package.
    - no venv: the pipx form, the documented install.
    """
    import re
    import shlex

    python = shlex.quote(executable)
    checkout = _editable_dir(direct_url)
    if checkout:
        cd = f"cd {shlex.quote(checkout)} && "
        if installer == "uv":
            return f'{cd}uv pip install --python {python} -e ".[recommended]"'
        return f'{cd}{python} -m pip install -e ".[recommended]"'

    tag = f"v{version}" if re.fullmatch(r"\d+\.\d+\.\d+", version) else "<tag>"
    from techne import install_spec

    spec = f'"{install_spec("recommended", tag)}"'
    venv = prefix.replace("\\", "/")
    if "/pipx/venvs/" in venv or prefix == base_prefix:
        return f"pipx install --force {spec}"
    if "/uv/tools/" in venv:
        return f"uv tool install --force {spec}"
    return f"{python} -m pip install {spec}"


# Probes answer "is the recommended state in place?": True, False, or None when
# the entry does not apply here or the state cannot be measured. None drops the
# entry from the report and the invitation — "not cached" for a cache we could not
# read is a guess dressed as a fact (see `embeddings.model_cached`).


def _probe_vectors_backend(config: Config) -> bool | None:
    from techne.core.embeddings import backend_available

    return backend_available()


def _probe_vectors_model(config: Config) -> bool | None:
    from techne.core.embeddings import backend_available, model_cached

    if not backend_available():
        return None  # vectors_backend already says so; one gap, one entry
    return model_cached(config)


def _probe_single_memory_layer(config: Config) -> bool | None:
    # Under spool-primary techne works WITH Claude Code's memory on purpose
    # (techne#107), so auto-memory being on there is the chosen setup, not a gap.
    if config.platform != CLAUDE_CODE or config.posture.mode == "spool-primary":
        return None
    from techne.platforms.claude_code.paths import claude_code_has_run

    # Without Claude Code there is no auto-memory beside techne, only the absence
    # of a settings key saying it is off (same false claim #1123 fixed in init).
    if not claude_code_has_run():
        return None
    from techne.platforms.claude_code.hooks import auto_memory_enabled

    return not auto_memory_enabled()


_PROBES: dict[str, Callable[[Config], bool | None]] = {
    "vectors_backend": _probe_vectors_backend,
    "vectors_model": _probe_vectors_model,
    "single_memory_layer": _probe_single_memory_layer,
}


def capability_state(cap: Capability, config: Config) -> bool | None:
    """On, off, or None (not applicable / cannot tell) for one entry."""
    if not cap.probe:
        return bool(getattr(config.hooks, cap.flag, False))
    try:
        return _PROBES[cap.probe](config)
    except Exception:  # noqa: BLE001 — one unreadable probe must not sink the report
        return None


# --- acknowledgements (techne#1022) -------------------------------------------
#
# An operator who leaves something off ON PURPOSE should stop being counted as
# unconfigured. Acknowledgements do not expire (decided for #1022): an opt-out is
# a decision, and re-asking on a later version would be the nag this avoids.


def acknowledgements(conn: sqlite3.Connection) -> dict[str, dict]:
    """The operator's deliberate opt-outs, keyed by flag."""
    from techne.core.extension_state import get_state

    return get_state(conn, ACK_STATE_KEY) or {}


def acknowledge(conn: sqlite3.Connection, config: Config, flag: str, reason: str = "") -> dict:
    """Record that `flag` is off on purpose.

    Raises KeyError for an unknown flag, and ValueError when the flag is ON: "off
    on purpose" would be false today, and a stored ack would quietly exempt the
    flag from the invitation if it were ever switched off later.
    """
    from techne import __version__
    from techne.core.extension_state import set_state

    cap = next((c for c in CAPABILITIES if c.flag == flag), None)
    if cap is None:
        raise KeyError(flag)
    if capability_state(cap, config) is True:
        raise ValueError(f"{flag} is on; nothing to acknowledge")
    acks = acknowledgements(conn)
    acks[flag] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": __version__,
        "reason": reason,
    }
    set_state(conn, ACK_STATE_KEY, acks)
    return acks[flag]


def clear_acknowledgement(conn: sqlite3.Connection, flag: str) -> bool:
    """Forget an opt-out. True if there was one to forget."""
    from techne.core.extension_state import set_state

    acks = acknowledgements(conn)
    if acks.pop(flag, None) is None:
        return False
    set_state(conn, ACK_STATE_KEY, acks)
    return True


def _read_acknowledgements(config: Config) -> dict[str, dict]:
    """Read-only, and {} on any failure: the report reads, it never writes."""
    try:
        conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        return acknowledgements(conn)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


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


def _enable_snippet(cap: Capability, config: Config, on: bool) -> str:
    """The change to make, written so it cannot be read as the current state.

    A bare ``hooks:\n  per_turn_seed: true`` is ambiguous the moment it leaves
    this report: it is valid YAML describing a config, so a reader who sees
    ``true`` concludes the flag IS true. That misread is not hypothetical — an
    agent narrating this report quoted the snippet for an OFF flag and the
    operator, reading it as status, believed the exact opposite of what the
    report said. The `on` bool was right there in the same row and lost anyway,
    because a config-shaped block outranks a sibling field in the reading.

    So the snippet carries its own frame: the state it changes FROM, and the
    file it goes in. And an ON capability gets nothing — an "enable" line for
    something already enabled is the ambiguity with no upside.
    """
    if on:
        return ""
    if cap.probe:
        from techne.platforms.claude_code.paths import claude_config_dir

        body = cap.enable.format(
            vectors_install=vectors_install_command(),
            claude_settings=claude_config_dir() / "settings.json",
        )
        return f"# {cap.flag} is currently NOT IN PLACE — to set it up:\n{body}"
    path = config.loaded_from or (techne_home() / "config.yaml")
    return f"# {cap.flag} is currently OFF — to turn it on, add to {path}:\n{cap.enable}"


def capability_report(config: Config) -> dict:
    """The whole picture, as data. The agent turns this into a conversation."""
    from techne import __version__

    facts = store_facts(config)
    acks = _read_acknowledgements(config)
    caps: list[dict] = []
    for cap in CAPABILITIES:
        on = capability_state(cap, config)
        if on is None:
            continue  # not applicable here, or not measurable — say nothing
        row = asdict(cap)
        row["on"] = on
        row["enable"] = _enable_snippet(cap, config, on)
        row["evidence"] = "" if on else _evidence_for(cap.evidence_key, facts)
        row["acknowledged"] = acks.get(cap.flag)
        caps.append(row)

    off = [c for c in caps if not c["on"]]
    workflows = workflow_state(config)
    missing = [w for w in workflows if w["state"] in ("absent", "outdated")]

    return {
        "version": __version__,
        "store": asdict(facts),
        "capabilities": caps,
        "off_count": len(off),
        "unacknowledged_off_count": sum(1 for c in off if not c["acknowledged"]),
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
            "exactly, and quote the `enable` text INTACT — it is the change to MAKE, "
            "not the current state, and stripping its comment line has already led a "
            "reader to take the `true` in it as status. A capability that is ON has no "
            "enable text at all. An entry with a `probe` is not a config flag: its enable "
            "text is a command or a settings edit. Offer to apply each OFF item, one at a "
            "time, and apply nothing without the operator's yes. An OFF item with "
            "`acknowledged` set is a deliberate opt-out: do not pitch it again. If the "
            "operator declines an item on purpose, record it with "
            "techne_capabilities(acknowledge=<flag>) so it stops counting. "
            "If workflows_missing is non-empty say so plainly; a config flag "
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

    # A deliberate opt-out (see `acknowledge`) is not news, so it is not counted.
    acks = acknowledgements(conn)
    off = [
        c for c in CAPABILITIES if capability_state(c, config) is False and c.flag not in acks
    ]
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
