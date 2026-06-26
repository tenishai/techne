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
