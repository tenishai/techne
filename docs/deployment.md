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

### macOS — launchd agent

```bash
cp deploy/ai.tenish.techne-backup.plist ~/Library/LaunchAgents/
# edit the techne path and the log paths
launchctl load ~/Library/LaunchAgents/ai.tenish.techne-backup.plist
launchctl list | grep techne-backup
```

`RunAtLoad` is deliberately false — loading an agent should not fire a backup at a moment
nobody chose. If the machine is asleep at the scheduled time, launchd runs it once on wake.

### What is NOT automated, stated plainly

- **Transcript archiving.** There is no techne primitive for it. Session transcripts are
  the durable history behind several recovery paths — `techne import` backfills
  observations from them, and they have settled questions no table could — but archiving
  them is a manual `tar` today. If you rely on them, put that on your own schedule and
  know that techne is not doing it for you.
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
