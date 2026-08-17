"""Operator-vs-agent trust boundary for egress-selecting config (techne#393).

WHY THIS EXISTS
---------------
techne's MEMORY-egress surface is closed by default — ``sync.adapter=noop``, no
``llm_api_base``, no federation connections. (Scoped deliberately: the LLM path
is not closed, see :data:`EGRESS_KEYS`.) But the file that *selects* all of
that, ``config.yaml``, lives under ``techne_home()`` and is writable by the
agent. The coord trust model states the premise plainly (``docs/arch/coord.md``):
"anything that writes techne.db can already do anything." So a prompt-injected
or compromised agent could rewrite its own config to point memory at a network
sync adapter or a foreign LLM endpoint. The door is locked; the key is in the
agent's pocket.

This module supplies the *operator layer*: a second config file holding only the
keys that choose a DESTINATION for memory, which techne honors only when the
file is provably not writable by the process reading it.

WHAT IT DOES AND DOES NOT BUY
-----------------------------
Within a single OS user there is no boundary and this module does not pretend
otherwise: an agent that can rewrite ``config.yaml`` can also rewrite the venv
this code is imported from. What the gate buys *there* is that a silent key-flip
becomes a fail-closed and visible one. The gate becomes a real wall only when
the deployment gives the agent its own uid — the one-install-per-agent target —
at which point ``st_uid != euid`` is enforced by the kernel rather than by
politeness.

SCOPE: DESTINATION, NOT VOLUME
------------------------------
:data:`EGRESS_KEYS` governs *where* memory may go. It deliberately does not
govern how much goes there: ``threads.detector='llm'`` or ``reflect.enabled``
increase the traffic to the endpoint, but they cannot change which endpoint it
is, because the endpoint-selecting keys (``model``, ``reflect.model``,
``conflict.model``, ``llm_api_base``, ``llm_api_key``) are all in the set. That
line is drawn on purpose so the guarantee is statable: an agent under a locked
operator layer can make techne talk more, never to somewhere new.

This module imports nothing from :mod:`techne.config` — the home directory
arrives as an argument — because config.py imports *it*.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Config keys that select a destination for memory, in dotted form. The
#: operator layer is authoritative for this ENTIRE set: any key it does not
#: declare reverts to the IN-CODE DEFAULT, rather than falling back to
#: config.yaml. Partial authority is not authority — an operator layer that
#: pinned only ``sync`` while config.yaml kept ``llm_api_base`` would read as
#: locked while leaving a destination the agent still chooses.
#:
#: THE DEFAULTS ARE NOT ALL "CLOSED", and an earlier revision of this comment
#: said they were. ``model`` defaults to ``claude-haiku-4-5-20251001`` — a live
#: destination — and with ``llm_api_base``/``llm_api_key`` empty, litellm falls
#: back to env-var provider routing (``ANTHROPIC_API_KEY``; see config.py's own
#: note at the ``llm_api_base`` field). So techne under an enforcing verdict
#: still makes outbound LLM calls to the built-in provider. The guarantee is
#: that the agent cannot REPOINT them, not that nothing leaves. Say "built-in
#: default destinations", never "closed defaults" — the second reads as "no
#: traffic" to an operator, and that is false.
#:
#: EXPIRY CONDITION: this set is correct for today's code partly by accident of
#: scope — ``stores`` DSNs are SQLite-only (`core/stores.py:load_store` raises on
#: any other adapter, techne#135) and sync adapters are enumerated. techne#87
#: (postgres stores) would make a store DSN a network destination; whoever lands
#: it must revisit this tuple.
EGRESS_KEYS: tuple[str, ...] = (
    # Whole block: adapter selects a push target; url/path/auth parameterize it.
    "sync",
    # Federation stores. Opened read-only, so this is ingestion rather than
    # exfiltration — included anyway because a chosen input is a chosen trust
    # source, and #393's acceptance names it.
    "connections",
    # LiteLLM endpoint override + its credential.
    "llm_api_base",
    "llm_api_key",
    # Model identifiers ARE destinations: litellm routes "openai/gpt-4" to
    # OpenAI. reflect/conflict each override the base model for their own call
    # (core/reflect.py:652, ConflictConfig.model), so pinning only `model`
    # would leave two unpinned ways to repoint the same traffic.
    "model",
    "reflect.model",
    "conflict.model",
    # standalone=false routes team memory to the archivist sink at this path.
    "archivist_db_path",
    "standalone",
    # Domain-store DSNs (librarian/codex/architect/documents). Structurally
    # identical to archivist_db_path — an arbitrary path that domain memory is
    # written to (`core/stores.py:store_path`) — so the "destination, not
    # volume" line that includes one must include the other. Bounded today by
    # SQLite-only, but a world-readable DSN is already a cross-agent leak in the
    # one-install-per-agent deployment where this gate is meant to be a wall.
    "stores",
    # `sources.claude_code_transcripts.paths` is a list of arbitrary filesystem
    # paths whose CONTENTS are ingested into the observation ledger. ADMITTED
    # 2026-08-17 (techne#585, Richard's call) on the consistency argument: the
    # rationale that admits `connections` above — "opened read-only, so this is
    # ingestion rather than exfiltration — included anyway because a chosen input
    # is a chosen trust source" — applies to it word for word, and keeping the
    # two apart made the gate's own reasoning contradict itself.
    #
    # THIS CHANGED ENFORCEMENT, which is why it needed a decision rather than a
    # test: under a LOCKED operator layer, EGRESS_KEYS entries in config.yaml are
    # DROPPED, so a locked deployment now discards agent-supplied source paths
    # instead of honouring them. That is the intended behaviour of locking — an
    # agent choosing what gets read into its own memory is the thing being
    # prevented — but it is a live behaviour change for any locked install that
    # set `sources` in config.yaml, not a no-op. Pinned by
    # TestKeySurgery::test_strip_removes_sources_now_that_it_is_governed.
    "sources",
)

# The other half of the partition (techne#509). EGRESS_KEYS cannot be DERIVED —
# "selects a destination for memory" is a judgment about meaning, not a property
# of a type, and the field surface proves it: `archivist_db_path` is a
# destination and `db_path` is not; `standalone: bool` is and `staleness_hours:
# int` is not; `sync` is and `search` is not; `stores: dict` is and
# `preserved_egress: dict` is not. Every type class straddles the line, so no
# structural signal exists to infer from.
#
# What CAN be enforced is that the judgment was MADE. Every top-level `Config`
# field must appear in exactly one of these two tuples, and
# tests/test_config_trust.py fails if one appears in neither. A new
# destination-selecting field can then no longer be added in silence — the
# failure mode this list exists to close, and the one that left `stores`
# ungoverned until round 2 of #508's review noticed it.
#
# THIS TUPLE CARRIES NO ENFORCEMENT. It is a record of review, not a mechanism;
# only EGRESS_KEYS is consulted at runtime. Moving a name from here to there is
# the change that has teeth.
_NOT_A_DESTINATION: tuple[str, ...] = (
    # Local volume, not a destination: where THIS install keeps its own state.
    # The line #508 drew is "an arbitrary path memory is written to" — these are
    # the install's own root, not a chosen target.
    "db_path",
    "data_dir",
    "identity_dir",
    # Behaviour tuning. These change what memory is kept, ranked or surfaced;
    # none of them names a place it goes.
    "threads",
    "hooks",
    "heuristic",
    "search",
    "injection",
    "posture",
    "archive",
    "staleness_hours",
    "tag_threshold",
    "knowledge_confidence_half_life_days",
    # Which client's transcripts are read. Not a network target.
    "platform",
    # OUTPUTS of the trust gate, not inputs to it — set by config loading itself
    # (the verdict, the preserved agent values, the file it came from). Governing
    # them would be circular.
    "egress_trust",
    "preserved_egress",
    "loaded_from",
    # DOTTED ENTRIES BELOW EXIST BECAUSE A PARTIALLY-GOVERNED BLOCK MUST BE
    # JUDGED FIELD BY FIELD (techne#588). `reflect.model` and `conflict.model`
    # are in EGRESS_KEYS, and the completeness check used to treat that as
    # classifying the WHOLE `reflect` / `conflict` block by dotted-key prefix —
    # so neither block appeared here, and every OTHER field inside them forced no
    # judgment at all. Measured: adding `ReflectConfig.api_base`, an LLM endpoint
    # override, left the suite byte-identical at 51 passed. That is #509's own
    # failure mode one level down.
    #
    # A block named WHOLE in either tuple still covers everything under it; only
    # a block governed by dotted keys alone has to enumerate. So this list grows
    # only when someone adds a field to `reflect` or `conflict` — which is
    # exactly when a judgment is wanted.
    #
    # All of these change WHAT is kept, ranked, logged or reviewed. None names a
    # place anything goes.
    "conflict.log_raw_response",
    "conflict.max_comparison_candidates",
    "conflict.max_items_per_emit",
    "reflect.enabled",
    "reflect.emit_max_chars",
    "reflect.emit_max_threads",
    "reflect.existing_knowledge_k",
    "reflect.include_existing_knowledge",
    "reflect.log_raw_response",
    "reflect.max_candidates_per_thread",
    "reflect.min_confidence",
    "reflect.min_observations_per_thread",
    # `reflect.write_to` IS THE ONE WORTH ARGUING ABOUT, and it is the field
    # #588's fix was most likely to catch, so the reasoning is recorded rather
    # than assumed. It selects 'candidates' (human review) or 'live' (auto-
    # promote straight into knowledge/identity) — a SINK TOGGLE by the wording of
    # the completeness test's own error message, and `standalone` is in
    # EGRESS_KEYS on exactly that basis.
    #
    # It stays here because BOTH of its destinations are tables in the SAME LOCAL
    # DATABASE. `standalone=false` routes team memory to the archivist sink at
    # another path — bytes land somewhere else. `write_to` never changes where
    # bytes land; it changes whether a human sees them first. That is a review
    # gate, not a destination, and it already has its own control: posture forces
    # 'candidates' under spool_primary (`core/reflect.py:effective_write_to`).
    "reflect.write_to",
)

#: THE ONLY TIER THAT IS A REAL LOCK. Not env-relocatable, so it is the one
#: resolution step an agent cannot step around by editing its own environment.
#: An agent that can write here already has root, at which point this module is
#: not the thing standing between it and the network.
SYSTEM_OPERATOR_CONFIG = Path("/etc/techne/config.operator.yaml")

#: Env override, for deployments whose operator-owned tree is not /etc.
#:
#: A CONVENIENCE, NOT A LOCK, and the distinction is the whole security story.
#: `resolve_operator_config` closes the DANGLING-pointer case (a set-but-wrong
#: value is reported rather than ignored), but it cannot close the UNSET case:
#: an agent running `env -u TECHNE_OPERATOR_CONFIG techne ...` — or relocating
#: `TECHNE_HOME`, which `config.techne_home()` also reads from the environment —
#: falls through to ``absent``, and config.yaml regains full authority. An
#: operator who deploys to /opt and satisfies every instruction in the card will
#: get a correct LOCKED verdict and still have no boundary. Only the hardcoded
#: /etc path survives a hostile environment. (Found by Prudence on PR #508;
#: verified by observation, not argued.)
OPERATOR_CONFIG_ENV = "TECHNE_OPERATOR_CONFIG"

ABSENT = "absent"
LOCKED = "locked"
UNLOCKED = "unlocked"


@dataclass(frozen=True)
class TrustVerdict:
    """Whether an operator layer exists and whether it can be trusted.

    ``state`` is one of:

    * ``absent``   — no operator layer anywhere. config.yaml keeps full authority
      (unchanged behavior), and the install is honestly reported as having an
      agent-writable egress selector.
    * ``locked``   — the layer exists and this process cannot rewrite it. Its
      values are authoritative; config.yaml's egress keys are ignored.
    * ``unlocked`` — the layer exists but this process COULD rewrite it. Fail
      closed: neither file is trusted for egress, so the BUILT-IN DEFAULT
      destinations apply. Note "built-in default", not "closed" — `model`
      defaults to a live Anthropic destination and litellm still routes on env
      credentials. The agent cannot REPOINT that traffic; it does not stop.

    ``reason`` is quotable evidence, not a category — a reader should be able to
    re-derive the verdict from it (which uid owns the file, which parent dir is
    writable) instead of trusting this checker.
    """

    state: str
    path: Path | None
    reason: str
    #: The path whose properties were actually PROVEN — ``path`` with symlinks
    #: resolved. Readers must open THIS, not ``path``: the predicate walks the
    #: resolved ancestor chain, so opening the unresolved path can land on a
    #: different inode when an intermediate component is a symlink we control.
    #: ``path`` stays the configured spelling, because that is what an operator
    #: needs to see in a remedy.
    resolved: Path | None = None

    @property
    def enforcing(self) -> bool:
        """True when this verdict overrides config.yaml's egress keys."""
        return self.state in (LOCKED, UNLOCKED)

    @property
    def read_target(self) -> Path | None:
        """The inode to actually read the layer from."""
        return self.resolved or self.path


def resolve_operator_config(home: Path) -> Path | None:
    """First operator-layer path that exists, in descending authority.

    The system path is checked FIRST and unconditionally. If it were merely a
    fallback, an agent could select the permissive ``absent`` state by pointing
    :data:`OPERATOR_CONFIG_ENV` at a nonexistent file — the env var would become
    an off switch for the whole mechanism.
    """
    if _lexists(SYSTEM_OPERATOR_CONFIG):
        return SYSTEM_OPERATOR_CONFIG
    env = os.environ.get(OPERATOR_CONFIG_ENV)
    if env:
        candidate = Path(env).expanduser()
        if _lexists(candidate):
            return candidate
        # A set-but-dangling override is a misconfiguration, not an absence.
        # Returning it (rather than falling through) makes evaluate_lock report
        # it, so a typo in a deployment surfaces as a named path instead of
        # silently restoring config.yaml's authority.
        return candidate
    candidate = home / "config.operator.yaml"
    return candidate if _lexists(candidate) else None


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except (OSError, ValueError):
        return False
    return True


def _writable_by_us(path: Path) -> bool:
    """Can this process write ``path``, accounting for euid/gid and ACLs."""
    if os.access in os.supports_effective_ids:
        return os.access(path, os.W_OK, effective_ids=True)
    return os.access(path, os.W_OK)


def _writable_parent(path: Path, resolved: Path | None = None) -> Path | None:
    """The nearest ancestor dir this process can write, on EITHER path chain.

    A writable ancestor defeats every property of the file itself: the agent
    renames the directory aside and supplies its own tree. This is the check
    that makes the default ``~/.techne/config.operator.yaml`` location
    unlockable for a same-uid agent — $HOME is writable, so the whole subtree is
    replaceable — and it is why the deployment doc points operators at a tree
    outside the agent's home.

    BOTH CHAINS ARE WALKED, and walking only the resolved one was a real hole
    (Prudence, PR #508). Given ``/agent/link -> /etc/techne`` with ``/agent``
    writable by us, ``lstat`` follows the intermediate symlink and reports the
    genuine root-owned file, and ``resolve()`` yields ``/etc/techne/...`` whose
    ancestors are all root-owned — LOCKED, while the directory that actually
    decides where a later ``open()`` lands was never inspected. Reproduced
    before fixing.
    """
    chains = [path.parents]
    if resolved is not None and resolved != path:
        chains.append(resolved.parents)
    for chain in chains:
        for parent in chain:
            if _writable_by_us(parent):
                return parent
    return None


def evaluate_lock(path: Path | None) -> TrustVerdict:
    """Decide whether ``path`` is an operator lock this process must obey.

    Every negative branch names the specific property that failed, because the
    remedy differs per branch (chown vs chmod vs relocate) and a bare "not
    locked" would send an operator hunting.
    """
    if path is None:
        return TrustVerdict(ABSENT, None, "no operator config found")

    # Resolved ONCE, here, and carried on the verdict so the caller reads the
    # inode whose properties were proven rather than re-walking the name.
    try:
        resolved: Path | None = path.resolve()
    except OSError:
        resolved = None

    try:
        st = os.lstat(path)
    except OSError as exc:
        return TrustVerdict(UNLOCKED, path, f"cannot stat: {type(exc).__name__}: {exc}", resolved)

    import stat as _stat

    if _stat.S_ISLNK(st.st_mode):
        return TrustVerdict(
            UNLOCKED, path, "is a symlink — whoever owns the link chooses the target", resolved
        )
    if not _stat.S_ISREG(st.st_mode):
        return TrustVerdict(UNLOCKED, path, "not a regular file", resolved)

    if not hasattr(os, "geteuid"):
        return TrustVerdict(
            UNLOCKED,
            path,
            "ownership checks unavailable on this platform (no os.geteuid)",
            resolved,
        )
    euid = os.geteuid()

    if euid == 0:
        return TrustVerdict(
            UNLOCKED,
            path,
            "this process runs as root (euid 0) — no file is unwritable to it",
            resolved,
        )
    if st.st_uid == euid:
        return TrustVerdict(
            UNLOCKED,
            path,
            f"owned by this process's own euid {euid} — an owner can always chmod it back",
            resolved,
        )
    if st.st_mode & 0o022:
        return TrustVerdict(
            UNLOCKED,
            path,
            f"mode {oct(st.st_mode & 0o777)} is group- or world-writable",
            resolved,
        )
    if _writable_by_us(path):
        return TrustVerdict(
            UNLOCKED, path, "writable by this process (group membership or ACL)", resolved
        )
    bad_parent = _writable_parent(path, resolved)
    if bad_parent is not None:
        return TrustVerdict(
            UNLOCKED,
            path,
            f"parent dir {bad_parent} is writable by this process — "
            "the file can be replaced by renaming its directory aside",
            resolved,
        )

    return TrustVerdict(
        LOCKED,
        path,
        f"owned by uid {st.st_uid} (not our euid {euid}), mode {oct(st.st_mode & 0o777)}, "
        "no writable ancestor",
        # THE ONE THAT MATTERS MOST. Every other branch refuses the layer, so a
        # missing `resolved` there costs nothing; this branch is the only one
        # whose file is actually READ, and omitting it here would send the reader
        # back to the unresolved name — reinstating the exact symlink hole the
        # ancestor walk above was just extended to close. Caught by this module's
        # own test, not by review.
        resolved,
    )


# ---------------------------------------------------------------------------
# Dotted-key surgery on the raw YAML mapping
# ---------------------------------------------------------------------------


def _pop(raw: dict, dotted: str) -> bool:
    """Remove ``dotted`` from ``raw`` in place. True when something was removed."""
    head, _, rest = dotted.partition(".")
    if not rest:
        return raw.pop(head, _MISSING) is not _MISSING
    child = raw.get(head)
    if not isinstance(child, dict):
        return False
    return _pop(child, rest)


def _get(raw: dict, dotted: str) -> Any:
    head, _, rest = dotted.partition(".")
    if head not in raw:
        return _MISSING
    if not rest:
        return raw[head]
    child = raw[head]
    return _get(child, rest) if isinstance(child, dict) else _MISSING


def _set(raw: dict, dotted: str, value: Any) -> None:
    head, _, rest = dotted.partition(".")
    if not rest:
        raw[head] = value
        return
    child = raw.get(head)
    if not isinstance(child, dict):
        child = {}
        raw[head] = child
    _set(child, rest, value)


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "<missing>"


_MISSING = _Missing()


def strip_egress(raw: dict) -> list[str]:
    """Delete every :data:`EGRESS_KEYS` entry from ``raw`` in place.

    Returns the keys actually removed. NOT a reporting channel — the production
    caller (`Config._gate_egress`) discards it, deliberately, because
    `check_config_trust` probes the filesystem live rather than trusting a value
    threaded from load time. An earlier docstring here promised "so the caller
    can report what it ignored", which was an unkept API contract: no caller did.
    The return is for tests and for anyone composing these primitives.
    """
    return [key for key in EGRESS_KEYS if _pop(raw, key)]


def capture_egress(raw: dict) -> dict[str, Any]:
    """Snapshot the egress keys AS WRITTEN in the agent-writable file.

    Taken BEFORE the gate strips them, and carried on the Config so ``save()``
    can write the file back with the operator's own prior values intact. Without
    it, the gate's strip is a DESTRUCTIVE read: `techne init` is a
    load→mutate→save round-trip, so the first init after a layer appears rewrites
    config.yaml without `sync`, `connections`, `llm_api_base`, `llm_api_key`,
    `model`, `standalone`, `archivist_db_path` or `stores` — including the
    credentials in `sync.auth` and `llm_api_key`, unrecoverably. Reproduced on PR
    #508 before this existed; found by Prudence, and the test that was supposed to
    cover `save()` seeded no egress key, so it could not have caught it.

    NOT PERSISTING THE OPERATOR'S VALUES AND NOT DESTROYING THE FILE'S OWN ARE
    DIFFERENT REQUIREMENTS, and satisfying only the first produced the mirror
    error: a revocation that outlives the thing that caused it. config.yaml
    records what the agent-side asked for; the gate goes on ignoring it.
    """
    captured: dict[str, Any] = {}
    for key in EGRESS_KEYS:
        value = _get(raw, key)
        if value is not _MISSING:
            captured[key] = value
    return captured


def restore_egress(data: dict, captured: dict[str, Any]) -> list[str]:
    """Write captured egress values back into a serialization dict."""
    for key, value in captured.items():
        _set(data, key, value)
    return list(captured)


def apply_operator_layer(raw: dict, operator_raw: dict) -> list[str]:
    """Make ``operator_raw`` authoritative for the egress set of ``raw``.

    Strips the whole set from ``raw`` first, then re-adds only what the operator
    layer declares. Anything the operator omits is therefore left at the in-code
    default — see :data:`EGRESS_KEYS` for why partial authority is refused.
    """
    strip_egress(raw)
    applied: list[str] = []
    for key in EGRESS_KEYS:
        value = _get(operator_raw, key)
        if value is not _MISSING:
            _set(raw, key, value)
            applied.append(key)
    return applied


def read_operator_layer(path: Path) -> dict:
    """Parse the operator file. A malformed or unreadable layer yields {}.

    Returning {} keeps the fail-closed direction: an operator file that cannot
    be parsed grants nothing, so the built-in default destinations stand. The caller still
    reports the file as present, so this does not read as ``absent``.
    """
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}
