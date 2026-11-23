# Going public — the sequence (techne#556)

**Status: PAUSED, DELIBERATELY — not stalled.** Richard's call, 2026-08-17: the
flip waits until more milestones are complete. This is the plan, not a record.

**The host-specific steps are held privately and are not in this repo (#1005).**
They name the CI host and its paths, in a file that would ship with the public
repo. The operator has them; ask before executing.

---

## CI no longer uses a runner of ours

Every job in `.github/workflows/test.yml` runs on a GitHub-hosted runner
(`ubuntu-latest`), and fork PRs run the same jobs as in-repo PRs. The old hazard,
attacker-controlled code from a fork PR on a machine we run, is gone because no
workflow targets such a machine, not because a guard keeps forks away from it.
`tests/test_ci_workflow_triggers.py` pins this: no job in any workflow file may
target a runner that is not provably GitHub-hosted, and no job may carry an `if:`
that skips fork PRs.

The `ci` job in that workflow is the one check a ruleset should require. It fails
unless every other job succeeded, so a skipped or cancelled job cannot read as a
pass.

## The order

1. Stop and deregister the self-hosted techne runners, **after the change that
   moved CI to hosted runners has merged**. Nothing in this repo needs them
   any more, and a runner that stays registered is a standing target the moment
   the repo is public. Stop the container as well as deregistering: an ephemeral
   runner's entrypoint re-registers on start, so API de-registration alone does
   not stick while the container runs. Confirm GitHub lists no runner for the repo.
2. Flip the repo to public. *(Deliberately a UI step — doing it by API invites
   doing it early.)*
3. Apply the fork-PR approval gate **in the same sitting**, set to
   all outside collaborators. **Do not accept the default**, which is believed to be
   first-time contributors only — that is defeated by landing a typo fix first.
   Verify by API; the UI radio is easy to misread. The setting cannot be applied
   while the repo is private:

   ```
   gh api repos/tenishai/techne/actions/permissions/fork-pr-contributor-approval
   → 422  "Fork PR approval is not allowed for private repositories."
   ```

   With hosted runners the window before this step costs minutes, not a
   compromised host, but a fork PR can still spend them.
4. Require the `ci` check in a ruleset on `main`.
5. **Exercise a real fork PR before announcing** (#592). Until that has happened
   once, the fork path is a hand trace, and it is the only step that tests the
   thing being defended.

## What protects the repo today, and why it changes

On a **private** repo an org-level policy means fork PRs execute no workflows at
all. Verified by opening a real fork PR: zero check runs, an empty Checks tab.

That policy is private-repos-only. It stops applying when step 2 completes, so
step 5 is the first time the fork path runs for real.

## Blockers outside this repo

- **#207's leaked revision is purged** — done 2026-08-16, verified 2026-09-22.
  Nothing to do; recorded because a cleared blocker that vanishes reads as one
  nobody checked. Settle it yourself:

  ```bash
  gh api graphql -f query='query { repository(owner: "tenishai", name: "techne") {
    issue(number: 207) { userContentEdits(first: 20) {
      totalCount nodes { createdAt deletedAt diff } } } } }'
  ```

  Run 2026-09-22 it returned `totalCount 2`, one node with
  `deletedAt: 2026-08-16T13:53:55Z` and diff `deleted`. A *second* deleted
  revision appearing later means something was redacted since.

- **The rest of the edit histories are clean**, measured 2026-09-22 (#1002): 383
  revisions across five repos via GraphQL `userContentEdits`, because REST
  cannot see revisions. No credentials. **Re-run immediately before the flip** —
  every redaction from here on creates a new one, so that result is only true
  for the moment it ran, and nobody has reproduced it.

- Other repos have their own blockers. The five-repo readiness pass tracks them
  in #1003.
