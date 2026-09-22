# Going public — ordered runbook (techne#556)

**Status: PAUSED, DELIBERATELY — not stalled.** Richard's call, 2026-08-17: the
flip waits until more milestones are complete. This document is the plan, not a
record. techne is private as of 2026-08-17.

Nothing here is blocked on effort or on an unanswered question — the sequence is
written, its preconditions are measured, and the two open issues (#556, #592) are
gated on the flip rather than on work. **Do not start any of it opportunistically.**
Step 2 is the point of no return for forks, and the value of this document is
that the whole sequence happens in one sitting, in order, when someone has
decided to.

This exists because #556's most important step — the only one an operator can
take *before* the flip — was written down nowhere anyone would look. A control
that lives only in an issue comment is not a control.

---

## The hazard, in one paragraph

`.github/workflows/test.yml` runs four jobs on `runs-on: [self-hosted, …]`. The
moment the repo is public, a fork PR runs attacker-controlled code on that
runner. **The in-workflow `if:` guards cannot prevent this**: for the
`pull_request` event GitHub evaluates the workflow *from the PR's ref*, so a fork
runs the fork's copy of `test.yml` and an attacker simply deletes the guards
(measured on run 31971484190). The real control is a repo setting — and **it
cannot be turned on while the repo is private**:

```
gh api repos/tenishai/techne/actions/permissions/fork-pr-contributor-approval
→ 422  "Fork PR approval is not allowed for private repositories."
```

Re-verified 2026-08-17. That makes this a **sequencing problem, not a checkbox**.

## The window, and why step 1 closes it rather than shortening it

Between "repo becomes public" and "approval setting applied" there is a window in
which a fork PR would reach the self-hosted runner. You cannot eliminate the
window by being quick.

**You eliminate it by having no runner.** A job whose `runs-on` labels match no
*online* runner **queues — it does not execute.** So with the runner container
stopped, a fork PR arriving mid-window sits in the queue harmlessly until you
bring the runner back, by which time the approval gate is on.

That is the whole reason step 1 comes first, and why "be fast" is not the plan.

---

## Preconditions — verify, do not assume

Run these and compare. Values below were measured 2026-08-17.

```bash
gh api repos/tenishai/techne \
  --jq '"private=\(.private) allow_forking=\(.allow_forking) forks=\(.forks_count) license=\(.license.spdx_id)"'
# expect: private=true allow_forking=false forks=0 license=AGPL-3.0

gh api orgs/tenishai --jq '.members_can_fork_private_repositories'
# expect: false

gh api repos/tenishai/techne/actions/runners \
  --jq '"total=\(.total_count) " + ([.runners[] | "\(.name)(\(.status))"] | join(", "))'
# expect: total=1 mash1-techne(online)

grep -rn "secrets\." .github/          # expect: no output
grep -rn "^permissions:" .github/workflows/test.yml   # expect: no output
```

**Forking is currently disabled at BOTH levels**, which is why the exposure is
theoretical today and why `fork-pr` has never executed (#592). Do not read
"0 forks" as "the guard works" — nothing has ever tested it.

### THERE IS A THIRD LAYER, and it is the one that disappears at the flip

Measured 2026-08-17 by opening a real fork PR (#628, since closed):

```bash
gh api repos/tenishai/techne/actions/permissions/fork-pr-workflows-private-repos
# run_workflows_from_fork_pull_requests:  false   <- fork PRs run NOTHING
# send_secrets_and_variables:             false   <- good
# require_approval_for_fork_pr_workflows: false   <- HAZARD, see below
```

On a **private** repo, fork PRs execute **no workflows at all**. With forking
temporarily enabled at both levels, a real fork PR produced `total=0` check runs —
an entirely empty Checks tab. Nothing ran. Attempting to enable it returns
`422 "Policy is forbidden by entity owner"`: an **org-level** policy forbids it,
readable only with `admin:org` scope and applying to all 20 org repos.

Two consequences, and the second is the one to carry into the flip:

1. **Today's safety does not come from the workflow guard.** It comes from this
   policy, which is private-repos-only. The guard has never been exercised.
2. **This layer vanishes the instant the repo is public.** Everything that has
   been protecting the runner so far stops applying at exactly the moment step 2
   completes — which is why step 1 (no online runner) is not optional.

And note `require_approval_for_fork_pr_workflows: false`. If anyone ever relaxes
the org policy to test the fork path while still private, **fork PR workflows
would run unapproved**. Enable approval in the same sitting or do not do it.

### Blockers outside this repo

- **#207's leaked revision is PURGED — done 2026-08-16, verified 2026-09-22.**
  The hazard was real: the body was redacted 2026-08-13, and the redaction
  *created* a revision holding a collaborator's personal email, where his
  commits use GitHub's private-email address — so publishing it would have
  undone a privacy setting he deliberately enabled. GraphQL now reports that
  revision `deletedAt: 2026-08-16T13:53:55Z`, diff `deleted`, and the current
  body carries no address. Nothing to do here; it is left in writing because a
  cleared blocker that vanishes from the runbook reads as one nobody checked.

  **Settle it yourself rather than trusting this line** — one command, and it
  is the same one that answers the bullet below:

  ```bash
  gh api graphql -f query='query { repository(owner: "tenishai", name: "techne") {
    issue(number: 207) { userContentEdits(first: 20) {
      totalCount nodes { createdAt deletedAt diff } } } } }'
  # expect: totalCount 2, and one node with deletedAt set and diff "deleted"
  ```
- **The rest of the edit histories are clean**, measured 2026-09-22 (#1002):
  383 revisions across all five repos, scanned via GraphQL `userContentEdits`
  because REST cannot see revisions. No credentials; the only emails are
  Richard's own plus-addresses. **Re-run it immediately before the flip** —
  revisions accumulate, and every redaction made from here on creates one.
  Scripts: `fetch_edits.py` + `scan_edits.py`, attached to #1002. That result
  is one person's scan and nobody has reproduced it, so treat the re-run as
  the verification rather than as a formality.
- Other repos have their own blockers (cabal's tracked `.env`, gojira/ladon
  copyright, skills' missing LICENSE). Out of scope here; techne carries
  AGPL-3.0 already. The five-repo readiness pass tracks them in #1003.

---

## The sequence

Do these in order. Do not announce the repo anywhere until step 5 passes.

### 1. Stop the runner container — BEFORE anything else

```bash
ssh mash1 'docker compose -f /var/servers/docker-compose/mash1/ci/compose.yaml down'
ssh mash1 'docker ps -a --filter name=gha-runner-techne --format "{{.Names}}|{{.Status}}"'
# expect: no output, or a container in Exited state
```

**STOP THE CONTAINER; DO NOT ONLY DE-REGISTER.** Measured during the mash1
migration: prinkle's runner **re-registered itself twice** via its stored PAT,
within seconds, taking a new id each time (972 → 974 → 978), because the
entrypoint re-registers on start. API de-registration alone does not stick while
the container runs. Its restart policy is `unless-stopped`, so a manual `docker
stop` also survives a daemon restart — but `compose down` is cleaner and removes
the container outright.

Then confirm GitHub agrees the runner is gone:

```bash
gh api repos/tenishai/techne/actions/runners --jq '.total_count'
# expect: 0, or the runner listed "offline"
```

### 2. Flip the repo to public

Settings → General → Danger Zone → Change visibility → Public.

*(This is deliberately a UI step. Doing it by API invites doing it early.)*

### 3. Apply the approval gate — IMMEDIATELY, same sitting

Settings → Actions → General → **Fork pull request workflows from outside
collaborators** → **Require approval for all outside collaborators.**

Verify by API, because the UI radio is easy to misread:

```bash
gh api repos/tenishai/techne/actions/permissions/fork-pr-contributor-approval
# expect: approval_policy = "all_external_contributors"  (no longer 422)
```

**Do not accept the default.** The public default is believed to be
"first-time contributors only", which the patient attack defeats exactly — land a
typo fix to stop being a first-time contributor, then send the workflow-editing
PR. GitHub's own guidance describes this. That default was never verified (the
API refuses on a private repo), so **check it, do not assume it.**

### 4. Restart the runner

```bash
ssh mash1 'docker compose -f /var/servers/docker-compose/mash1/ci/compose.yaml up -d'
gh api repos/tenishai/techne/actions/runners \
  --jq '[.runners[] | "\(.name)(\(.status))"] | join(", ")'
# expect: mash1-techne(online)
```

Confirm its network isolation is still enforced after the restart — it is a
separate systemd unit and does not come back with the container:

```bash
ssh mash1 'systemctl is-active ci-firewall'   # expect: active
```

### 5. Prove it before announcing

- [ ] `fork-pr-contributor-approval` reports `all_external_contributors`
- [ ] `mash1-techne` online, `ci-firewall` active
- [ ] An in-repo PR still gets its full self-hosted Checks tab
- [ ] **A fork PR is actually exercised** — see #592. Until that has happened
      once, the fork path is a hand trace. This is the step most likely to be
      skipped, and it is the only one that tests the thing being defended.

      **This is the FIRST OPPORTUNITY to do it.** It was attempted 2026-08-17 and
      is not possible while private: an org policy forbids fork-PR workflows on
      private repos, so a real fork PR produced zero check runs. Post-flip that
      policy no longer applies, so this becomes both possible and urgent — the
      window between "the guard is now the only control" and "the guard has been
      tested" should be minutes, not weeks.

      Concretely, once step 3 is verified: fork to a personal account, push a
      one-file branch, open a PR, and confirm the split:

      | job | expected |
      |---|---|
      | `test (3.12)`, `test (3.13)`, `eval-vec`, `arch-cards`, `crash-recovery` | **skipped** |
      | `fork-pr` | **runs** on ubuntu-latest |

      An empty Checks tab is a FINDING, not a pass — that is #530's shape, and it
      is exactly what was observed while private.

---

## What this does NOT fix

**The in-workflow guard remains attacker-editable, by construction.** It is
defence in depth *underneath* the repo setting, never a substitute. What it buys
is non-adversarial exposure (an honest contributor's PR does not land on the
runner) plus fork CI coverage — not protection from someone trying.

**A test-suite invariant cannot backstop it either.** In #570 the guard assertion
runs as pytest step 7, behind `uv pip install -e` (which executes hatchling from
the attacker's `pyproject`), an import of attacker code, and a `conftest` load.
Three arbitrary-code windows execute first. A red X afterwards is a post-mortem,
not a control.

**Ephemeral runners are still the real answer** (#556 item 3). Everything here
makes a compromise *unlikely*; a disposable runner makes it *survivable*. The
runner today is long-lived and sits on a host that now carries the media stack,
the book pipeline and an Arcane agent — the blast radius grew when CI consolidated
onto mash1, and that is recorded rather than argued.

---

## Rollback

If anything in steps 2–5 is wrong, **make the repo private again first**, then
diagnose. Visibility is the only reversible control in the list; the runner can
be stopped again in seconds, but a fork that already exists does not un-exist.

```bash
ssh mash1 'docker compose -f /var/servers/docker-compose/mash1/ci/compose.yaml down'
# then Settings → Change visibility → Private
```

Note that going private again **does not delete forks created while public**, and
re-privatising a repo that had forks has its own consequences on GitHub. Treat
step 2 as the point of no return for forks.
