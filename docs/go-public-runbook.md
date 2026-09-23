# Going public — the sequence (techne#556)

**Status: PAUSED, DELIBERATELY — not stalled.** Richard's call, 2026-08-17: the
flip waits until more milestones are complete. This is the plan, not a record.

**The step-by-step runbook is held privately and is not in this repo (#1005).**
It names the CI host and its paths — a map of the machine this sequence exists
to protect, in a file that would ship with the
public repo. The operator has it; ask before executing.

What is safe to say here, because the workflow already shows it, is *why* the
order matters. Anyone editing CI needs that much.

---

## The hazard

`.github/workflows/test.yml` runs jobs on a self-hosted runner. The moment the
repo is public, a fork PR runs attacker-controlled code on that machine.

**The in-workflow `if:` guards cannot prevent this.** For the `pull_request`
event GitHub evaluates the workflow *from the PR's ref*, so a fork runs the
fork's copy of `test.yml` and an attacker simply deletes the guards.

The real control is a repo setting — and it cannot be enabled while the repo is
private:

```
gh api repos/tenishai/techne/actions/permissions/fork-pr-contributor-approval
→ 422  "Fork PR approval is not allowed for private repositories."
```

That makes this a **sequencing problem, not a checkbox.**

## Why the runner comes down first

Between "repo becomes public" and "approval setting applied" there is a window
in which a fork PR reaches the runner. You cannot close it by being quick.

You close it by having no runner. **A job whose `runs-on` labels match no
*online* runner queues — it does not execute.** With the runner stopped, a fork
PR arriving mid-window sits harmlessly until the gate is on.

Stop the container; do not only de-register. An ephemeral runner's entrypoint
re-registers on start, and API de-registration does not stick while the
container runs (measured: one re-registered itself twice within seconds).

## The order

1. Stop the runner container. Confirm GitHub agrees it is gone.
2. Flip the repo to public. *(Deliberately a UI step — doing it by API invites
   doing it early.)*
3. Apply the fork-PR approval gate **in the same sitting**, set to all outside
   collaborators. **Do not accept the default**, which is believed to be
   first-time contributors only — that is defeated by landing a typo fix first.
   Verify by API; the UI radio is easy to misread.
4. Restart the runner. Confirm its network isolation came back — it is a
   separate unit and does not return with the container.
5. **Exercise a real fork PR before announcing** (#592). Until that has happened
   once, the fork path is a hand trace, and it is the only step that tests the
   thing being defended.

## What protects the runner today, and why it disappears

On a **private** repo an org-level policy means fork PRs execute no workflows at
all. Verified by opening a real fork PR: zero check runs, an empty Checks tab.

That policy is private-repos-only. **It stops applying at exactly the moment
step 2 completes** — which is why step 1 is not optional and why the guard that
has never executed becomes the sole control.

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
