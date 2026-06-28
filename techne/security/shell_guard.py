"""Two defensive gates for agent-issued shell execution (techne#120).

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

techne itself has no agent-shell-exec path today; this module exists so techne, cabal, or
any caller can compose these gates the moment one is added. The gates are pure/independent
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


def check_command(command: str) -> None:
    """Raise :class:`CommandChainingError` if *command* contains a shell control operator.

    Call this **before** any allow-list — the operator gate is unconditional and is the
    whole point: an allow-list that matches a prefix (``git diff*``) is bypassed by
    ``git diff; rm -rf ~``. Does no I/O; safe to call anywhere. Returns ``None`` (the
    command is a single, un-chained command) on success.
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
        if rest and rest[0] in subcommands:
            packages = [t for t in rest[1:] if not t.startswith("-")]
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
            ["pip-audit", "--requirement", str(req), "--format", "json",
             "--progress-spinner", "off"],
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
    """pip-audit/OSV severity is irregular (sometimes absent, sometimes a CVSS vector,
    sometimes a label). Pull a coarse label when present; None otherwise (which our
    threshold treats as 'unknown' → blocks at any threshold)."""
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
    """
    ecosystem, packages = _detect_install(command)
    if ecosystem is None:
        return AuditResult(ok=True, audited=False, ecosystem=None, reason="not an install command")
    if not packages:
        # e.g. `pip install -r requirements.txt` (the spec is a flag) or `npm install` (no
        # args) — nothing concrete to audit pre-install. Fail open.
        return AuditResult(
            ok=True, audited=False, ecosystem=ecosystem,
            reason=f"{ecosystem}: no concrete package to audit — allowed (fail open)",
        )

    auditor = _AUDITORS.get(ecosystem)
    if auditor is None:
        return AuditResult(
            ok=True, audited=False, ecosystem=ecosystem,
            reason=f"{ecosystem}: no pre-install auditor wired — allowed (fail open)",
        )
    if shutil.which(auditor) is None:
        log.warning("shell_guard: %s not on PATH — allowing install unaudited (fail open)", auditor)
        return AuditResult(
            ok=True, audited=False, ecosystem=ecosystem,
            reason=f"{auditor} not installed — allowed (fail open)",
        )

    try:
        findings = _pip_audit_findings(packages, threshold, timeout)
    except (
        FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError, OSError
    ) as e:
        log.warning(
            "shell_guard: %s failed (%s) — allowing install unaudited (fail open)", auditor, e
        )
        return AuditResult(
            ok=True, audited=False, ecosystem=ecosystem,
            reason=f"{auditor} failed ({type(e).__name__}) — allowed (fail open)",
        )

    if findings:
        ids = ", ".join(f"{f['package']} {f['id']}" for f in findings)
        return AuditResult(
            ok=False, audited=True, ecosystem=ecosystem,
            reason=f"{ecosystem}: {len(findings)} vulnerability(ies) at/above '{threshold}': {ids}",
            findings=findings,
        )
    return AuditResult(
        ok=True, audited=True, ecosystem=ecosystem,
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
    through. Raises ``ValueError`` if *command* is empty after splitting.
    """
    check_command(command)
    if audit:
        result = audit_install(command, threshold=threshold, timeout=audit_timeout)
        if not result.ok:
            raise VulnerableInstallError(result.reason)

    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")
    return subprocess.run(argv, shell=False, **subprocess_kwargs)
