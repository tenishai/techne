"""Evidence-anchor resolution and graded staleness (techne#90).

A *memory* that points into a repo (a `file:line`, function, or flag) is an
**evidence-anchored** memory. At write time we capture a content hash of the
anchored file plus the git HEAD sha; at recall time we re-classify that anchor
against the current working tree into one of five states so the consuming agent
spends verification cost only where it's warranted:

    unchanged — file present, bytes identical → trust as-is
    modified  — file present, bytes differ    → surface, re-read before quoting
    moved     — file gone, identical content found elsewhere in the repo
    deleted   — file gone, no matching content found
    diverged  — bytes identical, but the commit the anchor was pinned at is no
                longer in HEAD's history (rebase, force-push, other branch) —
                the match may be coincidental (techne#391). Checked only when the
                bytes match; a sha this clone has never seen (or one already
                garbage-collected) is "cannot tell" and stays ``unchanged``.

A sixth, ``unknown``, is the honest off-git answer: the anchor can't be resolved
on this host (no repo checked out, path outside any repo, git failure/timeout).
Per the portable-memory principle (techne#57) memory follows the operator across
machines and clients where the repo may not exist at all — so ``unknown`` NEVER
asserts freshness and NEVER invalidates. Every error path funnels here with a
human-readable reason; nothing in this module raises to the caller.

Only memories with a non-NULL ``anchor_path`` are ever checked. Behavioral and
relational memory (``feedback_*``, ``user_*``) has no anchor and is untouched.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Bound on the work `moved` detection will do. When a repo tracks more files
# than this we skip the (potentially expensive) content scan and report the
# missing file as `deleted` rather than hashing the whole tree on a recall.
MAX_MOVED_SCAN_FILES = 2000

# All git subprocess calls share one timeout so a hung/locked repo can never
# wedge a recall — it degrades to `unknown` instead.
_GIT_TIMEOUT = 10


def _sha256_file(path: Path) -> str:
    """Return the hex sha256 of *path*'s bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Promoted to techne.core.repo (techne#209) so the Codex per-repo router shares
# the same "which repo owns this path" logic. Re-exported under the original
# private name to keep this module's callers (and any tests) untouched.
from techne.core.repo import find_repo_root as _find_repo_root  # noqa: E402


def _git(repo_root: Path, *args: str) -> tuple[bool, str]:
    """Run ``git -C <repo_root> <args>`` with a hard timeout.

    Returns ``(ok, output)``. ``ok`` is False on non-zero exit, timeout, or any
    OS error; ``output`` is stdout (stripped) on success, else a reason string.
    Never raises.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"git {args[0] if args else ''} timed out"
    except (OSError, ValueError) as e:  # git missing, bad args, etc.
        return False, f"git invocation failed: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "git non-zero exit").strip()
    return True, proc.stdout.strip()


def resolve_anchor(path: str) -> dict | None:
    """Resolve an anchor at WRITE time.

    Locates *path*, finds its git repo root, and captures the content hash plus
    the current HEAD sha. Returns::

        {"repo_rel_path": str, "content_hash": str, "git_sha": str}

    Returns None when the file does not exist OR is not inside a git repo (no
    anchor can be pinned). All subprocess calls are timeout-guarded.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    repo_root = _find_repo_root(p.resolve())
    if repo_root is None:
        return None

    try:
        content_hash = _sha256_file(p)
    except OSError:
        return None

    ok, head = _git(repo_root, "rev-parse", "HEAD")
    if not ok:
        return None

    try:
        repo_rel_path = str(p.resolve().relative_to(repo_root))
    except ValueError:
        # p resolved outside the discovered root (symlink edge case) — fall back
        # to the basename so we still persist *something* useful.
        repo_rel_path = p.name

    return {"repo_rel_path": repo_rel_path, "content_hash": content_hash, "git_sha": head}


def _find_moved(
    repo_root: Path, original_rel: str | None, stored_content_hash: str
) -> tuple[str | None, bool]:
    """Best-effort: find a tracked file whose sha256 matches *stored_content_hash*
    at a path different from *original_rel*.

    Returns ``(new_repo_rel_path_or_None, scan_ok)``. ``scan_ok`` is False ONLY
    when the ``git ls-files`` call itself failed/timed out — so the caller emits
    ``unknown`` (per the contract) instead of a possibly-wrong ``deleted``. A
    completed-but-empty scan (incl. the deliberate too-large degrade) returns
    ``(None, True)`` → ``deleted``.
    """
    ok, listing = _git(repo_root, "ls-files")
    if not ok:
        return None, False  # git failure → caller emits `unknown`, not `deleted`
    rel_paths = [line for line in listing.splitlines() if line]
    if len(rel_paths) > MAX_MOVED_SCAN_FILES:
        # Too large to scan on a recall — documented degrade to `deleted`.
        return None, True
    for rel in rel_paths:
        if rel == original_rel:
            continue
        candidate = repo_root / rel
        if not candidate.is_file():
            continue
        try:
            if _sha256_file(candidate) == stored_content_hash:
                return rel, True
        except OSError:
            continue
    return None, True


def _history_diverged(anchor_file: Path, stored_git_sha: str | None) -> bool:
    """True only when git says *stored_git_sha* is definitely NOT an ancestor of HEAD.

    ``git merge-base --is-ancestor`` exits 0 (ancestor), 1 (not an ancestor), or
    something else (unknown object, no repo, …). Only exit 1 counts as diverged:
    a sha this clone has never fetched (memory written on another machine) is
    "cannot tell", and an unverifiable history must not downgrade content the
    hash check just verified. Never raises.
    """
    if not stored_git_sha:
        return False
    repo_root = _find_repo_root(anchor_file.resolve())
    if repo_root is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", stored_git_sha, "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False
    return proc.returncode == 1


def classify(
    anchor_path: str,
    stored_content_hash: str | None,
    stored_git_sha: str | None,
) -> tuple[str, str]:
    """Classify an anchor's freshness at RECALL time.

    Returns ``(state, detail)`` where *state* is one of
    ``unchanged | diverged | modified | moved | deleted | unknown``:

      - ``unchanged`` — file present, sha256 == stored → ``('unchanged', '')``
      - ``diverged``  — sha256 == stored, but *stored_git_sha* is no longer an
                        ancestor of HEAD → ``('diverged', '<short sha>')``
      - ``modified``  — file present, sha256 differs    → ``('modified', '')``
      - ``moved``     — file gone, identical content elsewhere in repo
                        → ``('moved', '<new repo-rel path>')``
      - ``deleted``   — file gone, no match            → ``('deleted', '')``
      - ``unknown``   — anchor unresolvable on this host (no repo, path outside
                        any repo, no stored hash, git failure/timeout)
                        → ``('unknown', '<reason>')``

    Never raises — every failure becomes ``('unknown', reason)``.
    """
    if stored_content_hash is None:
        return "unknown", "no content hash stored"

    try:
        p = Path(anchor_path).expanduser()

        if p.is_file():
            try:
                current = _sha256_file(p)
            except OSError as e:
                return "unknown", f"could not read anchor file: {e}"
            if current == stored_content_hash:
                if _history_diverged(p, stored_git_sha):
                    return "diverged", (stored_git_sha or "")[:12]
                return "unchanged", ""
            return "modified", ""

        # File is gone — we need a repo to say anything more than "unknown".
        repo_root = _find_repo_root(p.resolve() if p.is_absolute() else p.absolute())
        if repo_root is None:
            return "unknown", "anchor path is not inside a git repository"

        # Original repo-relative path (best effort) so we don't report the file
        # as "moved" to its own (now-absent) location.
        original_rel: str | None = None
        try:
            original_rel = str(p.absolute().relative_to(repo_root))
        except ValueError:
            original_rel = None

        new_path, scan_ok = _find_moved(repo_root, original_rel, stored_content_hash)
        if new_path is not None:
            return "moved", new_path
        if not scan_ok:
            # ls-files itself failed — we didn't finish looking, so honor the
            # contract: `unknown`, not a possibly-wrong `deleted`.
            return "unknown", "git ls-files failed while checking for a moved file"
        return "deleted", ""
    except Exception as e:  # noqa: BLE001 — hard guarantee: no crash escapes recall
        return "unknown", f"anchor classification failed: {e}"


# --- recall-time freshness tagging ------------------------------------------

# Per-state recall presentation. `unchanged` carries no tag (no cost to the
# consumer); everything else is surfaced — nothing is ever silently dropped.
_RE_READ = "re-read before quoting"


def _freshness_payload(state: str, detail: str) -> dict | None:
    """Build the freshness payload attached to a recalled, anchored row.

    Returns None for `unchanged` (no tag — trust as-is). Otherwise a dict with a
    machine-readable `state` plus a human `tag` string the consumer can render.
    """
    if state == "unchanged":
        return None
    if state == "modified":
        tag = f"[freshness: modified — {_RE_READ}]"
        note = "anchored file changed since this was stored; reduced confidence"
    elif state == "diverged":
        at = f" {detail}" if detail else ""
        tag = f"[freshness: diverged — anchored commit{at} is not in HEAD's history; {_RE_READ}]"
        note = (
            "file bytes match, but the commit this was pinned at was rebased away, "
            "force-pushed over, or is on another branch; the match may be coincidental"
        )
    elif state == "moved":
        where = f" → {detail}" if detail else ""
        tag = f"[freshness: moved{where} — {_RE_READ}]"
        note = "anchored file moved; follow the new path and re-confirm"
    elif state == "deleted":
        tag = "[freshness: deleted — anchored file no longer present]"
        note = "anchored file is gone from the repo"
    else:  # unknown
        reason = f" ({detail})" if detail else ""
        tag = f"[freshness: unknown — anchor not verifiable here{reason}]"
        note = "anchor could not be resolved on this host; not asserting freshness"
    payload: dict = {"state": state, "tag": tag, "note": note}
    if detail:
        payload["detail"] = detail
    return payload


def annotate_freshness(db_path, rows: Iterable[dict]) -> list[dict]:
    """Attach a `freshness` payload to recalled knowledge rows that are anchored.

    Looks up the stored anchor for each knowledge result and classifies it
    against the current working tree. Mutates and returns *rows*.

    Scope guarantees (techne#90 hard constraints):
      - Only rows with a non-NULL ``anchor_path`` are touched. Non-anchored
        memory (the majority — behavioral/relational) gets no `freshness` field
        and is never read from disk or git.
      - `unchanged` adds no tag. No row is ever dropped or invalidated here —
        recall only *annotates*.
      - This runs on the recall/surfacing path only, never on touch/relevance
        updates, so high-frequency reaffirm paths pay nothing.
    """
    from techne.core.database import connect

    rows = list(rows)
    knowledge_ids = [
        r["id"] for r in rows if r.get("source_table") == "knowledge" and r.get("id") is not None
    ]
    if not knowledge_ids:
        return rows

    placeholders = ",".join("?" * len(knowledge_ids))
    conn = connect(db_path)
    try:
        anchored = {
            row["id"]: row
            for row in conn.execute(
                f"""SELECT id, anchor_path, anchor_symbol, content_hash, anchor_git_sha
                    FROM knowledge
                    WHERE id IN ({placeholders}) AND anchor_path IS NOT NULL""",
                knowledge_ids,
            )
        }
    finally:
        conn.close()

    if not anchored:
        return rows

    for r in rows:
        if r.get("source_table") != "knowledge":
            continue
        meta = anchored.get(r.get("id"))
        if meta is None:  # non-anchored row — untouched
            continue
        anchor_path = meta["anchor_path"]
        symbol = meta["anchor_symbol"]
        state, detail = classify(anchor_path, meta["content_hash"], meta["anchor_git_sha"])
        r["anchor_path"] = anchor_path
        if symbol:
            r["anchor_symbol"] = symbol
        payload = _freshness_payload(state, detail)
        if payload is not None:
            r["freshness"] = payload
    return rows
