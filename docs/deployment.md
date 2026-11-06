# Deploying and upgrading techne

Running `techne watch` as a service, scheduled backups, and
[upgrading an existing install](#upgrading-an-existing-install).

`techne watch <dir>` is the long-running ingest daemon. It starts a techne
session, does an initial full walk of the watched tree, then uses
[watchdog](https://python-watchdog.readthedocs.io/) to observe filesystem
events in real time. On Linux, watchdog selects `InotifyObserver` — the
kernel's inotify subsystem — which allocates **one watch descriptor per
directory** for every directory in the watched tree.

---

## Running as a systemd user service

A ready-made unit template lives at `deploy/techne-watch.service` in the repo.

```
~/.config/systemd/user/techne-watch.service
```

**Quick setup:**

```bash
mkdir -p ~/.config/systemd/user
cp deploy/techne-watch.service ~/.config/systemd/user/techne-watch.service
```

Edit the three placeholder lines in the `[Service]` section:

| Placeholder | Replace with |
|---|---|
| `/ABSOLUTE/PATH/TO/.venv/bin/techne` | Absolute path to the `techne` binary in your virtualenv |
| `/PATH/TO/CORPUS` | Root directory (or directories) to watch |
| `TECHNE_HOME=%h/.techne` | Leave as-is for the default, or point to a custom state dir |

Then enable and start the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now techne-watch.service
journalctl --user -u techne-watch -f    # follow logs
```

The unit is `Type=simple` — `techne watch` blocks in the foreground on
`Observer.join()` and exits cleanly on `SIGTERM` (the default stop signal).
`Restart=on-failure` revives the daemon if it crashes; transient exits
(e.g., a corpus directory disappearing temporarily) are recovered
automatically.

---

## Scheduled backups (techne#742)

`techne backup` copies **every present domain db together** into one timestamped set, via
SQLite's online backup API — consistent even while something is writing — with a manifest
that `techne restore` validates before applying. Cross-db invariants span files, so always
back up and restore the **whole set**; a single-db backup desyncs pointers.

**`--keep` is not optional on a timer.** Every run writes a full set and nothing prunes by
default, so a nightly job without it fills the disk. That is why the units below all pass
it, and why the flag landed with this documentation rather than after it.

```bash
techne backup --keep 14      # one set per run, newest 14 retained
```

Retention is ordered by each set's **own manifest timestamp**, not by mtime — reading or
copying a backup directory rewrites mtimes, and ordering on them would evict a genuine
recent backup because something touched it. Anything under `backups/` without a manifest
is left alone, including the migration `.db.bak` files that live there under their own
retention.

> **Agent: raise this unprompted.**
>
> - They are writing a cron job, timer, launchd plist or CI step that calls
>   `techne backup` **without `--keep`** — say it will grow without bound. This is the
>   single most likely way to turn a backup story into an outage.
> - They ask "do I have backups?" — check whether anything actually *schedules* one.
>   Having the command is not having backups, and `techne doctor` does not check this.
> - They are planning a **machine migration, a wipe, or a disk replacement** and have not
>   mentioned transcripts — nothing archives those. (Keyed on the act, not on the word
>   "transcript": a standing property mentioned every time it comes up trains the operator
>   to skip the block.)
> - They are restoring — remind them it is all-or-nothing across domains; a single-db
>   restore desyncs cross-db pointers.

### Linux — systemd user timer

Ship both `deploy/techne-backup.timer` and `deploy/techne-backup.service`:

```bash
cp deploy/techne-backup.{timer,service} ~/.config/systemd/user/
# edit ExecStart in the .service: absolute path to your techne binary
systemctl --user daemon-reload
systemctl --user enable --now techne-backup.timer
loginctl enable-linger $USER          # REQUIRED — see below
systemctl --user list-timers techne-backup
```

**`enable-linger` is not optional and the verification step will not tell you it is
missing.** Without it the `systemd --user` manager stops at last logout, so on a headless
box — or any machine you do not stay logged into — the timer never fires. `list-timers`
shows it perfectly healthy *while you are still logged in*.

`Persistent=true` catches up after downtime rather than silently skipping a night, and
`RandomizedDelaySec` keeps a fleet from stampeding.

### Exit codes, because a timer only ever sees this one

`systemctl --user status` and `launchctl list` surface **exit status and nothing else** —
whatever the run said on stderr went to the journal or `backup.log`, which nobody reads
until something is already wrong. So retention failure has its own code rather than being
folded into either success or failure:

| code | meaning |
|---|---|
| `0` | backup written, retention completed |
| `3` | **backup written, one or more sets were not reclaimed** — see below |
| `4` | **the run captured NOTHING** — no domain database was present to back up |
| `1` | the backup itself failed |
| `2` | usage error (e.g. `--keep -1`), before any work happens |

`4` takes precedence over `3`: both can be true, and an empty backup is the more serious.

**`4` almost always means the job is pointed at the wrong store.** A typo in `TECHNE_HOME`,
a unit installed before `techne init` ran, or a service running as a user whose `~/.techne`
is not the one you meant. The message names the directory it looked in — check that first.
A backup of zero databases is not a backup, and at exit `0` it was indistinguishable from a
working one on the only channel a timer has, which is a green light and no backups until
the day you need to restore.

`3` is not a failed backup and should not page anyone as one.

#### What `3` actually means, because the causes differ and they want different fixes

The run names every set it could not reclaim and why. **A stranded set is still a valid,
fully restorable backup** — `restore` does not inspect `created_at`, so nothing here has
corrupted anything. What you are looking at is a set retention will never remove on its own.

| the message says | what happened | what to do |
|---|---|---|
| `[Errno N] <anything else>` | the delete itself failed, and the OS said why | act on **that** errno — it is reported verbatim. Permissions (`EACCES`) is the common one, but a read-only filesystem (`EROFS`), a file still open (`EBUSY`), or a full disk are all reachable and want different fixes |
| `manifest is unreadable (...)` | techne cannot read the set's own manifest, so it will not judge it | fix the permissions on that `manifest.json` |
| `created_at ... is not YYYY-MM-DDTHH:MM:SSZ` | the stamp is not in the one format that sorts chronologically as text, so the set cannot be ordered | see below |
| `created_at ... is in the future` | the set cannot be ordered against real time — usually a clock that was ahead when it was written | see below |
| `is a symlink` | something under `backups/` is a symlink to a set | remove the symlink; techne will not follow it or delete its target |
| `has no manifest` | a directory named like a backup set, carrying no manifest | **identify it before touching it** — see below |
| `manifest does not parse` | a set whose `manifest.json` is corrupt | same — treat it as unidentified, not as a set |
| `lists a database file that is not there` | the manifest names a database the directory does not contain — **or names something that is not a file**, such as `"."` or `"./"`, which do not name a file | `restore` refuses this set, so techne does not count it as a backup and will not prune it. Check the manifest's `file` values first: if one names a directory, the set was written by something other than techne, or edited by hand. If they look right, something outside techne removed the file — check for a partial copy or a half-finished sync. Move it out of `backups/` once you have salvaged what you want |
| `lists databases in a shape techne cannot read` | the manifest says it is a techne backup, but its `databases` entries are malformed — **or an entry's `file` is empty or whitespace** (such as `""` or `"  "`), **resolves outside the backup set** (an absolute path or a `../` traversal), **or contains a NUL byte** (written as `\u0000` in valid JSON) | Check the manifest's `file` values first: an empty value, an absolute path, or a `../` traversal are all fixable by editing that one string. A NUL byte reaches this check as the literal `\u0000` escape in the manifest text — search the file for it rather than eyeballing the field, since it does not stand out among ordinary path characters. If a database is also absent, the same line says so after this phrase (`; and it lists a database file that is not there (…)`); when that second clause is missing, every other listed file is present. Only if the entries themselves are malformed (not a `file` problem) is there nothing to read — keep that set if you may want to salvage the `.db` files inside by hand, and move it out of `backups/` to stop the alarm |

The first row is deliberately not a list of specific errnos. Retention reports whatever the
OS returned, and naming one of them here would send an operator to check permissions on a
full disk.

**On the two "cannot identify it" rows.** techne reports these and will never delete them,
because it cannot tell three situations apart from a directory name:

- a backup **running right now** — this clears itself when the run finishes;
- a backup that **died** (`SIGKILL`, OOM, power cut — an in-process `^C` cleans up after
  itself, so it never gets this far);
- a **foreign directory** that happens to end in a timestamp. `photos-20240101-000000` and
  `export-20260101-120000` both match the shape techne writes.

So do not delete on the strength of this message. To tell them apart: check whether a backup
is in progress; check the directory's age against your schedule; and look inside — one of
ours holds `*.db` files and nothing else. If it is genuinely an abandoned set of ours, it is
an **incomplete copy and not a restorable backup**, and removing it is safe. If you cannot
establish that, move it out of `backups/` rather than deleting it.

**The two stamp cases are the ones with no in-tool remedy, deliberately.** Retention only
ever deletes what it can *order*, because the failure mode of guessing is a deleted backup.
There is no `--prune-unorderable` flag and there should not be: it could only ever delete a
restorable backup, which `mv` does better and reversibly. Move the set out of `backups/` if
you want the alarm to stop, or restore from it and then move it.

A future stamp usually means the clock was ahead when that set was written and has since
been corrected — `Persistent=true` fires a catch-up run right after boot, which is exactly
the window before NTP has synced.

Once a set is stranded, **every subsequent run exits `3` until you move it.** That is
intended: the condition is permanent until someone acts, and it names its set and its
remedy each time rather than repeating an unactionable warning.

**A failed run does not stop the timer.** `systemctl --user list-timers` still shows the
next run, and the next run still writes a backup. An operator who sees `failed (Result:
exit-code, status=3)` and concludes backups have stopped is reaching for the wrong fix at
the worst possible moment: the backups are fine, the housekeeping is not.

Leave the units alone on this. Neither sets `Restart=` or `KeepAlive`, so exit 3 cannot
cause a respawn — and `SuccessExitStatus=3` would be actively wrong, because it records the
run as successful and destroys the signal.

### macOS — launchd agent

```bash
cp deploy/ai.tenish.techne-backup.plist ~/Library/LaunchAgents/
# edit the techne path and the log paths
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.tenish.techne-backup.plist
launchctl list | grep techne-backup
```

`launchctl load` still works and is what older guides show, but it is deprecated; the
plist header says `bootstrap` and these two should not disagree.

`RunAtLoad` is deliberately false — loading an agent should not fire a backup at a moment
nobody chose. If the machine is asleep at the scheduled time, launchd runs it once on wake.

### What is NOT automated, stated plainly

- **Transcript archiving.** There is no techne primitive for it. Session transcripts are
  the durable history behind several recovery paths — `techne import` backfills
  observations from them, and they have settled questions no table could — but archiving
  them is a manual `tar` today. If you rely on them, put that on your own schedule and
  know that techne is not doing it for you.
- **The pre-restore snapshot is not a keepsake.** `techne restore` snapshots the current
  state first and prints `prior state snapshotted at: <path>`, which reads as durable. It
  is an ordinary set under `backups/` — deliberately, so retention can reclaim it — so the
  next `--keep N` run counts it as one of the N and it ages out like any other. If you
  want it kept, move it out of `backups/` yourself.

---

## Upgrading an existing install

An upgrade replaces the code. It does **not** touch `~/.claude/settings.json` or
`~/.techne/config.yaml`, and it does not embed anything; `techne init` does all three.

**Run `techne init` before you change any config key (0.18+).** Up to 0.17.0, `techne
init` wrote every key into `config.yaml`, so each default was pinned at the value it had
then. From 0.18 the file holds only the keys you set. On load, an old file (no
`config_version`) is migrated in memory: every key whose value equals a default that key
has ever shipped with is dropped, so it follows today's default. A value no release
shipped, and any key techne does not recognise, is kept as your choice. The next `techne
init` rewrites the file in the sparse form, first copying it to
`config.yaml.bak-<timestamp>`, and prints one line naming the keys that changed value.
Until that rewrite, every load migrates the file again, so a value you type that was once
a default (`per_turn_recall: false`, `capture_bash: false`) is un-pinned again and does
not take effect; `techne doctor` and the session-start banner name every such key. After
init, set the keys you want and they stay pinned. The one case the migration cannot tell
apart is a key you set by hand to a value that was also once its default: check the line
init prints, and set it again after init if you meant it. The CHANGELOG's Upgrading
section lists which defaults moved.

**`techne init` downloads the embedding model and embeds what is stored (0.18+, #1021).**
The first run after an upgrade can make a network fetch (tens of MB, then cached) and
take a while on a large store. Pass `--no-vectors` to skip it; search then stays
keyword-only until you run `techne vectors backfill`. A failed download prints how to
finish later and does not fail init.

Three more consequences that neither the upgrade output nor `techne doctor` will tell you
about:

**A store with patterns tagged outside `techne` needs one line of config (0.18+).**
`architect.pattern_targets` replaced a hardcoded roster in #1008, and it defaults to
`["techne"]` alone. Tagging silently drops a target that is not configured, so run
`techne architect targets` after upgrading: it compares the config with what the store
holds and prints the line to paste. Nothing already stored is lost, and
`techne architect backfill` recovers any tags missed in between.

Patterns written before a target was configured stay untagged for it: tags come only from a
"where to apply in <target>" phrase in the body. Tag them by hand with `techne architect tag
<id> --applies <target>`, which `backfill` never undoes (#1293). If bodies name a project by
another word, such as a persona that lives in another repo, map it with
`architect.target_aliases: {prudence: agents}`.

`techne architect search` treats a renamed project's old and new names as one word, so a
search for `liminal` also finds bodies written before the rename that say `limenal` (#1345).
Add your own renames with `architect.search_aliases: {old-name: new-name}`. It is kept apart
from `target_aliases` on purpose: a persona alias like `prudence: agents` would widen every
search for the common word "agents".

**`techne init` owns the hook chain, and only `init` rewrites it.** `register_hooks` strips
every hook group that runs one of techne's own hook commands (`-m techne.hooks.*`,
`techne-hook.sh`, the preflight guard) and re-appends its own template; a hook of your
own stays, even under a path that contains "techne" (#1250), so the
SessionStart chain an install is running is the one written the last time `init` ran. An
install that predates a change to that template keeps the old chain indefinitely, looking
healthy the whole time — `doctor`'s hook-drift check compares `(event, matcher)` and never
the command.

**A store that has never been embedded stays that way until something fetches the
model.** Nothing embeds on write. The SessionStart chain runs `techne vectors backfill
--no-download`, which is correct for a hook — it degrades to "swept, embedded nothing"
rather than blocking session start on a model fetch — but it means a cold model cache
never warms itself. `techne init` (unless given `--no-vectors`) and a bare `techne vectors
backfill` are the two things that download it.

So, after upgrading:

```sh
techne init             # re-writes the hook chain, including the backfill segment;
                        #   rewrites an old config.yaml to the sparse form; downloads the
                        #   model and embeds the backlog; refreshes the bundled skills
techne skills install   # for anyone who skipped init — otherwise a no-op
techne agents install   # likewise, and it is a SEPARATE command: the research sandbox
                        #   rides the same --no-skills flag but `skills install` does not
                        #   install it. Check it with `techne agents status`.
```

Then, only if init printed a download or embedding failure, or you passed `--no-vectors`:

```sh
techne vectors backfill  # bare — warms the model cache and catches up the backlog
```

`init` re-resolves `--posture` from its `auto` default every run, so an operator who chose
`observe` or `spool-primary` should pass it again — it is machine-wide, and `init` reports
the resolved state rather than announcing a change.

`techne init` reports success as `Semantic recall ready: embedded N stored row(s).`
`techne vectors backfill` embedded rows only if it printed a JSON object with counts; a
JSON object holding only `tags` (the pending tags it inferred) means it embedded nothing,
and some paths print a plain line and no JSON at all. Check stderr and the exit code too.
On a Python whose `sqlite3` can't load extensions it exits 1 and says so on stderr
(#1321); reinstall techne with a uv-managed, distro or Homebrew Python. A `vector search
unavailable: no embedding backend installed` line means it embedded nothing and exited 0 —
and that line appears for two different causes: the `vectors` extra absent, or the extra
present but the embedding backend unable to load. `cli.py` deliberately avoids naming
sqlite-vec in the second case, because blaming it sends an operator to re-check what they
have already ruled out — so diagnose from whether you got counts, not from the cause.

If you did not get counts: `techne vectors status` reports whether a backend resolved, and
re-running `techne vectors backfill` somewhere the model cache can be fetched fixes the
cold-cache case — the bare command downloads by default (`allow_download=not no_download`),
and only the SessionStart hook passes `--no-download`.

> **Agent: raise this unprompted.**
>
> - They have just upgraded techne, or are about to, and have not mentioned `techne init`
>   — the hook chain is only rewritten by `init`, so an upgrade alone leaves whatever
>   chain was written last. Say so before they conclude the new version did nothing.
> - They report that recall is empty, that semantic search finds nothing, or that
>   `vectors:coverage` will not go green after an upgrade — check whether `techne init`
>   (without `--no-vectors`) or `techne vectors backfill` has ever run from the CLI with
>   network access. The SessionStart hook runs backfill with `--no-download`, so on a
>   cold model cache it embeds nothing, every session, silently.

## inotify watch-limit on Linux

### Symptom

On Linux, inotify allocates one watch descriptor per **directory** (not per
file). The kernel default for `fs.inotify.max_user_watches` is **8 192** on
many distributions. When `techne watch` is pointed at a large tree — a
monorepo, a project with `node_modules`, a docs corpus — the limit can be
exhausted. `techne watch` does not carry on half-watching: when watchdog
reports the limit (`ENOSPC` for `fs.inotify.max_user_watches`, `EMFILE` for
`fs.inotify.max_user_instances`), it stops with a message naming the sysctl
and a value to raise it to, and exits 78 (EX_CONFIG), so the shipped unit
stops instead of respawning (#529). Raising the limit, or excluding
directories, is the fix; restarting is not.

### How to raise the limit

Check the current value:

```bash
cat /proc/sys/fs/inotify/max_user_watches
```

Raise it for the running kernel (takes effect immediately, resets on reboot):

```bash
sudo sysctl fs.inotify.max_user_watches=524288
```

Persist the change across reboots:

```bash
echo 'fs.inotify.max_user_watches=524288' | sudo tee /etc/sysctl.d/90-inotify.conf
sudo sysctl --system
```

524 288 is a widely used value; 8 192 × 64. For extremely large monorepos
(millions of files, tens of thousands of directories) you may need more.
Each watch descriptor uses roughly 1 KB of kernel memory, so 524 288 watches
≈ 512 MB — well within the budget of any modern server.

### Reducing the watch count with exclude_globs

The most effective way to stay within the default limit is to exclude
directories that generate noise but don't need tracking. There is no
config.yaml key for watch excludes; pass them on the command line with
`--exclude`:

```sh
techne watch /corpus \
    --exclude '**/.git/**' \
    --exclude '**/node_modules/**' \
    --exclude '**/__pycache__/**' \
    --exclude '**/.venv/**'
```

Or in the systemd unit:

```ini
ExecStart=/home/you/.venv/bin/techne watch /home/you/corpus \
    --exclude '**/.git/**' \
    --exclude '**/node_modules/**'
```

Excluding `node_modules` alone typically reduces the directory count by an
order of magnitude in JavaScript projects.

### macOS note

macOS uses `FSEvents`, which has no equivalent per-directory watch limit.
The inotify cap is Linux-only and will not appear in development on macOS.
