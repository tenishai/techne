# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately to **security@tenish.ai**.

If this repository's **Security** tab shows a **Report a vulnerability**
button, you can use that instead. GitHub offers it only on public
repositories where it has been turned on.

Include what you can of:

- what the problem is and what an attacker could do with it;
- the version, commit, or tag you tested;
- steps to reproduce, or a proof of concept;
- any configuration it depends on.

You don't need a complete write-up to report. A clear description of the
problem is enough to start.

## What happens next

- We acknowledge your report within **14 days**.
- We tell you whether we've confirmed the problem as soon as we know.
- We fix confirmed problems in the next release, or sooner for serious ones,
  and tell you before the fix is public.
- We credit you in the release notes unless you'd rather we didn't.

We ask that you give us a reasonable chance to fix a problem before disclosing
it publicly, and that you don't access or change data that isn't yours while
testing.

## Supported versions

techne is pre-1.0. Security fixes go into the **latest release** only; older
releases are not patched. If you're running an older version, upgrade to get
fixes.

## Scope

techne is a local memory store and MCP server for AI agents. In scope:

- the stored memory: anything that lets one agent or project read or change
  another's memory without being configured to;
- credential and secret handling: a secret in a format techne's redaction is
  meant to recognise that still reaches the memory store, an export, or an
  injected prompt. Redaction is pattern-based and best-effort, not a
  confidentiality guarantee; a token shape it doesn't recognise yet is a
  hardening request, not a vulnerability.
  The architect pattern store, the codex code-graph store, and short
  structured fields (source, tags, target) are not redacted by design;
- egress: configuration that is supposed to keep memory local but doesn't.
  Only `/etc/techne/config.operator.yaml` is a lock, and only while the process
  reading it cannot write it. `$TECHNE_OPERATOR_CONFIG` and
  `<techne_home>/config.operator.yaml` are conveniences an agent can bypass
  (`env -u`, or a different `TECHNE_HOME`), and with no operator layer
  `config.yaml` keeps full authority. An agent redirecting its own egress in
  those setups is documented behaviour, not a vulnerability (see "Operator-owned
  config" in the README);
- the hooks and the MCP server: anything a tool result, transcript, or
  repository file can use to run code or change techne's configuration;
- the shell guard (`techne.security.shell_guard`), a library for callers that
  run agent-issued commands:
  - `check_command`: a command string it accepts that a shell would still
    read as chaining, redirection, or command substitution. Single-command
    expansion (globs, `~`, `$VAR`, `(` subshells, `!` history, `#`) is
    accepted by design, since `run_guarded` passes it to the program
    literally; and a single program that starts others (`bash -c`, `xargs`,
    `find -exec`) is the caller's allow-list's job, not this check's;
  - `audit_install`: a `pip install` of a named package with a known
    vulnerability that it allows while `pip-audit` runs successfully.
    Install detection is shallow and fails open by design (other pip
    spellings, `-r`/`-e`, other package managers, audit errors), so gaps
    there are hardening requests.

**Out of scope:** vulnerabilities in third-party dependencies (please report
those upstream, and tell us if we ship an affected version), and problems that
require an attacker to already control the machine or the operator account
techne runs under.
