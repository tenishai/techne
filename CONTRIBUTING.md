# Contributing to techne

## Getting a change in

```bash
git clone https://github.com/tenishai/techne.git
cd techne
python3 -m venv .venv && source .venv/bin/activate   # Debian/Ubuntu: sudo apt update && sudo apt install python3-venv first
pip install -e ".[dev]"
pytest -q tests/test_x.py  # the tests for what you changed; CI runs the full suite
ruff check techne tests    # ADVISORY — see below
```

Then: branch, commit with `git commit -s` (see [Developer Certificate of
Origin](#developer-certificate-of-origin)), open a PR. Before pushing, run the tests
that cover what you changed, and show any new test fails without your fix. CI is the
full gate: it runs the whole suite on 3.12 for every PR push, and on 3.12 and 3.13
for every push to main, so a 3.13-only break shows up on main after merge. To run
3.13 on a PR branch, use `gh workflow run test.yml --ref <branch>`. Run the full suite
locally only when CI can't, for example when you are changing CI itself.
CI's `acceptance` job also builds the wheel and tests the
installed copy, which catches a module or package-data file missing from the package;
run `bash scripts/acceptance.sh` locally when you add either.
CI's `install-e2e` job follows the README's source and pipx installs from a fresh
`$HOME` and checks init, the hooks, MCP, store and recall, doctor and the vector
backfill. If you change the install path, `init` or doctor, run
`scripts/install-e2e.sh --path source` locally; the pipx path takes about 11 minutes.

To see how long CI jobs queue for the self-hosted runners, run `python3 scripts/ci_queue_wait.py` (read-only, needs an authenticated `gh`; see its docstring).

Everyone taking part in this project is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Report security problems privately, as
[SECURITY.md](SECURITY.md) describes, not in a public issue.

**ruff is advisory here, and a clean checkout is not clean.** `ruff check techne
tests` reported 158 errors on an untouched tree with ruff 0.16.0 (2026-09-30; the
count moves with the ruff version, which `ruff>=0.5.0` leaves unpinned), and CI runs
the identical command with `|| true`, so lint never fails the build. Run it to compare against
the lines you touched, not as a gate you have to reach zero on. If that surprises
you, it is the project's state and not your environment.

A few things worth knowing before you spend effort:

- **Tests live in `tests/`**, and a behaviour change wants a test that fails
  without it. "It is covered by an existing test" is worth
  checking rather than assuming — reverting your change and watching a test fail is
  the cheap way to know.
- **Architecture cards.** Load-bearing subsystems have a card in `docs/arch/`, each
  pinned to its source file by a content hash. Change such a subsystem and you update
  its card in the same commit, hash included. `python scripts/check_arch_cards.py`
  tells you; so does CI.
- **Comment-block gate.** `scripts/check_comment_blocks.sh` refuses a commit that
  adds a comment block longer than 12 lines (techne#998) — reasoning belongs in the
  issue and the PR, not in code that reads as current forever. It reads the staged
  diff, so run it after `git add`: `bash scripts/check_comment_blocks.sh`. Wire it as
  your own pre-commit hook if you want it to run automatically; CI runs it too, on
  what your PR adds relative to `origin/main`. Deliberate exception locally:
  `ALLOW_LONG_COMMENT=1 git commit ...` — **this has no CI counterpart.** A block
  committed with the escape passes locally and then turns `arch-cards` red on the
  PR; this repo has no branch protection, so that red does not block a merge, and
  a maintainer who judges the exception warranted merges anyway. If that becomes
  a recurring need, add a real CI-side escape rather than reaching for this one.
- **Schema changes have their own rules**, below. Read that section before touching
  anything that creates or alters a table — picking the wrong mechanism either leaves
  existing databases broken or burns a version bump that was not needed.
- **AGENTS.md** is the context file for AI coding agents, and `CLAUDE.md` is a
  one-line bridge to it. If you add a rule an agent must follow, it belongs there,
  not only here.

Issues are the place for design discussion. A PR that changes behaviour is easier to
review when it says what it measured, not only what it intends.

## On macOS, put the GNU tools first

The scripts here, and the commands in this repo's docs, are written for the GNU versions of `sed`, `awk`, `grep`, `find`, `xargs`, `tar`, `date`, `stat`, `readlink`, `env` and `diff`, as on Linux. The BSD versions that ship with macOS take different flags. A common case is `sed -i`: BSD needs a backup suffix after it, GNU doesn't. Some commands fail outright, and some print something else and carry on. Install the GNU tools and put them ahead of `/usr/bin` on your `PATH`.

With MacPorts:

```bash
sudo port install coreutils findutils gsed gawk grep gnutar diffutils
export PATH="/opt/local/libexec/gnubin:$PATH"    # in ~/.profile or ~/.zshrc
```

With Homebrew:

```bash
brew install coreutils findutils gnu-sed gawk grep gnu-tar diffutils
for f in coreutils findutils gnu-sed gawk grep gnu-tar; do
  PATH="$(brew --prefix)/opt/$f/libexec/gnubin:$PATH"
done
export PATH                                      # in ~/.profile or ~/.zshrc
```

Check: `sed --version` should print `sed (GNU sed)`. Re-open your shell, or source the profile, after changing `PATH`.

## Developer Certificate of Origin

techne uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) instead of a contributor license agreement. Every commit in a pull request
must carry a `Signed-off-by:` line with your name and email, and they must match
the commit's author. `git commit -s` adds it from your `user.name` and
`user.email`:

```
Signed-off-by: Jane Doe <jane@example.com>
```

By adding that line you certify the following, quoted from
<https://developercertificate.org/>:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

CI checks every non-merge commit between the pull request's base and head, and
fails naming each commit whose sign-off is missing or doesn't match its author.
To fix it:

```bash
# the last commit only
git commit --amend --signoff --no-edit

# every commit on your branch
git rebase --signoff origin/main

# sign-off present but under a different name or email than the author:
# set user.name / user.email to the identity you want, then
git commit --amend --reset-author --signoff --no-edit

git push --force-with-lease
```

Commits already on `main` from before this rule are not re-checked; the check
covers the commits a pull request adds. Commits authored by Dependabot are
exempt from the sign-off requirement — Dependabot signs off as `dependabot[bot]
<support@github.com>`, which never matches its noreply author email — but only
for same-repo dependency-update PRs; the check stays strict on PRs from forks.

A second exception: a pull request opened by someone with write access to
this repository (admin, maintain or write, as GitHub's repository-permission
API reports it) skips the check, including any commits it carries from others.
Every pull request from a fork is checked, whoever opened it.

## Using AI tools

AI-assisted contributions are welcome; we build this way ourselves. You are
responsible for every line you submit: understand it, test it, and be able to
explain it in review. Say in the pull request description which AI tools you
used. Crediting the agent with a `Co-Authored-By:` trailer is optional. Don't
submit code a tool reproduced from a source whose license isn't compatible with
the MIT license.

## Schema evolution

Two parallel mechanisms manage the SQLite schema. Pick the right one — using the wrong one either leaves old databases broken or burns a SCHEMA_VERSION bump on a change that didn't need one.

---

### Use `_migrate()` when

- The change requires a **data backfill** (e.g., populating a new column from an old one).
- The change is **structurally destructive** — DROP TABLE, DROP COLUMN, index rebuild that requires DROP + CREATE.
- You need future migration steps to be able to **assume a structural precondition** (e.g., "column X definitely exists") that a purely-additive reconciler pass can't guarantee.

Steps:

1. Bump `SCHEMA_VERSION` at the top of `techne/core/database.py`.
2. Add a `if from_version < N and to_version >= N:` block inside `_migrate()`.
3. Update `SCHEMA_SQL` to reflect the post-migration state (so fresh databases get the right definition from the start).

Real examples:
- **v2** — `outbox` status columns + backfill from `synced_at` encoding.
- **v4** — `sessions` summary split; existing `summary` left in place (provenance indeterminate).
- **v5** — `confidence_numeric` + `last_confirmed_at` with backfill from legacy enum.
- **v8** — `observations_fts` rebuild after schema change (DROP + CREATE is destructive).

---

### Use SCHEMA_SQL only (no SCHEMA_VERSION bump) when

The change is **purely additive**: new nullable column, new table, new index, new trigger. The reconciler (`_reconcile_tables` / `_reconcile_columns` / `_reconcile_triggers`) runs on every connect and diffs SCHEMA_SQL against the live DB, so the change lands automatically the next time any code opens a connection.

**Post-#140 caveat:** connect()-time reconcile is sentinel-gated and skips databases that have already been reconciled at the current schema version. An additive SCHEMA_SQL change at an **unchanged SCHEMA_VERSION** will NOT automatically reach already-reconciled databases — they have passed the gate and won't re-run the diff. To force re-application on an existing DB, run `techne reconcile` (re-stamps the sentinel) or bump SCHEMA_VERSION (which always triggers a full reconcile pass).

Steps:

1. Add the column / table / index to `SCHEMA_SQL`. That's it.
2. If deploying to existing DBs without a version bump, remind users to run `techne reconcile` once.

Real examples:
- `extension_state` table (issue #18) — new table, no migration needed.
- `event_at` column on `observations` (issue #8) — new nullable column.
- `summary_llm_source` column on `sessions` (issue #1) — new nullable column.

---

### What `schema_version` actually tracks

`schema_version` tracks **semantic compatibility**, not structural completeness. A database at version N can have any additive structure reconciled in without a version bump. That means two databases at the same `schema_version` can differ in which additive columns / tables they have — and that's intentional.

Consequence: if you write a migration step that reads a column introduced additively (i.e., never bumped SCHEMA_VERSION for it), call `_reconcile_columns()` before reading. The reconciler is idempotent and safe to call inside a migration block.

---

### Historical inconsistency — v7

v7 (observation `tags` / `kind` columns) was a purely additive `ALTER TABLE`; it did not require a backfill or a DROP. Under the convention above it should have gone through SCHEMA_SQL only. It was bumped anyway. Leave it — the migration is harmless, the column definitions are correct, and retrofitting it would rewrite history without benefit. Don't use v7 as a model for future additive changes.

---

### Decision tree

```
Does the change touch existing data (backfill, transform, rename)?
  Yes → _migrate() + bump SCHEMA_VERSION + update SCHEMA_SQL
  No  → Is the change destructive (DROP, index rebuild)?
          Yes → _migrate() + bump SCHEMA_VERSION + update SCHEMA_SQL
          No  → SCHEMA_SQL only (reconciler picks it up on next connect)
```
