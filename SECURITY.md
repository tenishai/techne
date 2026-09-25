# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately, either way:

- **Email:** security@tenish.ai
- **GitHub:** the **Report a vulnerability** button on this repository's
  **Security** tab (private vulnerability reporting).

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
- credential and secret handling: redaction before storage, and anything that
  lets a secret reach the store, an export, or an injected prompt;
- egress: configuration that is supposed to keep memory local but doesn't;
- the hooks and the MCP server: anything a tool result, transcript, or
  repository file can use to run code or change techne's configuration.

**Out of scope:** vulnerabilities in third-party dependencies (please report
those upstream, and tell us if we ship an affected version), and problems that
require an attacker to already control the machine or the operator account
techne runs under.
