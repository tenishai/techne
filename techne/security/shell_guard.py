"""Two defensive gates for agent-issued shell execution (techne#120).

**Status: no caller in techne.** techne has no shell-exec path, so nothing here runs
in production; nothing in techne enforces these gates. The module is kept as a library
helper for code that imports it (and for the tests), not as a gate on techne itself.

This is **not a sandbox** — it is two high-leverage, independently-usable gates to
place in front of a command before it reaches a subprocess:

1. ``check_command`` — the **operator anti-chaining gate** (lifted from Outfit). Reject,
   unconditionally and *before* any allow-list, a command string containing a shell
   control operator (``; & | `` `` < > $( ${`` newline). An allow-list alone is unsound:
   ``git diff*`` chains into ``git diff; rm -rf ~``. Gate operators first; let the caller
   layer its own allow-list on top of a known-single command.

2. ``audit_install`` — the **pre-install CVE audit gate** (lifted from ShibaClaw). Detect a
   package-install command and run a vulnerability auditor (``pip-audit`` for pip),
   blocking when it reports a finding. It **fails OPEN** — warns and allows — whenever the
   auditor is absent or an ecosystem has no auditor wired, so bare environments still work.
   The supply-chain moment of an install is the highest-value place to spend an audit.

``run_guarded`` composes both in front of a **no-shell** (argv) ``subprocess.run`` — the
anti-chaining gate guarantees a single command, which is then executed via ``shlex.split``
with ``shell=False`` so no metacharacter is ever interpreted by a shell.

techne itself has no agent-shell-exec path; this module is here so techne, cabal, or
any caller can compose these gates if one is added. The gates are pure/independent
(``check_command`` does no I/O) so they can be used à la carte.

Sources (architect.db): Outfit "Shell operator anti-chaining gate";
ShibaClaw "Pre-execution install-command CVE audit gate (fail-open if auditor absent)".
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Shell control operators: each turns one command into several, or redirects/substitutes
# I/O. Membership is intentionally a substring test, not a parse — the gate is conservative
# by design (favour a false reject over a missed bypass). A command that legitimately needs
# one of these characters in an *argument* should be passed as an argv list, which bypasses
# this string gate entirely.
SHELL_OPERATORS: tuple[str, ...] = (";", "&", "|", "`", "<", ">", "$(", "${", "\n", "\r")


class ShellGuardError(Exception):
    """Base for a command rejected by a gate."""


class CommandChainingError(ShellGuardError):
    """The command contains a shell control operator (chaining / redirection /
    substitution) and was rejected before any allow-list could run."""


class VulnerableInstallError(ShellGuardError):
    """A package-install command was found to install a vulnerable package at or above
    the configured severity threshold."""


class MalformedCommandError(ShellGuardError):
    """The command could not be parsed into argv — empty, or malformed quoting
    (unbalanced quote). Raised by run_guarded so a caller catching ShellGuardError handles
    it alongside the gate rejections, rather than a bare shlex ``ValueError``."""


def check_command(command: str) -> None:
    """Raise :class:`CommandChainingError` if *command* contains a shell control operator.

    Call this **before** any allow-list — the operator gate is unconditional and is the
    whole point: an allow-list that matches a prefix (``git diff*``) is bypassed by
    ``git diff; rm -rf ~``. Does no I/O; safe to call anywhere. Returns ``None`` (the
    command is a single, un-chained command) on success.

    SCOPE: this prevents *chaining* (running a second program), not single-command shell
    *expansion*. Globs (``* ? [``), ``~``, ``$VAR``, ``(`` subshells, ``!`` history, ``#``
    are intentionally NOT rejected — under ``run_guarded`` they reach ``execve`` literally
    (``shell=False``) and are harmless. A caller that wires this in front of a ``shell=True``
    path still needs its own allow-list: this gate stops ``rm a; rm b`` but not ``rm *``.
    """
    found = [op for op in SHELL_OPERATORS if op in command]
    if found:
        shown = ", ".join(repr(op) for op in found)
        raise CommandChainingError(
            f"command rejected: contains shell control operator(s) {shown}. "
            "Chaining/redirection/substitution is not allowed; pass a single command "
            "(or an argv list, which bypasses this gate)."
        )


# ---------------------------------------------------------------------------
# Pre-install CVE audit
# ---------------------------------------------------------------------------

# Severity rank for threshold comparison. Auditors name severities inconsistently
# (npm: low/moderate/high/critical; OSV/CVSS: low/medium/high/critical); both spellings
# map here. Unknown/missing severity sorts ABOVE the highest known rank so it is never
# silently dropped below a threshold — an un-scored vuln still blocks at any threshold.
_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "moderate": 1,
    "medium": 1,
    "high": 2,
    "critical": 3,
}
_UNKNOWN_SEVERITY_RANK = 99  # sorts above everything → never filtered out by a threshold

# install-command detection: (ecosystem, the subcommand tokens that mean "install")
_INSTALL_ECOSYSTEMS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (ecosystem, executable tokens, install-subcommand tokens). `python -m pip install`
    # is unwrapped to the pip case in _detect_install before this table is consulted.
    ("pip", ("pip", "pip3"), ("install",)),
    ("npm", ("npm",), ("install", "i", "add")),
    ("npm", ("pnpm", "yarn"), ("install", "add")),
    ("apt", ("apt", "apt-get"), ("install",)),
    ("brew", ("brew",), ("install",)),
)


@dataclass
class AuditResult:
    """Outcome of :func:`audit_install`.

    ``ok`` is the gate decision (True = allow). ``audited`` says whether an auditor
    actually ran (False = not an install command, or fail-open). ``findings`` holds the
    vulnerabilities at/above the threshold when ``audited`` and not ``ok``.
    """

    ok: bool
    audited: bool
    ecosystem: str | None
    reason: str
    findings: list[dict] = field(default_factory=list)


def _detect_install(command: str) -> tuple[str | None, list[str]]:
    """Classify *command*: return ``(ecosystem, packages)`` or ``(None, [])``.

    Best-effort token scan (the command is already known single + un-chained when called
    from run_guarded). ``packages`` are the non-flag tokens after the install subcommand;
    they may include version specifiers (``requests==2.20`` ) — passed through verbatim.

    DELIBERATELY shallow — this audit is defense-in-depth, not a sandbox, and is fail-open
    by design, so detection misses degrade to "allowed unaudited", never a false block.
    Known, accepted gaps: non-standard exe names (``pip3.11``, a venv's absolute path to a
    differently-named shim); ``-r requirements.txt`` / ``-e .`` / VCS specs (no concrete
    named package → not audited); a space-separated flag VALUE (``--index-url URL``) is read
    as a package and handed to pip-audit (which then resolves or errors → fail open). Never
    rely on this as the sole control.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, []
    if not tokens:
        return None, []

    exe = Path(tokens[0]).name  # strip any path: /usr/bin/pip3 -> pip3
    rest = tokens[1:]

    # `python -m pip install ...` — unwrap to the pip case.
    if exe in ("python", "python3") and rest[:2] == ["-m", "pip"]:
        exe, rest = "pip", rest[2:]

    for ecosystem, exes, subcommands in _INSTALL_ECOSYSTEMS:
        if exe not in exes:
            continue
        # Locate the subcommand, skipping leading global flags so `pip --quiet install evil`
        # can't evade the audit by putting a flag before `install`.
        sub_idx = next((i for i, t in enumerate(rest) if not t.startswith("-")), None)
        if sub_idx is not None and rest[sub_idx] in subcommands:
            packages = [t for t in rest[sub_idx + 1 :] if not t.startswith("-")]
            return ecosystem, packages
    return None, []


def _severity_at_or_above(severity: str | None, threshold: str) -> bool:
    """True if *severity* (auditor-reported, possibly None) is >= *threshold*."""
    sev_rank = _SEVERITY_RANK.get((severity or "").lower(), _UNKNOWN_SEVERITY_RANK)
    thr_rank = _SEVERITY_RANK.get(threshold.lower(), _SEVERITY_RANK["high"])
    return sev_rank >= thr_rank


def _pip_audit_findings(packages: list[str], threshold: str, timeout: float) -> list[dict]:
    """Run ``pip-audit`` against a temp requirements file built from *packages* — a real
    PRE-install audit (pip-audit resolves + checks the requirements without installing
    them). Returns findings at/above *threshold*. Raises FileNotFoundError if pip-audit is
    not on PATH (caller turns that into fail-open)."""
    with tempfile.TemporaryDirectory() as td:
        req = Path(td) / "requirements.txt"
        req.write_text("\n".join(packages) + "\n")
        proc = subprocess.run(
            [
                "pip-audit",
                "--requirement",
                str(req),
                "--format",
                "json",
                "--progress-spinner",
                "off",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    # pip-audit exits non-zero WHEN it finds vulns; the JSON is still on stdout. Only a
    # genuinely empty/garbage stdout is an error we should fail-open on.
    if not proc.stdout.strip():
        raise RuntimeError(f"pip-audit produced no output (stderr: {proc.stderr.strip()[:200]})")
    data = json.loads(proc.stdout)
    # Pin the schema contract: pip-audit ≥ 2.x always emits a top-level "dependencies" list
    # (even with zero vulns). If a future version changes shape, `data.get("dependencies",
    # [])` would silently yield no findings → fail OPEN unnoticed. Raise instead, so the
    # caller fails open WITH a logged reason (and a test trips on the contract change).
    if not isinstance(data, dict) or "dependencies" not in data:
        raise RuntimeError("pip-audit JSON missing 'dependencies' — unexpected schema")
    findings: list[dict] = []
    for dep in data.get("dependencies", []):
        name = dep.get("name", "")
        version = dep.get("version", "")
        for vuln in dep.get("vulns", []):
            severity = _extract_severity(vuln)
            if _severity_at_or_above(severity, threshold):
                findings.append(
                    {
                        "package": name,
                        "version": version,
                        "id": vuln.get("id", ""),
                        "severity": severity,
                        "fix_versions": vuln.get("fix_versions", []),
                    }
                )
    return findings


def _extract_severity(vuln: dict) -> str | None:
    """Best-effort coarse severity label, or None.

    NB (verified vs pip-audit 2.10.1): pip-audit's vuln objects are
    ``{id, fix_versions, aliases, description}`` with NO ``severity`` — and the aliases are
    CVE/GHSA/SNYK IDs, not severity labels. So this returns None for every real pip-audit
    finding today, which the threshold treats as 'unknown' → blocks at any threshold. The
    ``severity``/alias probing is forward-compat for an auditor that DOES emit a label."""
    sev = vuln.get("severity")
    if isinstance(sev, str) and sev.strip():
        return sev.strip()
    aliases = vuln.get("aliases") or []
    for a in aliases:
        if isinstance(a, str) and a.lower() in _SEVERITY_RANK:
            return a
    return None


# Auditor per ecosystem: the CLI that must be on PATH, and the findings fn. pip is wired
# (pip-audit gives a clean pre-install audit via -r). npm/apt/brew are detected but have no
# clean *pre-install* auditor wired in v1 — they fail open with an explicit reason rather
# than pretend coverage. The seam is here for later.
_AUDITORS: dict[str, str] = {"pip": "pip-audit"}


def audit_install(
    command: str,
    *,
    threshold: str = "high",
    timeout: float = 60.0,
) -> AuditResult:
    """Detect a package-install command and audit it for known vulnerabilities.

    FAILS OPEN (``ok=True``) when: the command is not an install command; the ecosystem has
    no auditor wired; the auditor is not installed; or the auditor errors. Only a clean run
    that REPORTS a vulnerability at/above *threshold* blocks (``ok=False``). *threshold* is
    one of low/moderate(medium)/high/critical.

    HONEST CAVEAT (verified against pip-audit 2.10.1): pip-audit's JSON carries NO severity
    field — only id/aliases/fix_versions. So every vuln it reports has 'unknown' severity,
    which by design ranks above any threshold → for pip, *threshold* is effectively inert
    and EVERY known vuln blocks (fail-closed — the safe direction). The threshold mechanism
    is kept for auditors that DO emit severity (and so callers can opt into a softer gate if
    one is wired later); it just doesn't loosen the pip gate today.
    """
    ecosystem, packages = _detect_install(command)
    if ecosystem is None:
        return AuditResult(ok=True, audited=False, ecosystem=None, reason="not an install command")
    if not packages:
        # e.g. `pip install -r requirements.txt` (the spec is a flag) or `npm install` (no
        # args) — nothing concrete to audit pre-install. Fail open.
        return AuditResult(
            ok=True,
            audited=False,
            ecosystem=ecosystem,
            reason=f"{ecosystem}: no concrete package to audit — allowed (fail open)",
        )

    auditor = _AUDITORS.get(ecosystem)
    if auditor is None:
        return AuditResult(
            ok=True,
            audited=False,
            ecosystem=ecosystem,
            reason=f"{ecosystem}: no pre-install auditor wired — allowed (fail open)",
        )
    if shutil.which(auditor) is None:
        log.warning("shell_guard: %s not on PATH — allowing install unaudited (fail open)", auditor)
        return AuditResult(
            ok=True,
            audited=False,
            ecosystem=ecosystem,
            reason=f"{auditor} not installed — allowed (fail open)",
        )

    try:
        findings = _pip_audit_findings(packages, threshold, timeout)
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        RuntimeError,
        OSError,
    ) as e:
        log.warning(
            "shell_guard: %s failed (%s) — allowing install unaudited (fail open)", auditor, e
        )
        return AuditResult(
            ok=True,
            audited=False,
            ecosystem=ecosystem,
            reason=f"{auditor} failed ({type(e).__name__}) — allowed (fail open)",
        )

    if findings:
        ids = ", ".join(f"{f['package']} {f['id']}" for f in findings)
        return AuditResult(
            ok=False,
            audited=True,
            ecosystem=ecosystem,
            reason=f"{ecosystem}: {len(findings)} vulnerability(ies) at/above '{threshold}': {ids}",
            findings=findings,
        )
    return AuditResult(
        ok=True,
        audited=True,
        ecosystem=ecosystem,
        reason=f"{ecosystem}: audited, no vulnerability at/above '{threshold}'",
    )


def run_guarded(
    command: str,
    *,
    audit: bool = True,
    threshold: str = "high",
    audit_timeout: float = 60.0,
    **subprocess_kwargs,
) -> subprocess.CompletedProcess:
    """Run *command* through both gates, then execute it WITHOUT a shell.

    1. ``check_command`` — raises :class:`CommandChainingError` on any shell operator.
    2. ``audit_install`` (when *audit*) — raises :class:`VulnerableInstallError` on a
       vulnerable install at/above *threshold*; fails open otherwise.
    3. ``subprocess.run(shlex.split(command), shell=False, **subprocess_kwargs)`` — argv
       execution, never a shell, so the (now operator-free) command can't expand or chain.

    Extra ``subprocess_kwargs`` (cwd, env, timeout, capture_output, text, check, …) pass
    through. Raises :class:`MalformedCommandError` (a ShellGuardError) if *command* is empty
    or has malformed quoting. Passing ``shell=True`` is a ``TypeError`` (it's hard-pinned
    False) — by design, this wrapper never invokes a shell.
    """
    check_command(command)
    if audit:
        result = audit_install(command, threshold=threshold, timeout=audit_timeout)
        if not result.ok:
            raise VulnerableInstallError(result.reason)

    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise MalformedCommandError(f"could not parse command (malformed quoting?): {e}") from e
    if not argv:
        raise MalformedCommandError("empty command")
    return subprocess.run(argv, shell=False, **subprocess_kwargs)
