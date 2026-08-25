# Deploying techne watch on Linux

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
- **The author's fleet sweep.** `~/.techne/db_snapshot.py` discovers sqlite dbs across
  arbitrary roots and prunes per identity. It is host-local and **does not ship**
  (techne#742). `techne backup` covers the four domain dbs, which is what a single-machine
  operator needs; the sweep exists for a multi-repo fleet and is not part of the package.

---

## inotify watch-limit on Linux

### Symptom

On Linux, inotify allocates one watch descriptor per **directory** (not per
file). The kernel default for `fs.inotify.max_user_watches` is **8 192** on
many distributions. When `techne watch` is pointed at a large tree — a
monorepo, a project with `node_modules`, a docs corpus — the limit can be
exhausted silently: inotify stops issuing events for directories beyond the
cap, and file changes in those directories are not ingested until the next
periodic resync walk (default: every 24 hours).

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
directories that generate noise but don't need tracking. In
`~/.techne/config.yaml`, add watch-level excludes (or pass them on the
command line with `--exclude`):

```yaml
# No config.yaml key for watch excludes yet — pass them at invocation time:
#
#   techne watch /corpus \
#       --exclude '**/.git/**' \
#       --exclude '**/node_modules/**' \
#       --exclude '**/__pycache__/**' \
#       --exclude '**/.venv/**'
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
