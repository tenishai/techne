# Sessions and threads

A **session** is a container the client hands techne. A **thread** is a unit of *work*
techne derives from what happened inside one. They are not the same thing, and neither one
is a subdivision of the other in the way the schema first suggests.

The short version: **a session is where the work happened; a thread is what the work was
about.** One session usually holds several threads. One thread usually spans several
sessions.

## Why they have to be separate

A session boundary is an accident of tooling. It starts when you open a client and ends
when you close it, hit `/clear`, or the process dies. Nothing about that lines up with what
you were actually doing:

- **You change topic without changing session.** A morning of backup-retention work, then
  an unrelated docs fix, then a code review — one session, three unrelated pieces of work.
  Recall that keys on the session hands back all three when you asked about one.
- **You continue a topic across sessions.** The backup work resumes tomorrow in a new
  session, and the day after that. If a thread cannot outlive its session, a line of work is
  lost at every `/clear` — which is exactly the thing memory is supposed to prevent.

Measured on this machine's own store: **534 sessions produced 2,209 threads**, and **1,242
of those threads are linked to a parent thread in an earlier session.** Neither number is
close to one-per-session in either direction.

## How the split is made

Detection is **structural and deterministic** — no model call, no API key. techne scores
every consecutive pair of observations in a session and cuts where the score crosses a
threshold.

### The signals

Each signal contributes additively (`techne/core/threads/scoring.py`):

| signal | weight | what it notices |
|---|---|---|
| `gap_hard` (≥ 60 min) | 1.0 | on its own, enough to cut |
| `gap_medium` (≥ 30 min) | 0.6 | usually a break |
| `gap_soft` (≥ 10 min) | 0.3 | mild |
| `repo_change` | 0.5 | the work moved to a different repository |
| `zero_entity_overlap` | 0.3 | both sides name entities and share none |
| `zero_file_overlap` | 0.2 | both sides touch files and share none |
| `family_change` | 0.4 | the kind of tool changed (git → web, say) |
| `user_prompt` | 0.3 | you said something |

A pair is a boundary at **≥ 0.7**. That threshold is why no single weak signal cuts: a
10-minute pause alone (0.3) does not, a 10-minute pause that also crosses into a different
repo (0.3 + 0.5) does.

Two details in there are worth knowing, because both exist to stop a plausible-looking
mistake:

- **A user prompt never cuts on its own.** It is an *anchor*, not a boundary: 0.3 amplifies
  a real signal beside it so the cut lands cleanly on the prompt rather than on some tool
  call next to it. Mid-flow questions do not shred one piece of work into many.
- **`family_change` is suppressed when crossing into a prompt**, because a tool → prompt
  transition always changes family and would double-count with the prompt signal itself —
  pushing every prompt over the threshold.

Meta tools (searching for a tool, reading help) do not count as a family change. The
comparison walks to each side's nearest *non-meta* neighbour, so a `ToolSearch` sandwiched
between two related calls does not manufacture a boundary.

### The merge pass

Cutting on scores alone over-segments, so the segments are then merged until stable
(`techne/core/threads/segmentation.py`):

1. **Tiny segments** (fewer than 3 observations) merge into whichever neighbour shares more
   entities; ties merge forward, on the reasoning that a small run is usually setup for what
   follows.
2. **Meta-only segments** are preamble and merge into what comes after.
3. **Same-repo neighbours** merge unless a ≥ 60-minute gap separates them.
4. **Same tool family plus shared entities** merge across gaps of up to **3 hours** — the
   "long break in the middle of one investigation" case, which rule 3 misses when the work
   is over dashboards or services rather than code.

## When it runs

At **session end**, via `run_session_end` — the client's stop hook for Claude Code, and
`session.idle` for the opencode plugin. The hook skips sessions with fewer than **10**
observations; the underlying function does not, so a manual re-run always processes.

Nothing runs during the session. Threads are a *retrospective* reading of what happened, not
a running classification, which is why they can use signals like "how long was the gap
before the next thing" at all.

## How a thread outlives its session

`threads.parent_thread_id` chains a new thread to a semantically similar one from a recent
session, matched by full-text search over the label and summary, excluding same-session
candidates. That chain is what makes a line of work followable across days.

A thread's `id` is **content-addressed** for heuristic-detected threads — derived from the
session, the first observation, and the boundary signal — rather than `{session}-{num}`.
That matters because anything holding a foreign key to a thread (a knowledge candidate, a
reflection) must derive the id the same way; a mismatch here once made every candidate
insert silently fail its FK (#92).

## Thread lifecycle

Threads age on their own activity, not the session's:

| status | when |
|---|---|
| `active` | default on creation |
| `completed` | no activity for `threads.complete_after_hours` (default **48**) |
| `stale` | completed and untouched for `threads.stale_after_days` (default **30**) |

Completing only flips the status — `resume_points` is preserved and the thread stays
searchable. It leaves the *active working set*, it is not archived.

## Looking at it, and changing it

```sh
techne threads                          # recent threads across sessions
techne threads --active                 # only ones with unfinished resume points
techne threads --topic backup           # search by topic
techne threads show <session-id>        # the timeline WITH the per-pair score breakdown
techne threads redetect <session-id>    # re-run detection on a recorded session
```

`threads show` is the one to reach for when a split looks wrong: it prints each boundary's
signals and their sum, so you can see *which* signal did or did not fire rather than
guessing at the outcome.

Tuning lives under `threads` in `~/.techne/config.toml`:

| key | default | effect |
|---|---|---|
| `detector` | `heuristic` | `llm` opts into a model-based detector |
| `summarizer` | `heuristic` | same, for thread summaries |
| `tool_families` | — | map your own tool names into families |
| `entity_patterns` | — | extra regexes for entity extraction |
| `complete_after_hours` | 48 | active → completed |
| `stale_after_days` | 30 | completed → stale |

The defaults are deterministic and free. The LLM detector is available and is not required
for anything — including on a fresh install, which is the point of the heuristic existing.
