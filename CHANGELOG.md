# Changelog

All notable changes to techne are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses semantic versioning.

## [Unreleased]

### Added

- **Pluggable recall sources and an operator-defined recall order** (#1170). An
  installed package can add read-only sources to `techne_recall` through the
  `techne.recall_sources` entry-point group. They are refused by default: allow one
  by entry-point name in `recall.allowed_sources`, or set
  `recall.allow_unlisted_sources: true`. Both keys are governed by the operator layer.
  `recall.order` lists the sources to consult, built-in and plugged in, in the order
  their results come back, for example `memory`, `librarian`, `archivist:local`,
  `archivist:remote`, then a remote librarian. Marking an entry `fallback: true`
  consults it only when the sources before it returned fewer than `recall.fill` rows.
  Each plugged-in source call is bounded by `recall.source_timeout_s`, after a
  `health()` probe, and a source that is down reports `unavailable` while recall
  carries on. Plugged-in rows are stamped `origin`, `recall_source` and
  `external: true`, and credential-redacted. `techne doctor` adds `recall:sources`.
  With no `recall.order` set and no source allowed, recall's output is unchanged.
- **Outbox sinks from installed packages** (#1172). A package can contribute sinks
  through the `techne.outbox_sinks` entry-point group: each entry point is a zero-arg
  callable returning an `OutboxSink` or a list of them. They are refused by default,
  like sync adapters: allow one by entry-point name in `sync.allowed_sinks`, or set
  `sync.allow_unlisted_sinks: true` (only a real `true` counts). Every techne site that
  drains or prunes sink rows registers them (`outbox flush-sinks`, `prune-orphans`, the
  session-end hook), as do the lifecycle's enqueue sites, so their rows no longer wait
  for a process that happened to import the package. Bare `outbox prune-orphans` refuses to run while an
  entry point is refused or failed, because it would mark that sink's pending rows
  dead. `--target sink:<name>` prunes only targets you name as dead.
  `techne sinks list` and `sinks describe` now work in a fresh process, and `list` also
  names refused and failed entry points. `techne doctor` adds `sinks:entry-points`, which
  names pending sink rows that have no sink.
- **Recall reads back from the team sync backend** (#1171). `SyncAdapter.pull` had no
  caller. A sync adapter that declares `supports_pull = True` now joins recall as the
  source `team`, under the same order, fallback and timeout rules: the adapter is
  constructed inside the bounded call, its rows are stamped `origin: team`, a backend
  that is down degrades to the other sources, and the failure is logged once per
  process. `team` is not governed by `recall.allowed_sources`; the adapter was already
  admitted through `sync.adapter`. `noop` and `filesystem` don't opt in, so nothing
  changes for them. A new `adapter_class()` returns a sync adapter's class behind the
  same gate as `load_adapter`.

### Changed

- **Copyright notice names tenish and the techne contributors** (NOTICE, README). Contributors
  keep their copyright under the DCO.

### Fixed

- **`techne reflect emit` with no session id reaches every pending session, not just
  the newest five** (#1158, found by Grip). It took its target from
  `pending_reflection`'s window of 5. Once those five were emitted it said "no
  session with unreflected work" while older sessions were still owed, and a bulk
  drain that guarded on a wider window looped forever. On the operator's store the
  bare emit could see 4 of 70 pending sessions. The same fix applies to the API and
  to `techne_reflect_emit` without a `session_id`. The SessionStart nudge and
  per-turn recall keep their newest-five window.
- **`techne init` no longer dies with a traceback on macOS when a parent of the
  working directory is unreadable** (#1153). macOS's `getcwd()` fails with EACCES
  there; Linux's does not. `init` now treats the directory as "no project": it skips
  curated-memory detection, so `auto` posture resolves to observe, and says why.
  `memory doctor` and `memory sync` exit 1 with the same explanation. Run them from
  your project directory.
- **Explicit search shows why a lexically matched observation row is on the page**
  (#1147). Observation rows are cut to their first 500 chars, so a row FTS matched on
  a term past that cut showed nothing the query asked for: 4 of 16 such rows on the
  gold-v2 queries. 0.18.0 fixed this for the phrase-seated row only. Now, when an
  observation row FTS matched on a query term outside the stopwords has that term
  past the cut, the row keeps its first line (the tool and command, up to a quarter of
  the width), then ` … `, then a window over the first matched term, at the same
  width, with
  `content_head`, `content_window` and `content_chars_total` saying what is shown. The
  phrase-seated row takes the same shape, replacing the 0.18.0 `…`-prefixed window.
  On the 2026-09-28 snapshot every lexical row now shows a query term (16/16, 25/25
  on 40 real prompts, 48/48 on the held-out phrase queries), and no page's order
  changed. Explicit search only (`techne_search`, `techne search`, `techne_recall`;
  `techne search` prints 200 chars of each row, and for a windowed row that is up to
  60 chars of the first line, then ` … `, then the window from 40 chars before the
  match, so the match shows there too (#1162));
  the per-turn and inbound recall rails search knowledge rows, which are never
  windowed, so injected text is unchanged.

## [0.18.0] — 2026-09-30

### Deprecated

- **`techne_recall`'s `_store` / `_role` row keys are deprecated; read `store` / `role`**
  (#1102). They are public federation provenance (the connection a row came from and
  its role) but were named like the internal keys the MCP boundary strips. Recall now
  emits `store` / `role` with the same values alongside them, on every row that has
  them. Nothing is removed or renamed in this release; the underscore names will be
  removed in a future release. Internal readers (`tiers.tier_of`, the freshness filter)
  are unchanged.

### Docs

- **The README's apt lines run on a fresh machine** (#1154). Each `sudo apt install`
  (`python3-venv`, `pipx`) is now preceded by `sudo apt update`, and so is the
  CONTRIBUTING hint. A fresh Ubuntu container or VM has empty package lists, and the
  install failed with "no installation candidate". Found by the 2026-10-02 clean-machine
  quickstart round.
- **Open-source community files for the go-public release** (#1130).
  `CODE_OF_CONDUCT.md` is the Contributor Covenant 3.0, verbatim except for the
  reporting line (security@tenish.ai) and one adopter's note removed. `SUPPORT.md` says
  where questions, bugs and security reports go. `CONTRIBUTING.md` gains a
  "Developer Certificate of Origin" section (sign off with `git commit -s`, the DCO 1.1
  text, how to fix a missing sign-off) and a "Using AI tools" section (AI help welcome,
  disclosure in the PR description required, `Co-Authored-By:` trailer optional). The
  README gains "How this was built" and a short "Contributing" section linking both
  files, SUPPORT.md and SECURITY.md. Added `.github/ISSUE_TEMPLATE/` (bug report, feature
  request, question, and a `config.yml` that links SECURITY.md and SUPPORT.md and turns
  off blank issues) and `.github/pull_request_template.md`. The copyright holder is now
  "Richard DiMartino and the techne contributors" in NOTICE and the README, and
  `pyproject.toml`'s `authors` is "Richard DiMartino" (was "tenishai").
- **README quickstart named the missing system packages and a PATH gap on stock
  Debian/Ubuntu** (#1121, #1122). A fresh `ubuntu:24.04` with only `python3` and `git`
  installed — the README's stated requirements — failed both install paths: the dev
  path's `python3 -m venv .venv` needs `python3-venv` (`sudo apt install python3-venv`),
  and the pinned-release path's `pipx` needs `sudo apt install pipx` (macOS:
  `brew install pipx`). Separately, `pipx install ... && techne init` failed with
  `techne: command not found` — `pipx` puts the binary in `~/.local/bin`, which isn't on
  PATH until `pipx ensurepath` runs and a new shell starts; the README now says so
  between the two commands. Found by a literal quickstart run on stock Ubuntu 24.04.
- **Two lines stopped saying every hook config change needs a new session** (#1032).
  `docs/welcome.md` and `AGENTS.md` both said "a config change doesn't fire until the
  next session" for any hook flag. Only `per_turn_recall` and `codex_grep_assist` are
  true of that — they decide which hooks are registered in Claude Code's
  `settings.json`, loaded at session start. `capture_bash`, `codex_edit_rail` and
  `codex_reindex_on_edit` are read inside PostToolUse's handler on every call once
  that hook is registered (default on via `hooks.post_tool_use`), so they already
  applied immediately; `docs/operator-guide.md` (new in this release, #1019) and the
  README already said so.
- **`docs/investigating.md` gets two filter-discipline rules** (#926, #927), from a
  2026-09-13 re-audit that found four misses in eight repos, all from the same
  failure: a real technique discarded along with a product it didn't resemble. A
  candidate can no longer be dropped without first naming what its technique does
  independent of the product it sits in ("not a reviewer", "contradicts our scope" are
  not reasons on their own); and a `skip-with-flag` reason must cite either a
  `[[wikilink]]` to the covering pattern, a `file:line` actually read, or an explicit
  availability/feasibility fact — a 111-row audit found every confirmed miss came from
  reasons citing none of these.
- **`deployment.md`'s phrase-6 row now names all four of its causes** (#786). `lists
  databases in a shape techne cannot read` picked up an escaping `file` path and a NUL
  byte as causes when #782/#784 landed, on top of the malformed-entry and
  empty/whitespace cases already documented. The remedy now says a NUL byte reaches the
  check as the literal `\u0000` escape in the manifest text, so search for it rather
  than eyeballing the field.
- **`integrations/` vs `techne/platforms/` — the boundary is now written down**
  (#760). `techne/platforms/` is techne's own code for adapting to a client, imported
  and shipped in the wheel; `integrations/` is a foreign client's own code (TypeScript
  today, for opencode and pi) that shells out to techne's CLI and never ships. Added
  `integrations/README.md` and a line in `ARCHITECTURE.md`'s `ENTRY:` block.
- **New bundled guide: `docs/operator-guide.md`** (#1019, PR #1031). Read it with
  `techne guide operator-guide`. It covers the recommended hook configuration (what is
  on by default, what each hook costs, how to turn it off, and what stays opt-in) and
  the operating habits that came from running techne day to day: the limit of the
  per-turn recall hook (it matches only what the operator types), telling a knowledge
  row from a GitHub issue in references, the model-pin trap, reading prompting guidance
  from source, and the `CLAUDE.md` `@import` that fails silently. `AGENTS.md` carries
  the same five habits in short form. The README's `threads.link_threshold` example
  said 0.85; the default is 0.05.
- **`SECURITY.md`** (#1012, PR #1036). Report a vulnerability privately to
  security@tenish.ai rather than in a public issue. It states the response times
  (acknowledged within 14 days, fixed in the next release), that only the latest
  release gets fixes while techne is pre-1.0, and what is in scope: memory isolation,
  redaction, egress, the hooks and MCP server, and the shell guard, each with the
  behaviour that is by design.
- **`CONTRIBUTING.md` now explains how to contribute** (#1006, PR #1011). It held only
  the schema-migration rules. It now also covers clone, install, `pytest`, `ruff`,
  branch and PR, and that tests live beside the code and architecture cards are
  re-stamped in the same commit as the code they describe. The README gained a
  copyright notice, `pyproject.toml`'s `authors` was updated to match, and an empty
  stray file under `docs/arch/` was removed (#1009).
- **README "Who's who"** (#1010, PR #1027). A short glossary for the people and
  automated reviewers whose names appear in citations across the docs and this
  changelog.

### Added

- **CI checks DCO sign-off on every pull request** (#1130). A `DCO sign-off check`
  step fails when a non-merge commit between the PR's base and head has no
  `Signed-off-by:` line matching its author, and names each such commit. The line may
  sit anywhere in the message, so a hook that appends its own trailer paragraph after
  `git commit -s` does not break it. It runs in `arch-cards` for in-repo PRs and in
  `fork-pr` for fork PRs, as inline shell that reads commit metadata with replace refs
  off. In both jobs it runs before any step that installs, imports or executes the PR's
  tree, so PR code in the same job cannot run before it or change what it sees. A fork
  PR runs the fork's copy of the workflow, so this does not stop a fork that edits the
  step itself; that limit is the same one the trust-split comment in `test.yml` records.
  Commits already on `main` predate the rule and are not checked.
- **`techne init --allow-worktree`** (#436). Registers even when techne runs from a git
  worktree (a bare repo's worktree may be your only checkout), with a stderr warning
  naming the worktree and what breaks when it is pruned. A flag only; there is no env
  var form.
- **The transparent-prefix table is executed against the real utilities** (#740).
  `tests/test_transparent_prefix_oracle.py` runs 30 spellings of `timeout`, `stdbuf`,
  `ionice`, `setsid`, `nice` and `nohup` and checks both that the utility execs the
  wrapped command and that `_skip_transparent_prefixes` lands on it. Missing utilities
  skip, so the Linux CI runner is where the GNU-only rows run.
- `techne knowledge supersede OLD_ID CONTENT` writes a new revision and retires the
  old entry; `techne knowledge supersede OLD_ID --by ID` retires it into an entry that
  already exists, without writing a row (#877, #887). Until now the only route was the
  Python API, so an agent that spotted a stale entry could only write "SUPERSEDES #N"
  in prose, which recall never reads. Both forms keep the old row and mark its
  dependents uncertain.
- `techne_store` takes `supersedes: [ids]`: the named entries are retired into the new
  one in the same write, and a bad id stores nothing (#887).
- `techne doctor` lists live entries whose opening declares ("SUPERSEDES #N",
  "CORRECTS #N", "CORRECTION to #N") that they replace an older live entry, each with
  the `supersede --by` command that records it (#887). Advisory: a `#N` in prose can
  name an issue rather than a knowledge row, so it does not change the exit code, and
  doctor writes nothing.

- `techne init` and `techne skills install` now install `investigate-url` and
  `survey-field` as thin stub skills (`~/.claude/skills/<name>/SKILL.md`, #1040).
  Each stub holds only an injected `techne guide investigating|surveying` line, so
  Claude Code loads the installed guide when the skill fires and there is no copy of
  the procedure to drift — the failure #988 had to reconcile by hand. The stub's
  description is read from the guide's own recommended frontmatter. Stubs follow the
  procedures' ownership rule (an edited stub is reported as `kept-user-copy`, never
  replaced without `--force`), keep their own ownership record, and are removed by
  `techne skills remove` only when unedited. `skills install` and `skills status` also
  report a legacy `commands/<name>.md` or an enabled plugin answering to the same name,
  without touching it.
- `techne capabilities` (and the `techne_capabilities` MCP tool) now reports three
  gaps that no hook flag shows, by probing the machine (#1022): no embedding backend
  installed (`vectors_backend`), backend installed but its model not downloaded
  (`vectors_model`), and Claude Code auto-memory running beside techne
  (`single_memory_layer`, skipped under `spool-primary`). Each carries what it buys, what it
  costs, and the exact command or setting that fixes it, and counts toward the session-start
  "N capabilities are off" line.
- `techne capabilities ack <flag> [--reason ...]` and `unack <flag>` record a deliberate
  opt-out (the MCP tool takes `acknowledge=` / `unacknowledge=`). An acknowledged item
  stays in the report, marked as the operator's choice, and stops counting toward the
  session-start line. Acknowledgements do not expire, and a flag that is on
  cannot be acknowledged.
- `AGENTS.md` tells agents to call `techne_capabilities` when that line appears or on
  first use, explain each off item, offer to apply it, and never apply one silently.
- Inbound recall, off by default (#1018 parts 2-4). Per-turn recall only runs on a
  prompt, so a subagent hand-back, a fetched page or a peer's coord message that
  arrives mid-turn got no recall. With `hooks.inbound_recall: true`, PostToolUse runs
  the same recall pipeline on the results of the tools in `hooks.inbound_recall_tools`
  (default: Agent, Task, WebFetch, `techne_inbox`, `techne_coord_threads`) and on the
  coord message bodies it is about to inject. It injects at most
  `hooks.inbound_recall_limit` rows (2), at most once per user prompt, logs as
  `source="inbound_recall"`, and shares re-send suppression with per-turn recall.
  Bash, Read, Edit, Grep and the other file tools are refused even if listed. The
  rail stays dormant until `hooks.inbound_recall_floor_vec_distance` is set as well,
  and `techne doctor` reports `hook:inbound_recall` as dormant for that reason; the
  floor for inbound text has not been measured yet. The recall pipeline in
  `user_prompt_submit` is now one function, `recall_block`, used by both rails. The
  one change to per-turn recall: the query builder now runs just before the hook's
  timeout block rather than inside it.
- **CI now tests the installed artifact, not just the checkout** (#1059). Every other
  job installs techne editable, so a module, package-data file, or entry point missing
  from the wheel is invisible to them — the class of defect that reached external
  partners in v0.16.0 (knowledge #2836). The new `acceptance` job builds the wheel,
  installs it into a fresh venv twice (core, then `[recommended]`), and — from a fresh
  temp dir, so the checkout can't shadow the install — checks `techne --version`
  against pyproject's version, every `console_scripts`/`techne.sync_adapters` entry
  point (`tests/test_entry_points.py`, also runnable standalone), two bundled guides,
  `init --help`/`doctor --help`, and that `techne.__file__` resolves under the venv.
  `scripts/acceptance.sh` is the same script docs/releasing.md's SOP now calls.
- **`techne audit` splits recall attribution by where each row came from** (#995,
  PR #996). `recall_attribution` gains `per_origin`, which buckets each injected
  knowledge row as `reflect`, `consolidate` or `hand` by its `source`, and `unknown` when
  the row no longer exists. `techne audit` prints it as a "by origin" table: rows,
  rows never cited, times shown, and the citation rate. The citation matcher also
  counts the two current citation forms, `knowledge #N` and `🧠N`, which it missed
  before; `techne #N` (an issue written in prose) still does not count. New
  `scripts/usefulness_audit.py` finds operator corrections in session transcripts and
  checks whether a stored row already held the answer.

### Fixed

- **`session_end` no longer times out on sessions full of paths that share
  ancestors** (#1155). Thread detection's repo lookup walked up from every absolute
  path in a session and checked five repo markers in each ancestor directory,
  remembering answers per full path only, so paths sharing parents checked the same
  directories again. On macOS, `/home` is an automount where each check costs about
  20 ms. A 789-observation session of Linux container paths checked `/home` 175
  times, detection took 10–13 s, and the 5 s `Stop` hook timed out on every turn,
  so that session's threads were never saved while it ran. The lookup now answers
  each directory once per pass and skips the marker checks for a directory that
  isn't there. That session's `session_end` work went from 10.1 s to 1.7 s, with
  the same 34 threads.
- **`techne init` no longer crashes when the cwd sits under a directory the user can't
  read** (#1149). The walk up to the git root called `Path.exists()`, which raises
  `PermissionError` on an unreadable level, so `sudo -u <user>` with an inherited cwd or a
  shared server stopped init outright. An unreadable level now counts as "no `.git`
  here" and the walk continues. Found by the 0.18.0 clean-machine quickstart run.
- **An explicit search that quotes a rare phrase now puts a row holding it second**
  (#1142). The vector channel could crowd such rows off the page entirely: on a 63k-row
  store, `litestream replicate techne.db exec` put neither exact row in the top 200.
  `techne_search`, `techne search` and `techne_recall` now seat one row holding the
  longest rare run of two or more query words (FTS tokenization and stemming apply)
  right after the top result, tagged `reserved_slot: "phrase_head"`. Rare means fewer
  than 0.207% of the searched rows hold it (at least 5), a bar chosen on 115 held-out
  exact-phrase queries. Questions and queries of more than 8 words are skipped, and the
  top result never moves. The per-turn and inbound recall rails do not use it; their
  pages are unchanged. Federated recall keeps the seat in place. `search()` gains
  `phrase_seat=False`.
- **The phrase-seated row now shows the phrase it was seated for** (#1146, follow-up
  to #1142). Observation rows reach the page cut to their first 500 characters, and
  the seat could pick a row whose phrase sits past that cut. On the 2026-09-28 eval
  store, `litestream replicate techne.db exec` seated a 1,733-character Dockerfile
  echo whose `litestream replicates # techne.db` starts near character 890, so the
  page showed only shell output. When the phrase ends past the cut, the seated
  observation row's content is now a 500-character window around it, starting with
  `…`, and the row gains `content_window` (offsets into the full text) and
  `content_chars_total`. This changes what the page shows, not what it retrieves:
  ranks, order and every other row are unchanged, and the eval gain on that query
  (marker rank 0 to 2 on three arms) comes from a row that was already second.
  Knowledge rows are not windowed, because federation's cross-store dedup compares
  head-cut content and a windowed local copy would be flagged against its own
  replica. The window follows the seat's row wherever it lands, not only when the
  seat moves it (#1150): on an FTS-only page (a fresh or not-yet-embedded store,
  found by the 0.18.0 quickstart re-run), as the top vector hit, or in the
  identifier head. Windowing other lexical rows is #1147.
- **`techne doctor`'s prose-supersession check no longer prints a ready-to-run
  `techne knowledge supersede` command for a pair it matched only through a bare
  `techne#N` reference** (#1065). On 2026-09-28 doctor printed exactly that for two
  live, unrelated knowledge rows: knowledge 1769 read "a correction to techne#621's
  own rationale" (meaning issue #621, not knowledge row 621, a grep-assist restart
  checklist), and knowledge 3537 read "CORRECTS techne#896's ... framing" (issue
  #896, not knowledge row 896, the dev02 hardware profile). `find_declared_supersessions`
  now marks a pair `ambiguous` when the only match is a literal `techne#N`, and
  doctor prints those as CHECK items — a sentence naming what to verify, never a
  copy-pasteable command — while a genuine `knowledge #N` or brain-sigil match still
  prints as before. Separately, since the 2026-08-31 knowledge-ref convention
  (`knowledge #N` / the brain sigil for a knowledge row, `techne#N` for an issue), a
  declarer written on or after that date gets no benefit of the doubt at all: a bare
  `techne#N` there is not accepted as a knowledge reference, so no pair is reported
  for it. An older declarer (e.g. knowledge 1155's "SUPERSEDES techne#882") still
  needs it and still gets it, just always flagged `ambiguous`. A review round caught
  that an unmarked list continuation of a `techne#N` opener (`"techne#1 and #2"`)
  still counted as confident, reproducing the same incident with one more id. A
  continuation now inherits the ambiguity of the nearest marked reference before it
  (in `"knowledge #1, techne#2 and #3"`, #3 is ambiguous), and doctor's CHECK line
  quotes it as written: `#3 (listed after techne#2)`, not `techne#3`. The reverse
  case (a later, unambiguous restatement of the same id, e.g. `"techne#5 and
  knowledge #5"`) is no longer shadowed by the earlier ambiguous one.
- **`DCO sign-off check` no longer fails every Dependabot PR** (PR #1140; seen on PR #1135). Dependabot
  does sign off, but as `dependabot[bot] <support@github.com>` — an address that never
  matches its noreply author email, and neither is configurable — so the check added
  in #1130 failed on every dependency-update PR. `arch-cards` (the same-repo job) now
  skips a commit authored exactly as `dependabot[bot]
  <49699333+dependabot[bot]@users.noreply.github.com>` and logs which commits it
  skipped as bot commits; `fork-pr` gets no such exemption, because a fork PR can
  forge any commit author, including that one.
- **`context_injections` now has a retention bound** (#1073). The table had none, and
  #1020 turned per-turn recall on by default, so every install gained several rows per
  turn forever. Each write now deletes up to 500 rows older than 180 days. 180 days
  covers every reader: re-send suppression reads only the current session, and `techne
  audit` with no `--since` now means "every retained row" rather than all-time. Two
  per-session `extension_state` families had no prune either and now get the 7-day one
  the others use: `session_start:deferred:<session>` and `heuristic_threads.<session>`.
  A third, `reflect:emitted:<session>`, stays unbounded on purpose: it is the reflect
  emit cursor, and a missing marker reads as "never emitted", so pruning it would put
  old sessions back up for reflection.
- **The install-the-extra hint now tests the REAL `_install_context()`/`_direct_url()`
  path, not only the pinned one** (#1128). Every hint test ran against
  `tests/conftest.py`'s autouse `_pin_install_context`, which replaces
  `_install_context` wholesale — so `_direct_url()`'s own read of the installed
  package's `direct_url.json` never ran in the suite at all. A mutant that read
  `"direct-url.json"` instead passed all 52 hint tests: it would route an editable
  install to the plain-venv branch, whose `<python> -m pip install
  "techne[recommended] @ git+..."` replaces the editable checkout with a second,
  older copy. `tests/test_install_command.py` now has three tests, marked
  `real_install_context` to opt out of the pin, that fake
  `importlib.metadata.distribution` and exercise the real functions end to end;
  reverting the fix (the `direct-url.json` typo) fails them. Also: an editable
  install in a `uv`-created venv (no pip at all) now reads `INSTALLER` from the
  dist-info and gets `uv pip install --python <python> -e ".[recommended]"`
  instead of a `-m pip install -e` that would fail outright.
- **`techne init` says so when Claude Code is not installed** (#1123, #1137). A bare
  `techne init` on a machine without Claude Code still registers the hooks, the
  skills and the MCP server (in `~/.claude.json`, which is where Claude Code reads
  user-scope MCP servers), so installing Claude Code afterwards needs no second
  init. It now prints "Claude Code not found; techne's hooks are registered and take
  effect when you install it" with the `--platform standalone` alternative, and no
  longer claims Claude Code auto-memory is on or offers to turn it off. Claude Code
  counts as present when `claude` is on PATH or its `projects/` dir exists (which
  Claude Code writes and techne never creates), so a rerun of `techne init` gets the
  same answer. Nothing changes on a machine that has it.
- **init's semantic-recall hint matches how techne was installed** (#1124). An
  editable install was told to `pipx install --force "techne[recommended] @ git+..."`,
  which puts a second, older, non-editable copy beside the checkout. It now gets
  `cd <checkout> && <its python> -m pip install -e ".[recommended]"`, read from PEP
  610 `direct_url.json`. A uv tool install gets `uv tool install --force`, any other
  venv gets its own pip with the git URL, and pipx or a non-venv install keeps the
  pipx form. `techne capabilities` uses the same command.
- **`techne doctor` no longer reports `hook:pre_compact`/`hook:post_compact` STALE
  when nothing has compacted in the window** (#1075). Both fire only on a
  compaction, and this store has no independent way to confirm one happened —
  Claude Code's SessionStart payload carries `source="compact"` (#906), but
  `session_start.py` reads it only to pick the banner shape and never persists it,
  and nothing parses the transcript's own compaction markers either. An aged-out
  or never-set marker on these two hooks now reads DORMANT (`idle: no compaction
  since <date>`, or `idle — no compaction recorded yet`) instead of STALE — the
  outcome under 1M-context sessions, where a compaction can legitimately not
  happen for a long stretch. A hook that DID run and failed is unaffected: its
  `last_error` marker still reads BROKEN before the new branch is reached.
- **`scripts/check_comment_blocks.sh` now actually runs** (#1071). The repo-local
  `hooks.commentblockgate` git config key had no reader — the operator's global
  pre-commit loader never grew a branch for it — so the gate had never executed
  anywhere, on any commit, since it was added for #998. The script itself is
  correct (verified against the real regression it would have caught: PR #1116's
  `scripts/acceptance.sh` landed with a 30-line header, over the default 12-line
  max). Wired into the `arch-cards` CI job as a step that diffs the PR's own
  commits against `origin/main`, so pre-existing content already on `main` is
  never re-flagged — only what a PR newly adds. Documented in CONTRIBUTING.md so
  contributors can also run it locally, staged, as a pre-commit check.
- **Per-turn recall no longer searches on harness frames** (#1080). A subagent
  hand-back reaches `UserPromptSubmit` as `<agent-message from="…">`, a harness
  paragraph ("[Subagent hand-back] … The report follows:") and the indented report.
  Recall searched the frame, and its rare tokens (`agent-message`, `hand-back`) pulled
  rows about hand-backs and took #1078's identifier-head slots. Recall now drops the
  tag and frame and searches the report the way inbound recall searches a tool result:
  techne's own rows stripped, then its question lines, else its opening. A
  `<cross-session-message>` loses its tag (socket path, name, mode) and keeps the
  peer's text the same way, and a `<task-notification>` sent with typed text is
  dropped while the typed text is searched. Prompts with none of these frames build
  the same query as before: 38 of 38 plain prompts in a 40-prompt sample.
- **`techne skills install` now upgrades a stale, pre-ownership `/self-care` copy
  instead of treating it as user-edited forever** (#1072). The installed
  `~/.claude/commands/self-care.md` on a machine whose manifest entry for this skill
  was lost or never shared (a recreated DB, a different `TECHNE_HOME` pointed at the
  same `~/.claude`) matched no `manifest[name]`, so it read as `kept-user-copy` no
  matter how many releases passed, even though the operator never touched it.
  `_install_owned` now also checks the on-disk hash against `KNOWN_SHIPPED_HASHES`,
  the sha256 of every past release of `self-care` and `techne-promote` (current
  excluded): a match adopts and refreshes the file, the same as an exact-current
  match; a hash matching nothing, current or historical, is still the real signal of
  a tuned document and is left alone. `skills status` reports the
  recognized-but-unadopted case as `outdated`, not `user-modified`. Verified against a
  scratch `HOME` with a byte-for-byte 2026-08-17 copy (adopts) and a hand-edited one
  (kept). `KNOWN_SHIPPED_HASHES` is hand-maintained, so a new test pins each
  procedure's current shipped hash and fails, with a message naming the fix, if
  content changes without the outgoing hash moving into the list (Prudence, PR #1117
  r1).
- **`techne/skills/self-care.md` drops a former-employer qualifier from its
  attribution line** (#1068). Line 9's parenthetical named the employer the
  adapted-from procedure came from; the sentence reads the same without it. A
  case-insensitive grep of the tracked tree found no other occurrence of the name in
  this repo.
- **`paraphrase-dbpool` scores 1.0 again in the vector eval, and losing any one
  paraphrase task now fails the gate** (#1083). Since #977 the lexical-tail task's
  padded gold row used infrastructure prose ("connection pooling ... sixty four
  sockets") as filler. Its averaged vector reached the page of 18 other tasks and took
  rank 1 on `paraphrase-dbpool`, pushing that gold off the page. The paraphrase class
  read r@10 0.833 over a 0.8 floor, so the gate stayed green. The filler is now sailing
  prose that appears on no other task's page; paraphrase is back at r@5 0.667 / r@10 1.0
  / MRR 0.546, the #401 bar, and the lexical task still arrives via `fts` in the
  `lexical_tail` slot at rank 10. The paraphrase r@10 floor is 0.85, so one lost task
  (0.833) fails. Eval only; no search code changed.
- **A late `obs_key` answer no longer re-offers its thread** (#1106). An answer
  ingested by `obs_key` after the emit that persisted its thread (a batch rendered on
  the fallback path and ingested later) was recorded under the `obs-` key, but the
  thread's queue row stayed owed, so the next emit served it once more. Emit now moves
  the `obs-` ledger for every thread that resolves to a row on each pass, not only the
  ones it just wrote, and drains the row of an answered one. Measured on the test
  fixture: 4 late-answered threads re-offered before, 0 after.
- **The eval-vec retrieval diff works for a PR that adds a `search()` kwarg** (#1086).
  The diff step runs HEAD's tests against origin/main's `search()`, and the harness
  passed `identifier_head_slots` on every call, so any base without that kwarg died with
  `TypeError` and wrote no metrics (PR #1082, run 36490769983). The harness now passes
  an optional kwarg only when a task sets it. On the base leg (`TECHNE_EVAL_BASE_LEG=1`)
  a task whose kwargs the base `search()` does not accept, checked with
  `inspect.signature`, is skipped and listed under `skipped_no_base`. The comparer
  prints it as "new in HEAD, no base" and diffs the rest, with class means over the
  tasks both legs ran. Outside the base leg an unsupported kwarg still fails the gate,
  and the #857 refusal still fires for any shortfall the skip list does not explain.
- **Federated recall keeps the local page's order and reserved seats** (#1089). With
  connections configured, `techne_recall` re-scored the local `search()` page by BM25,
  recency and decay when merging remote rows, discarding the vec-first order and the
  `identifier_head`, `curated_head` and `lexical_tail` seats. On the 2026-09-28 snapshot,
  with no remote rows at all, the page's #1 stayed #1 in 6 of 40 real prompts. The merge
  now keeps every reserved row at its index and fills the other seats by alternating the
  local page's rows with each remote store's rows, each in its own order, local first.
  With no remote rows the federated page is the local page. Local-only recall is
  unchanged.
- **Reflect emit no longer serves `thread_id: null` items** (#1091). Emit re-detects a
  session's threads from its observations, and that detection could run ahead of the
  last persisted one: observations recorded after the final `session_end` detection, or
  a detector change since the session was saved. Those threads went out with no
  `thread_id`, the overnight drain read that as "cannot ingest", and the #954 heal
  re-offered them every pass, so 13 of 95 sessions stopped partial. Emit now writes the
  missing `threads` rows (and their reflection-queue rows) before rendering, so every
  item has a real id and candidates get their `source_thread_id`. Rows that already
  exist are left alone. A thread answered, declined or rendered earlier under its
  `obs-` key keeps that state under its new id, so nothing a past drain answered is
  offered again. If the write fails, emit logs a warning and falls back to the
  `obs_key` path, which ingest still accepts.
- **Exact identifier lookups land at the top of the page** (#1078). A query carrying a
  rare identifier-shaped token (`emit_max_chars`, `b2dcdd2`, `reflect-drain --go`) now
  seats up to two rows FTS matched on that token at the head of the `search()` page,
  marked `reserved_slot: "identifier_head"`. Vec-first had ordered them by cosine
  distance: on the 2026-09-28 snapshot `emit_max_chars 100000 config.yaml techne` missed
  the page at limit 10 and 15, and `reflect-drain`/`commentblockgate` sat at the last
  slot; all three are now rank 1-2. "Rare" is fewer matching rows than
  `search.identifier_rare_fraction` (0.2%) of the searched tables, so `techne.db` never
  triggers. A query with no rare identifier-shaped token returns the same page as
  before; word-like terms (`well-known`, `reflect-drain`) must be twice as rare, since
  shape cannot tell them apart. The per-turn hook honours both settings. Three
  identifier tasks join the eval gate.
- **A short AND result is topped up with OR results instead of hiding them** (#1079,
  PR #1088). FTS ran its OR-of-terms pass only when the implicit-AND pass found
  nothing, so a single row holding every query term replaced the whole candidate pool
  and every row matching only some terms was lost. When the AND pass returns fewer rows
  than asked for, OR rows now fill the rest. The two blocks are reranked separately
  and the AND block comes first, so an OR row never outranks an AND row; with
  `table="all"` each table is topped up on its own. When no top-up happens the page is
  unchanged. A first attempt (PR #1081) reranked both blocks together, which pushed
  exact identifier matches down on a real store, and was reverted (PR #1084).
- **An anchored memory whose commit left HEAD's history is flagged, not trusted** (#391).
  Recall's freshness check compared only file bytes, so after a rebase or force-push a
  byte-identical file read `unchanged`. It now also asks `git merge-base --is-ancestor`,
  and a match whose pinned commit is no longer an ancestor reads `diverged` ("re-read
  before quoting"). A sha this clone has never seen, or one already garbage-collected,
  cannot be judged and stays `unchanged`.
- **`techne watch` exits 78 on an exhausted inotify limit** (#529). watchdog raises
  `OSError(ENOSPC)` / `OSError(EMFILE)` when `fs.inotify.max_user_watches` /
  `max_user_instances` runs out, which took the generic exit path, so the shipped unit
  respawned it forever. On Linux both now raise `StartupConfigError` naming the sysctl
  to raise; other errors, and every platform but Linux, are unchanged.
- **The outbox age sweep can remove `synced` rows with a NULL `synced_at`** (#547). The
  second tombstone's tie-break is vacuously true for NULL, so such rows (reachable only
  through out-of-band writes) were kept forever. That tombstone exists for
  `MAX(synced_at)`, which ignores NULL, so it now only protects dated rows; the newest
  row per target is still kept by the first tombstone.
- **`archivist_search` reports the #346 misconfiguration instead of raising** (#727).
  With `archivist_db_path` pointed at the personal db, `techne bindings` showed an
  `unavailable` row carrying the refusal while the domain call raised `ValueError`. Both
  now go through one conversion and return the same message. The issue's other two
  findings were already fixed on main.
- **A backup set with two problems reports both** (#785). A malformed manifest entry
  used to mask a genuinely missing database, so the operator learned the second cause
  only after fixing the first. The refusal now lists both phrases, and a malformed entry
  carries the resolver's reason (for example "contains a NUL byte") instead of only the
  domain name.
- **The codex edit rail stops injecting import and test nodes** (#406). Synthetic
  `<module>` nodes and test-file nodes are dropped before the `min_deps` threshold, the
  `limit` cap and the log. Measured on 2026-08-02, 92 of 117 injections hit the cap of
  10 and ~70% of those slots were this noise, which pushed real depth-2 dependents out.
  Test paths come from the new `hooks.codex_edit_rail_exclude_globs`; `total_hits`
  still logs the raw walk size so the audit's radius stays comparable.
- **The edit rail names why it stayed silent** (#721). `_edit_rail_outcome` returns
  `EditRailOutcome(text, declined_because)`, one reason per decline path (flag off,
  not an edit, no repo, repo not indexed, linked worktree, below `min_deps`, and so
  on). `assert ... is None` passed for thirteen reasons at once; a test can now assert
  the one it means.
- **`codex_blast_radius` says which index it answered from** (#730). Every return
  carries `domain` / `binding` / `state` / `target`, as `codex_search` already did.
  From a linked worktree it reads the primary's index, and before this only
  `codex_search` said so. `walked` / `truncated` are unchanged beside it.
- **TypeScript interfaces are indexed** (#859). The symbol entry test named three
  captures and the kind pick named four, so every `interface_declaration` match was
  discarded. `CODEX_KINDS` now lists every kind the indexer writes, and a test indexes
  one of each. An existing index picks interfaces up when its `.ts` files are
  re-indexed: run `techne codex index --rebuild <repo>`.
- **`architect sweep-legacy-verdicts` can spare real full-evals** (#549). New
  `--spare-with-eval-doc DIR` (spare a row whose URL slug has a
  `<slug>-evaluation*.md`) and `--skip-url URL`, both repeatable. The dry run labels
  each row `legacy` or `spare`, and `--apply` is refused without a spare option,
  because measured on 2026-08-11 every `--before` date that reached the legacy rows
  also reached real operator judgments.
- **`Where to apply in techne / cabal / liminal` links all three targets** (#693).
  `_APPLY_RE` captured only the first target, so the row looked linked while the rest
  were dropped. It now takes a continuation joined by `/`, `,`, `and` or `&` on the
  same line, counted only when the list then ends at `**`, `:` or the line end, so
  prose after the first target (`in techne, cabal's gate differs`) still links only
  the first. On the live corpus this adds 42 links to 36 patterns and loses none;
  run `techne architect backfill` to write them.
- **A target whose patterns are all retired reads the same on both surfaces**
  (#1016). `techne architect targets` counted every `pattern_applies` row while
  `pattern_search` served active patterns only. `targets` now counts active patterns
  and lists retired-only targets separately. On MCP, an unconfigured retired-only
  target's error says its patterns are retired and lists the targets that hold active
  ones; a configured one returns no rows with a notice saying the same.
- **The evidence gate reads the whole Bash command, not its first line** (#713, first
  finding). `actions` held only line 1, so a multi-line command opening with `cd <repo>`
  hid a `gh pr merge` on line 2 and `ran_action` reported it unbacked. The command is now
  cut at the first output marker, so output from a captured row never enters an action,
  and heredoc bodies are stripped because they are data. The `tests_green` union and
  heredoc-written files are not changed here.
- **`techne session-end` no longer records a clean exit for a session that died long
  ago** (#986). Past the sweep's 6-hour idle window it stamps `ended_at` from last
  activity with source `derived`, and leaves an existing end at or after last activity
  alone. The Stop hook still stamps now/`clean`.
- **SessionStart reads `source`** (#906). On `resume`, `clear` and `compact` the model
  gets a one-line reactivation notice instead of the full banner; the operator's banner
  is unchanged. After `clear` the model gets threads and knowledge back through
  `techne_resume` and peer sessions through `techne_inbox`, both named in the line.
  `startup`, a missing source or an unknown one keep the full banner.
- **Reflect keeps the error at the end of a long observation** (#907). Up to 3
  failure-shaped lines (120 chars each) from the part past the 200-char head are
  appended, so a Bash error is in the prompt instead of only the command echo.
- **Session-id prefixes match literally** (#902). `threads show`, `threads redetect`
  and `reflect undecline` share one lookup that escapes `%` and `_`; `undecline
  --session '%'` used to retract every decline in the newest session.
- **`techne doctor` reports the deployed runtime even when run from a repo checkout**
  (#690). `runtime:revision` only inspects the checkout `techne` was imported from, which
  from a working copy is DORMANT by design, so a deployment 20 commits behind main stayed
  invisible. When `~/.techne/runtime` is a different checkout, it now gets its own
  `runtime:deployed` row with the same rules.
- **`vectors:coverage` no longer stays yellow over its own remedy's capture** (#704).
  Unembedded observation and thread rows younger than 15 minutes are reported as pending
  and left out of the verdict; older gaps still read STALE.
- **One build identity on every surface** (#982). `--version` skipped `.resolve()` and
  could classify a symlinked install the other way from the banner; the MCP handshake
  reported a bare version. Both now come from `core/health.py::build_identity`, over the
  same resolved path and one build rule (`techne._is_installed_path`).
- **`core/repo.py::find_repo_root` no longer raises on an unreadable ancestor** (#981),
  the same guard `find_repo` got in #963. It is on the SessionStart banner path.
- **An empty legacy `codex.db` is removed instead of warned about on every process**
  (#875). A legacy db that has tables but no `repo_root` still warns.
- **The banner's ref age survives `git gc`** (#571): it is read from origin/main's
  reflog, which `pack-refs --prune` keeps, before the loose ref's mtime.
- **A registry-coverage test that could fail** (#701), replacing one that derived its
  expectation from the registry it was checking; plus the comment and test cleanups
  deferred from #569's review (#572).
- `techne knowledge add` and `knowledge list` take their `--type` choices from core's
  valid types (#877). They offered `pattern`, which core always refuses (patterns live
  in the Architect store), and omitted `document` and `document_card`, which it
  accepts. A refusal from core now prints as a CLI error, not a traceback.
- `techne architect tag ID --status superseded` without `--superseded-by` is refused
  (#880). It wrote a row the summary counted as superseded and `topic_show` listed
  under no parent. `--superseded-by` sets the status and is now the only way to it;
  `--status active` together with `--superseded-by` is refused as contradictory.

- `techne_reflect_emit` results fit under Claude Code's MCP result cap (#993). The size
  trim summed compact JSON per item while the server sends the whole result as indent=2
  JSON, and its 100,000-char default sat above the 25K-token cap: the store has recorded
  refusals from 56,974 chars up, and #993 saw 70,004- and 100,849-char results refused. The trim now measures the
  serialized result the server sends, and `reflect.emit_max_chars` defaults to 45,000.
  CLI `emit -o` slices get smaller by the same amount.
- Reflection debt no longer counts rows reflecting cannot drain (#870, #805). `techne
  reflect reconcile --apply` now parks below-threshold rows (schema v28 adds
  `reflection_queue.parked_at`/`parked_reason`): they stay undrained and in the table,
  but leave the SessionStart debt figure and `reflect stats`' `pending`, and the banner
  counts them on their own line. A parked row un-parks when its thread renders again,
  through session-end re-enqueue, `emit`, or the next `reconcile`. `emit` reports the
  split as numbers, `unemittable_superseded` and `unemittable_below_threshold`, and
  works it out from the detection it already holds instead of running two more.
- Consolidation Tier 1 finds reworded duplicates (#987). It queried FTS with each row's
  whole content, an implicit AND that only the row itself matched, so it formed almost
  no clusters. It now queries with the row's top tf-idf terms that some other live row
  also holds. On a copy of the main store it pairs rows such as knowledge 13 with 210,
  where before it returned self-only hits.
- Consolidation no longer proposes merging `document_card` rows (#823). Cards are left
  out of clustering, and `consolidate_ingest` refuses a merge naming a card (counted as
  `refused`), since a merge supersedes the cards' `doc:` pointers and mints a
  pointerless `fact`.
- Reminder notes and coord message bodies are now redacted before storage (#1037).
  `reminders.add()` and `coord.send()` wrote free text verbatim, so a token pasted
  into `techne_reminder_add` or `techne_send` reached `techne.db` as-is, and a coord
  body was then replayed to every live session by the inbox nudge. Both now call
  `redact_credentials` at the write boundary, like the other free-text sinks. Rows
  stored before this change are not rewritten.
- `docs/librarian-guide.md` no longer says credentials are "stripped" unhedged; it now
  says redaction is pattern-based and best-effort, matching `SECURITY.md` (#1043).
  `SECURITY.md`'s egress scope now says which operator-config layers are locks and
  which an agent can bypass, so a documented non-lock is not reported as a
  vulnerability (#1043).
- `research-readonly`'s standing fetch rule now covers both jobs it is spawned for
  (#1041). It read "stay on the URL you were given and its same-origin paths", which
  fit `investigate-url` but contradicted `docs/surveying.md` step 4, where the survey
  agent discovers its own candidates from `WebSearch` rather than being handed one URL.
  It now reads "Fetch only the URLs your task gives you, or, when the task asks you to
  find projects, the results of your own searches, plus each one's same-origin paths.
  Never fetch a domain or project a fetched page suggests." `docs/surveying.md` step 4
  still narrows this correctly in its task prompt, so no change was needed there.
  The shipped agent body changed, so an existing install needs `techne init` or
  `techne agents install`; see Upgrading.
- README.md and `docs/four-functions.md` no longer point readers at `tenishai/skills`
  for the richer `/investigate-url` and `/survey-field` procedures (#1007). That repo
  is private, and both links 404 for anyone outside it. The research guides
  themselves shipped in 0.16.0; `techne init --skills` installing them as thin stub
  skills followed in #1040/#1042. The docs now point at `techne guide investigating` /
  `techne guide surveying` instead of `tenishai/skills`.
- Per-turn recall no longer searches on Claude Code background-task notifications
  (#1045). A finished background task arrives at `UserPromptSubmit` as a prompt made
  only of `<task-notification>` blocks; recall searched on its task id and output path
  and injected unrelated rows. On one install that was 36% of recall injections over
  14 days. With recall on, such a turn now skips the search and records the skip:
  as `noop:task-notification`, or in the `ok` detail when other context is injected.
  A prompt with typed text alongside a notification is still searched, and the
  deferred session context and startup directive are unaffected.
- `knowledge.delete` now removes the entry's dependency edges (`knowledge_deps`,
  either direction) and repoints or soft-archives any entry that named it as its
  replacement (`superseded_by`), in one transaction, before deleting the row. Both
  foreign keys have no ON DELETE clause and every connection enforces them, so
  deleting a referenced entry failed with `FOREIGN KEY constraint failed`. Seen live
  when an agent's seeder retired a stale seed entry that a dream-promote had drawn
  an edge to: the delete raised at every boot and the agent crash-looped for two days.
  A predecessor is repointed at the deleted entry's own successor when it has one, or
  archived (`status = 'archived'`) when the deleted entry was the head of its chain —
  either way it stays out of default recall, egress, `get_resume_context`, and
  consolidate/recombine/conflict selection rather than resurfacing as current. The
  deleted entry's own dependents are flipped to `uncertain`, same as `supersede()`.
  `techne knowledge list` (`list_entries`) now also hides `status = 'archived'` rows
  from its default view, consistent with how archived rows are already hidden from
  default recall and egress.
- Follow-ups from reviews of #1116 and #1115 (#1119). `scripts/acceptance.sh`'s
  bundled-guides check now loops over every `techne.docs` `_MANIFEST` entry instead of
  two hardcoded names, and a new hook-modules check loops over every module a
  registered Claude Code hook bakes in (`_BAKED_IN_MODULES`) — both catch a dropped
  force-include or package file in general, the v0.16.0 defect class, rather than only
  the two cases already exercised. The script no longer parses `techne --version`'s
  output (`build_identity`'s docstring says nothing in this repo does that); it still
  runs `--version` as a smoke test and compares the installed version against
  `pyproject.toml` via `importlib.metadata` instead. Per-turn recall's paste-close and
  task-notification frame detection (`techne/hooks/_recall_query.py`) are now anchored
  to column zero (`(?m)^`), matching how the harness actually emits both: a hand-back
  report that quotes either tag's literal text mid-line, or a typed mention of
  `<task-notification>`, can no longer be mistaken for the real tag and truncate the
  report or drop text between two unrelated mentions. A frame's paste-close bound is
  also now depth-tracked (`_enclosing_paste_close`) rather than "the next textual
  close anywhere after it", so it is set only when the frame is genuinely nested
  inside a paste — depth-tracked with an OPEN tag anchored to column zero in the
  forward scan too (Prudence, PR #1127 r1): an unanchored open there let a report's
  own quote of one inflate the depth past what the real close could bring back down,
  which could silently drop the operator's own text after the paste.

### Changed

- **`config.yaml` holds only the keys you set** (#1070). `techne init` wrote every field,
  so a default changed in a later release never reached an install that had run it: the
  file pinned the old value. #993's `reflect.emit_max_chars` 100000 -> 45000 and #1020's
  hooks were both inert on existing installs for this reason. `Config.save` now writes a
  key only when the file already set it or its value differs from the default, plus
  `config_version: 2`; keys this version does not recognise are written back unchanged.
  An old full file is migrated as described under Upgrading. `techne doctor` lists the
  keys the file pins. The hook fast-path cache (`.config.env`) is now also invalidated
  when `techne/config.py` changes, since a default can change without config.yaml doing so.

- **`techne_recall` returns a count of federation conflicts, not the items** (#863).
  Each item carries an adjudication prompt with no cap; ten of them were 90% of a
  103,091-char recall that overflowed the tool-output limit. Recall now returns
  `federation_conflicts_pending` and a note; pass `include_federation_conflicts=true`
  to get the items for `techne_federation_conflict_ingest`.
- **Reflection authors verify before writing `high`** (#790). The reflect prompt, the
  `techne-promote` skill and the reflect guide now say the observation block is a
  clipped index, not evidence, and that a `high` claim naming a number, issue, state or
  mechanism must be checked against the source first, or written at `medium`. The
  skill's allowed tools gain `gh issue view`, `gh pr view` and `git log`.
- **One helper owns the #70 AND-then-OR fallback** (#559).
  `search.fts_with_or_fallback(query, run)` holds the control flow; the four FTS
  callers (`search_fts`, the knowledge connection search, `search_symbols`,
  `search_patterns`) keep their own SQL in `run`. No behaviour change.

- **Per-turn recall searches pasted content on its question lines** instead of on
  the whole prompt (#1018). Pasted content means a `<pasted_content>` block, or
  text longer than `hooks.per_turn_recall_long_prompt_chars` (default 600) with
  lines starting with each of Claude Code's transcript markers, `❯` and `⏺` (a
  shell prompt draws `❯` but never `⏺`). It is searched on its `❯` user turns
  and its lines ending in `?`. The operator's text around the paste is used only
  when the paste holds no question, and a wrapper over the limit contributes only
  its own question lines. Measured on 30 real turns with the question embedded in
  its surrounding transcript: mean top-5 overlap with the typed question's
  results rose from 0.09 (whole prompt) to 0.79 under this policy. Inside pasted content, techne's
  own rail output is stripped first (recalled rows, the session-start banner,
  codex blocks, coord and reminder lines, `<system-reminder>` blocks), so a
  pasted recall block does not re-select the rows it already names. Typed
  prompts of any length are searched unchanged and never stripped. The
  `context_injections` row now records the query that was searched.
- **`techne init` asks before turning off Claude Code auto-memory** (#1023). Running
  both memory layers drifts and double-injects. In a terminal, a new user is asked;
  on yes only `autoMemoryEnabled` in `~/.claude/settings.json` changes, re-read
  immediately before the write. Non-interactive init only prints the recommendation.
  Under the spool-primary posture nothing is offered (the offer keys on the
  resolved posture, not on detected memory files); init instead says how to move
  to techne-only, and that techne does not import Claude Code's existing memory.

- **Recommended hooks are on by default** (#1020). `per_turn_recall`, `capture_bash`,
  `codex_grep_assist`, `codex_edit_rail` and `codex_reindex_on_edit` now default to
  `true`, and `per_turn_recall_chars` to 1000 (was 450). A first-time user should not
  get a worse techne because they never heard of a flag. The codex rails do nothing
  outside an indexed repo.

  An existing install gets these defaults through the sparse-config migration (#1070);
  how, and how to keep the old values, is under Upgrading.

- **`techne init` downloads the embedding model** (#1021). A fresh install never got
  semantic search: the SessionStart backfill passes `--no-download` (#815) so a cold
  cache cannot hang startup, and nothing else fetched the model. `init` now does the
  one-time download in the foreground and embeds what is stored. A failed download
  prints how to finish later and never fails the install. `--no-vectors` skips it.

- **The architect's pattern-target roster is now configured, not hardcoded** (#1008).
  It was a literal set of ten of this project's own repo names, and the
  `pattern_search` MCP description interpolated it — so every agent connecting to a
  techne server received a list of projects it could not see. It is now
  `architect.pattern_targets`, defaulting to `["techne"]`.

  **An existing store may need one line of config** (step 7 of Upgrading). This set is
  validation as well as documentation: tagging drops a target that is not on the list,
  silently,
  because an unknown token is nearly always a typo. If your store has patterns tagged
  with anything other than `techne`, new patterns will stop being tagged with those
  names until you configure them. Nothing already stored is lost, and nothing becomes
  unqueryable — `pattern_search` serves rows under an unconfigured target and returns a
  `notice` saying it is unconfigured, rather than rejecting it. Re-running
  `techne architect backfill` recovers missed tags once the config is right.

  Run `techne architect targets` — it compares your config against what the store
  actually holds and prints the line to paste:

      architect:
        pattern_targets: ["cabal", "liminal", "techne"]

- `pattern_search` now returns an explicit error for a target that is neither configured
  nor present in the store, instead of zero results — a rejected target reading as "no
  patterns exist" was #674's original defect. A target that IS present is served, with a
  notice, because the roster governs writing and the store governs reading.

### Performance

Search and recall return the same pages as before, except for the tie noted under
PR #1098; they get there faster. Figures are
each PR's own measurement on a copy of the maintainer's store.

- **FTS ranks matches before fetching their rows, and drops OR terms that match more
  than half a table** (#1092, #1093, PR #1098). Each FTS table now sorts
  `(rowid, bm25)` pairs, keeps the candidate limit, and only then fetches those rows,
  instead of carrying every match's content through the sort. An OR term found in more
  than half of a table's rows scores near zero in FTS5 but still costs a scan, so it is
  dropped from the OR pass; the query is never emptied, and a table re-runs the full
  query when the pruned result is short or a dropped term could decide the last slot.
  `search(all, 10)` went from 150/448 ms to 84/234 ms (p50/p95), and the per-turn
  hook's `search(knowledge, 15)` from 19.1/40.3 ms to 17.2/27.2 ms. Across 4,396
  distinct per-turn queries, the only page differences were 18 pairs of rows, tied on
  every kept term, that swapped places.
- **One connection, one sqlite-vec load and one query embedding per `search()`**
  (#1094, PR #1100). The FTS channel, the vector channel and the identifier and curated
  pools each opened their own. p50 drops by about 6 ms for `search(all, 10)` and 2.5 ms
  for `search(knowledge, 15)`.
- **The model2vec embedding model loads in about 80 ms instead of 450 ms** (#1096,
  PR #1097, PR #1099). This is the backend on Python 3.14+ or when configured. A new
  loader, `techne/core/static_direct.py`, reads the cached model files directly and
  repeats model2vec's encode steps; its vectors are bit-identical, so nothing is
  re-embedded. The per-turn hook's median wall time went from 743 ms to 333 ms, and the
  MCP server and CLI start faster too. Other model layouts and a cold cache fall back to
  model2vec itself, which is still the only code that downloads. The `vectors` extra now
  pins `model2vec<0.10`, because the loader copies its encode steps.
- **The MCP server keeps one read connection open for `techne_search` and
  `techne_recall`** (#1095, PR #1103). It is memory-mapped and query-only, so the page
  cache and the sqlite-vec load survive between tool calls. Warm `techne_search` p50
  went from about 102 ms to 79 ms for `(all, 10)` and from 16 ms to 8 ms for
  `(knowledge, 15)`. Writes keep their own connections. The cost is memory: on a 247 MB
  store the server's RSS went from about 106 MB to 192 MB. The difference is clean,
  file-backed mmap pages the kernel can reclaim under pressure; dirty (heap) memory was
  unchanged (76.1 vs 74.8 MB with and without mmap).

### Removed

- **`search.expansion_ratio`** (#1074). Parsed and saved, passed to no search function:
  `search_fts` has its own default of 2 and `search()` hard-codes `limit * 2`. Search
  behaviour is unchanged. A config carrying the key still loads; the key is dropped.
- **`SyncConfig.auth`, `.audience` and `.extra`** (#1074). No sync adapter call passed
  them on (`load_adapter` gets `url`, `path` and `allow_unlisted` only). Values under
  `sync:` in config.yaml stay in the file and are not read, as before.

### Security

- **CI supply-chain hygiene ahead of the go-public flip** (#1134). Every third-party
  GitHub Action in `test.yml` is now pinned to a full commit sha, with a `# vX.Y.Z`
  comment naming the tag it resolves to, instead of a mutable `@v4`/`@v5` tag an
  upstream maintainer (or their compromised account) could repoint. Added
  `.github/dependabot.yml`: weekly, grouped updates for `github-actions` only for now —
  `pip` waits until the repo is public and `dependabot[bot]` PRs can be routed to the
  hosted `fork-pr` job instead of the self-hosted pool, since Dependabot PRs are
  same-repo PRs and a version bump runs install-time code before review. Ran `pip-audit`
  once against the full `.[dev,codex,vectors,recommended]` dependency set (109 packages,
  Python 3.12): no known vulnerabilities. Evaluated ruff's `S` (bandit) rule set as a
  candidate `static_analysis` control: 9,938 findings against `techne tests` (9,528 of
  them `S101`, assert-in-tests), 132 against `techne/` alone — too many to triage in one
  PR, so it is not enabled here; the existing 158-error baseline is untouched.

### Upgrading

Do these in order. Every step is safe to repeat.

**1. Back up.** `techne backup` copies every techne database as one timestamped set
under `~/.techne/backups/` (or `$TECHNE_HOME/backups/`).

**2. Upgrade the install.**

    pipx install --force "techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.18.0"

From a checkout, `git pull`, then reinstall with the venv's own Python so the changed
`vectors` pin is applied (#1096): `python -m pip install -e ".[recommended]"`.

**3. Run `techne init` before you edit `config.yaml`.** An upgrade replaces the code and
nothing else. This release, `init` does five things an upgraded install needs:

- It rewrites `config.yaml` in the new sparse form (#1070), holding only the keys you
  set. It first copies the old file to `config.yaml.bak-<timestamp>` and prints one line
  starting `config.yaml migrated to sparse form:` that names every key whose value
  changed. See "What the config migration changes" below.
- It registers the UserPromptSubmit hook (per-turn recall) and the PreToolUse hook
  (codex grep-assist), which are now on by default (#1020). Claude Code loads them
  from the next session.
- It installs the `investigate-url` and `survey-field` stub skills (#1040) and
  refreshes the `/self-care` and `/techne-promote` procedures and the
  `research-readonly` agent, whose fetch rule changed (#1041). `--no-skills` skips all
  of this; see step 5.
- It downloads the embedding model when a vectors backend is installed (#1021): a
  one-time download of tens of MB, then it embeds what is stored. `--no-vectors` skips
  it. A failed download does not fail `init`; it prints the `techne vectors backfill`
  command to run later.
- It migrates the database schema if nothing has yet (step 4).

Until `init` runs, `techne doctor` and the session-start banner show one warning line:
`config.yaml sets N key(s) that are not in effect until it is rewritten: <key> (file:
<old>, in effect: <new>), ...`. Expect it on any install set up with 0.17.0 or earlier.
The "in effect" values are what techne already uses; the line only says the file is
out of date. `init` clears it. If you passed `--posture` to an earlier `init`, pass it
again.

**4. The schema migrates itself, v27 -> v28.** The first techne command or hook to open
`techne.db` adds two nullable columns, `reflection_queue.parked_at` and
`parked_reason` (#870). It is additive, and techne first copies the database to
`~/.techne/backups/techne-v27-to-v28-<timestamp>.db.bak`. There is nothing to run.

**5. `techne skills install` and `techne agents install`**, only if you ran `init` with
`--no-skills`. Both replace a copy techne wrote that you have not edited, including an
unedited `/self-care` from an older release whose ownership record was lost (#1072).
An edited copy is reported and left alone; `--force` overwrites it. An agent file that
predates techne's ownership record (you never ran `agents install` or `init` since it
shipped) is reported as `kept-user-copy`; use `techne agents install --force` if you
never edited it.

**6. Opt out of the new defaults, after `init`.** These defaults changed:

| Key | Old | New |
|---|---|---|
| `hooks.per_turn_recall`, `capture_bash`, `codex_grep_assist`, `codex_edit_rail`, `codex_reindex_on_edit` | `false` | `true` (#1020) |
| `hooks.per_turn_recall_chars` | 200 or 450 | 1000 (#1020) |
| `reflect.emit_max_chars` | 100000 | 45000 (#993) |

To keep an old value, set it in `config.yaml` after step 3; a key set after the
rewrite stays pinned, and `techne doctor` lists the keys your file pins. All five hook
flags are read on every hook call, so `false` takes effect at the next prompt or tool
call. The per-turn recall and grep-assist hooks stay registered, skipping that work,
until the next `techne init` removes them. `capture_bash` grows the observation log; pure
read-only navigation commands are still dropped. `emit_max_chars` above 45000 can
produce `techne_reflect_emit` results larger than Claude Code accepts from an MCP tool
(#993).

**7. If these apply to you:**

- **You rely on `context_injections` rows older than 180 days** (#1073). From the
  first injection after upgrading, each write deletes up to 500 rows older than 180
  days, and `techne audit` with no `--since` covers only the rows kept. The step 1
  backup, taken before the upgrade, keeps the older rows.
- **Your architect store tags patterns with targets other than `techne`** (#1008). New
  patterns now silently drop a target that is not in `architect.pattern_targets`
  (default `["techne"]`). Run `techne architect targets`; it prints the line to add to
  `config.yaml`. Then run `techne architect backfill` to restore tags missed meanwhile.
- **Patterns list several targets on one "Where to apply" line** (#693). `techne
  architect backfill` writes the links that were dropped before.
- **You index TypeScript with codex** (#859). Interfaces were never indexed. Run `techne
  codex index --rebuild <repo>` for each repo with `.ts` files.
- **The banner reports reflection debt that never drains** (#870). `techne reflect
  reconcile` shows what it would do; `--apply` parks rows below the thread threshold so
  they stop counting as owed.

**What the config migration changes.** A file written by an older `techne init` has
every key and no `config_version`. On every load, techne removes each key whose value
equals a default that key has ever shipped with, so it follows today's default and
future ones. A value no release shipped is kept as your choice, and so is any key techne
does not recognise. The table of past defaults is `PAST_DEFAULTS` in
`techne/config_migration.py`. On an install set up with 0.17.0 or earlier, the keys whose
value changes are the ones in the table in step 6. `threads.link_threshold: 0.85` is
also un-pinned (the default has been 0.05 since June), and the retired
`search.expansion_ratio` is dropped (#1074). This applies in memory from the first load
after upgrading, in every hook and command; the file itself is rewritten by `init`.

This is why step 3 comes before any edit: until `init` rewrites the file, every load
migrates it again, so a value you type that was once a default (`per_turn_recall:
false`, `capture_bash: false`, `emit_max_chars: 100000`) is un-pinned again and the file
says one thing while another is in effect.

**One case the migration cannot tell apart:** if you set a key by hand to a value that
was also its default at some point (for example `per_turn_recall: false` before #1020),
it is un-pinned. Check the line `techne init` prints, and set the key again after
`init` if you meant it.

## [0.17.0] — 2026-09-18

### Added

- **Four new retrieval-at-scale arms, and the #883 vocabulary confound is settled**
  (#945). `pytest -m eval_scale` now varies one thing at a time against the same gold
  set: corpus size (the original #883 curve), lexical vs hybrid (`use_vectors`), the
  padding *vocabulary*, which table the crowd lives in, and synthetic vs real
  distractors.

  **The finding: the #883 curve was measuring vocabulary, not scale.** Padding drawn
  from a disjoint domain — same shape, same sizes, same index arithmetic, only the
  words differ — does not degrade paraphrase recall at all: 1.000 at both n=2,036 and
  n=4,036, where this project's own vocabulary scores 0.667 and 0.500. Both corpora
  embed the same number of rows, so the difference is semantic adjacency between the
  distractors and the gold, not how many of them there are.

  **So no paraphrase floor is set, and now there is a measurement saying why.** A floor
  from the near arm would encode this project's vocabulary as a property of the
  retriever; one from the far arm would be 1.000. The only arm resembling production —
  real rows borrowed read-only from the local store — scores 0.167 against the
  synthetic 0.667, and cannot be committed because it differs per machine.

  The real-content arm never copies store content anywhere durable: rows are read
  through a `mode=ro` URI (never `connect()`, which would run migrations against the
  operator's live store), used only as distractors in a temporary database, and the
  queries and gold stay the committed set. It skips cleanly where there is no store.

- **`eval_scale` has a CI home** (#895, partial). It runs in the existing `eval-vec`
  job, which already installs its dependencies — 38.8s for all five arms against that
  job's own ~30s gate. Reporting only: the arms assert their own instruments (padding
  present, distinct, embedded; the two channels genuinely differing) and never a score,
  so a merge cannot be blocked by embedding wobble. `probe`, the other marker #895
  names, is untouched and still has no home.

- **The suite refuses to run under an interpreter that has no techne** (#893). `python3 -m
  pytest` from the repo root resolves to the system interpreter and reported **36 failures**
  — `StopIteration` and `PackageNotFoundError` raised deep inside `importlib.metadata`, in
  tests named `test_litellm_not_in_core_deps` and `test_an_installed_build_still_trusts_dist_info`.
  That reads as a packaging regression, and nothing in the output said "wrong interpreter".

  **A hard exit rather than a warning, because the natural sanity check certifies the
  mistake.** *Does this fail on main too?* comes back **yes** — the interpreter is wrong on
  both sides. On 2026-09-06, while verifying #892, exactly that happened: 36 failures,
  reproduced on `main`, concluded to be pre-existing breakage. They were not. A warning line
  would have scrolled past above 36 red tests; an exit that replaces them cannot. It runs in
  `pytest_sessionstart` rather than a fixture because it must fire before collection, which
  is where the misleading errors are raised.

- **`eval-vec`'s base leg survives a PR that adds a module** (#857). The leg is built as
  "BASE's library, HEAD's tests", so any PR landing a module and its test module together
  made that file unimportable there — and collection runs **before** marker selection, so
  one file killed the whole leg over tests `-m eval_vec` would have deselected anyway. Main
  had been green throughout, so this was latent rather than a regression; #856 was the first
  PR to hit it.

  **The `--continue-on-collection-errors` flag alone would have been worse than the bug.**
  Tolerating collection errors lets a *short* base run reach the comparer, and
  `compare_retrieval_metrics.py` read `per_class[...]["n"]` without ever comparing it
  between the legs — a base measuring 4 cases against a head measuring 6 printed "No
  regression against the base commit." So the comparer now refuses unequal case sets
  **before** printing any table, rather than printing a `base -> head` row that invites the
  comparison the refusal exists to prevent.

- **A `table="all"` page reserves head slots for curated rows** (#946). The vec-first
  union ordered the page by cosine distance across every table, so a knowledge row
  competed with the whole observation ledger for a slot. #827's floor and #831's
  ordering could not help: both act on the lexical tail, and a curated row the VECTOR
  channel found was never in it. An external evaluator measured three gold queries
  ranking their curated answer at 1, 2 and 7 in `table="knowledge"` and losing it
  entirely at `table="all"` (35,096 observations against 25 curated rows).

  `search()` now holds `search.curated_head_slots` head positions (default 3) for the
  best curated rows, filled from a scoped knowledge query and displacing the weakest
  non-curated head rows one for one — the page does not grow and the tail floor is
  untouched. An unfilled reservation costs nothing; `0` restores the previous page
  exactly. Applies to `table="all"` only: a caller that narrowed the query gets its
  table and nothing else.

  Rows now carry `reserved_slot` (`"curated_head"` or `"lexical_tail"`) when a
  reservation seated them, and nothing when they placed on rank — so "why is this row
  here" is answerable without re-deriving a score. That decomposability is why this is
  a reservation rather than a distance handicap.

  Measured on the operator's live store, 12 real queries at limit 10: mean curated rows
  per page 4.00 → 5.00, per-page minimum 1 → 3. The default of 3 comes from the
  evaluator's k=10 sweep on their corpus, where 2 bought no recall over the status quo
  and 5 was no better than 3; it is capped at a third of the head so a narrow page is
  not swamped.

- **`techne reflect promote --content` promotes a reviewer's edit** (#952). Review had only
  promote and discard, so a candidate stating a durable claim inside counts, ids or
  versions that go stale forced a choice between losing the claim and storing the
  wrapper. `--content` (or `--content-file`) applies the edited text; the proposal stays
  on the candidate in `content`, and the edit is stored in the new
  `knowledge_candidates.revised_content`. `reflect review` shows both, and `reflect
  stats` reports a `revised` count. Only `add` candidates can be revised (a merge or
  supersede was faithfulness-checked against its original text), and a revision
  requires `--reason`.

- The evidence gate now covers **state claims**, not just deeds (#584). `evidence.py`
  verifies what an agent *did* against the observation ledger; assertions about
  *project state* — who owns an issue, whether a PR is merged, whether we already
  do X — had no check at all, because conversation offers no diff and no suite for
  a reviewer to read. The issue's measurement was four wrong state claims in one
  session, three of them caught by the operator.

  `techne/core/state_claims.py` adds two types, reachable through the same
  `techne_check_evidence` call and mixable with deed claims in one list:
  `artifact_state` (against `gh`, keeping `merged` distinct from `closed`) and
  `capability` (against our own source).

  A third type, `ownership` — mined from the coord bus — was built and **cut
  before merge** (#894). Both shipped checkers work because the source *answers
  the question asked*: `gh` returns the state, the filesystem returns the file.
  The bus does not answer "who owns X"; it holds prose ownership can sometimes
  be inferred from. Measured against the real bus it named a claimant for 21% of
  subjects and was wrong for about 30% of those. Shipping it would have made
  `backed` mean a lookup in two types and a guess in a third, with no way for a
  consumer of one verdict list to tell them apart.

  Absence is always `unverifiable`, never `unbacked` — a subject the bus never
  mentions, an issue `gh` could not reach, a missing file. `unbacked` means the
  source was read and contradicts the claim. The MCP result reports
  `unverifiable_count` beside `unbacked_count` so "0 unbacked" cannot be misread
  as "all checked". Advisory only, as the deed gate is.

- `architect` stats now report `patterns_superseded` beside the active count, and
  `techne architect` prints `Patterns: N active … (+M superseded)` (#878). Nothing was
  miscounted — `patterns` has always meant *active* patterns — but the label said
  less than the value held, so checking it against `SELECT COUNT(*) FROM patterns`
  showed a gap that read as a broken counter. Measured on the live store: 555
  reported against 561 rows, and the six supersessions were reported as a bug in
  the summary. Active plus superseded now reconciles with the table.

  `architect tag --status` is now a `click.Choice` over the two values those counts
  report. It previously accepted anything: `--status archived` returned "updated" and
  exit 0, after which the summary read `Patterns: 0 active … (+1 superseded)` against
  two rows — itemised-looking and wrong, which is worse than the bare count it
  replaced. The buckets are exhaustive by construction rather than by comment, so the
  reconciliation above is true as written. Two limits: this does not heal a store that
  already carries a third status, and it does not cover raw SQL. Neither is reachable
  through the CLI, and no live store has a third value.

- `context_injections` now records `rows_truncated` and `chars_dropped` for the
  per-turn recall rail (#853). The table said what was injected and never what was
  severed, so the char cap — a tuning knob — had no observable effect: raising or
  lowering it could not be evaluated from data. Both columns arrive by lazy
  migration on an existing db. The pair is deliberate; forty rows cut by five
  characters and forty cut by four hundred are different worlds, and a count alone
  cannot tell them apart.

- **`checks_green` — a lint or type-check claim now has a checkable shape** (#939).
  The evidence gate could confirm a linter RAN and not that it PASSED: `ran_action`
  matches a command substring and answers "did this appear", never "what did it
  return", so an agent that ran `ruff check`, got 14 errors and wrote "lint is clean"
  produced a BACKED verdict.

  **Outcome is read from each tool's own summary line, because there is nothing else
  to read.** The ledger carries no exit code — surveyed across 52 live-captured Bash
  responses, the harness supplies `stdout`, `stderr`, `interrupted`, `isImage`,
  `noOutputExpected` and two background keys, and nothing resembling a return status.

  So the vocabulary splits. `ruff`, `mypy` and `pyright` print an explicit summary and
  green is provable. `tsc`, `eslint`, `biome`, `prettier`, `go vet` and `cargo clippy`
  are SILENT on success: they are recognised, so the gate knows a check ran, and their
  outcome is `unverifiable` — never `backed`. A readable tool may not vouch for an
  unread one.

  **A known limit, stated here because you will meet it:** the gate is scoped to a
  session WINDOW, so a run where a linter fails, is fixed, and passes reports
  `unbacked`. That is the modal agent workflow. The verdict text says so, naming the
  later success, but it is a false accusation and it is deliberate rather than
  overlooked.

- **The SessionStart banner reports codex and arch-card freshness** (#937). A stale
  code index does not degrade gracefully — it answers "we don't have that" with the
  same confidence as a correct miss, and nothing on the way in said so. `codex:freshness`
  compares a newly recorded `indexed_head` against the current HEAD; `cards:freshness`
  compares each card's anchor against an in-process git blob sha.

  Both are cheap by construction rather than deferred: 32 checks in 0.149s against a
  5s fail-open hook budget. The card check computes `sha1(b"blob %d\0" % len(data) + data)`
  in-process — byte-identical to `git hash-object` wherever no eol filter applies — at
  0.006s for 19 anchors, against 21 subprocesses and 0.50s for shelling out.

- **`techne --version` identifies a build installed from a sha** (#965). It reported
  the last RELEASE on a build carrying seven merged PRs and two migrations, because
  `__version__` resolves through installed distribution metadata. PEP 610's
  `direct_url.json` carries the resolved commit for a VCS install, so `--version`,
  `techne_status` and the banner can all name it.

- **`conflict.min_shared_keywords`** (#933), a floor on shared content tokens for the
  no-tags fallback path. Defaults to **1**, which is the implicit floor it replaces —
  no behaviour change on an existing config. Values below 1 are refused rather than
  coerced: they do not loosen the floor, they remove the topical filter entirely.

- **pi is a first-class client** (#57) — the same three-lane memory bridge the opencode
  plugin has. It needed **no new capture surface**: pi's in-process TypeScript lifecycle
  events map straight onto the existing one, so there is still one `techne observe` path
  and now three clients. `tool_execution_end` and a user-role `message_end` become
  observations, `agent_settled` is the Stop-hook analogue that ends the session, and
  `before_agent_start` appends `techne context` to the system prompt.

  Sessions are stamped `platform: pi` and keyed by the **session-file UUID**, so a resumed
  pi session keeps its techne session across a restart. Content goes over stdin rather than
  argv — no size limit, no shell-quoting hazard, multi-line safe. A missing `TECHNE_BIN` or
  a stale install warns **once** instead of silently dropping every observation. Only
  techne's own plumbing is skipped from capture; calls to other MCP servers are real work
  and get captured.

  **One lane pi still lacks, as opencode does:** no compaction checkpoint/restore. Claude
  Code's `PreCompact`/`PostCompact` hooks have no pi equivalent, so a compaction there
  loses the checkpoint that a Claude Code session keeps.

- **A writer can mark a knowledge row `uncertain`** (#885). The only way from a `current`
  row to that state was the dependency cascade — something was superseded, so its
  dependents became doubtful.
  `mark_uncertain()` is the write-side counterpart to `reconfirm()`, inverse on `status`
  alone. It is idempotent, and it leaves a superseded or archived row alone.

  **A library seam with no CLI or MCP surface** — nothing in techne calls it and techne's
  own behaviour is unchanged. It exists for an agent persona writing its own conclusions
  into a store (cabal#141), where those rows landed `current` and came back as established
  fact.

### Changed

- `hooks.per_turn_recall_chars` now defaults to **450**, was 200 (#826). Of the 2,468
  live injectable knowledge rows (median length 476), **a 200-char cap would sever
  2,442 — 98.9% — where 450 severs 1,305, or 52.9%.** Those are properties of the store,
  not counts of rows the rail actually cut: it injects five rows per turn selected by
  embedding similarity and BM25, so the severance it delivers is drawn from that
  population by a decidedly non-uniform process. #826 measured 58 of 58 injected facts
  severed in the field at 200, which agrees with the corpus figure — but at 200 nearly
  everything severs however it was selected, so that agreement says nothing about 450,
  where 47.1% of the store now sits below the cap and selection bias has room to move the
  number. Treat 52.9% as a bound on the store, not a prediction for the rail; #853 adds
  the telemetry that will settle it.
  Until #839 (released in 0.16.1) a severed row carried no marker distinguishing it from
  a complete one; that is fixed, and what this change adds is that 47.1% of rows now
  arrive **whole** rather than marked-and-severed, with the still-severed ones carrying
  ~2.25× the content.
  Costs ~312 extra tokens per turn on a five-row push typically, ~412 typical-worst (not a bound — `rstrip` runs after the floor, so a whitespace run at the cut reaches ~420). (312
  is the full-delta figure; the worst case is wider, not narrower, because
  boundary-seeking floors at 60% of the cap and so shrinks the *old* emission too —
  `int(200×0.6)=120` against `int(450×0.6)=270`, a 330-char delta.) This is a **per-turn**
  cost where #808's schema block is per-session: over 40 turns it is ~12,500 tokens, and
  the two are not comparable until that multiplication is done.
  The cap remains configurable, and since #842 (below) it is honoured at any value —
  when this entry was first written `search()` still pre-truncated at 500, so no
  default above 499 could take effect.

- **Conflict candidates are ranked before truncation, and the order reaching the model
  changes** (#933). The SQL carried no `ORDER BY` and nothing sorted the matches, so
  `max_comparison_candidates` kept whichever rows SQLite happened to return. Candidates
  are now ranked by shared-token COUNT, with an overlap-coefficient tiebreak and an id
  tiebreak for determinism.

  **This reaches the LLM prompt on every conflict check, not only when truncation
  bites.** A second-order effect worth knowing on upgrade: `check_hash` is keyed on the
  candidate id set, so a group already marked proposed re-emits ONCE wherever the match
  count exceeds `max_comparison_candidates`.

  The count is primary for a measured reason. Ranking on the coefficient alone favours
  SHORT rows, because `shared/min(len)` scores a one-token row 1.0 against anything:
  against a focus of `{canary, deploy, five, minutes, window}`, the row `"canary"`
  outranked `"the deploy canary window is thirty minutes"`.

- **The reflect proposing prompt names two failure modes** (#950). It pushed one way
  only — toward specifics — and specifics are what go stale. A blind-judged run ranked
  the opposite failure LARGER: `obvious` was 4 of 9 discards against decaying specifics
  at 2. The prompt now names TOO OBVIOUS and TOO SPECIFIC with an example of each and
  describes the band between them. **Not yet measured** against a before/after run on
  the same threads, which #950 asks for and which needs a bound model.

- **`knowledge.last_confirmed_at` is guaranteed by the write path, not a schema default**
  (#938). It was `NOT NULL DEFAULT CURRENT_TIMESTAMP` on a fresh install and plain
  nullable on a migrated one AT THE SAME `schema_version`, so a store's provenance
  changed its retrieval order. The declaration now matches what the v5 ALTER can
  produce and all five `INSERT INTO knowledge` paths set the value.

  **The existing NULLs are deliberately NOT backfilled** — that re-ranks every install
  and #938 says it must be measured against the eval gate first. See the Upgrading
  section and #979.

### Fixed

- **Nothing is injected into a subagent's context** (#929). A subagent presents the
  **parent's `session_id`**, so every session-keyed lookup in `post_tool_use` resolved to
  the parent's state and `_inject_coord_messages` handed a peer's message to the subagent.
  Due reminders and the codex blast-radius rail rode the same `additionalContext` path.
  Measured: 15 WebFetch calls made by `research-readonly` subagents on 2026-09-14 are
  recorded in the ledger under the parent session id.

  **This matters more than untidiness because of which subagent it is.**
  `research-readonly` reads untrusted third-party pages, and its sandbox is built by
  *removing* tools — no Bash, no store access, no MCP, and no `Read` specifically to close
  the local-read → WebFetch exfiltration path. These rails put internal state into that
  context by a route none of those removals touch. It was found because the subagent
  volunteered that it had been handed a peer coordination message and had declined to act
  on it; behaving well is not a control.

  The gate is `agent_id` and **only** `agent_id`. It is the one field present *only* inside
  a subagent call — `agent_type` is also set for an ordinary top-level `--agent` session, so
  gating on it would silently stop delivering coord messages and due reminders to a real
  session with a person in front of it. It **fails open** on a client old enough not to send
  `agent_id`, which is stated in the code rather than papered over.

  **A second bug fell out, and it defeated the push half of techne#376.** The coord rail is
  surface-once-per-session and a subagent shares the session id, so a subagent's tool call
  consumed the parent's message and the parent never received the injected copy — for any
  session using subagents. It stayed invisible because the message remained unread and the
  banner recovered it. Found by mutation-testing the fix.

- **A metadata-only transcript no longer fails import** (#941). Claude Code writes a
  one-line transcript (`{"type":"custom-title",...}`) when a session is titled and never
  takes a turn. It has no timestamp, so the session insert passed NULL for
  `started_at` and failed with `NOT NULL constraint failed`. Import now skips a
  transcript with no user or assistant records, reason "no events (metadata-only
  transcript)", and `techne import` treats that skip as benign (exit 0). No session row
  is written: a defaulted timestamp would record a session nobody observed.

- **`reflect promote` / `reflect discard` no longer record an unnamed decision as
  `human`** (#953). `--by` defaulted to `human`, so an agent that left it off was audited
  as a person. The default is now `unspecified`. Existing rows recorded as `human`
  cannot be told apart after the fact.

- **`codex blast-radius --only-proven` now proves same-file callers, and never
  prints a bare empty result** (#940). The resolver bound calls only through
  imports, so a function called from its own file had no proven callers and
  `--only-proven` printed `(nothing reachable)` — the answer that reads as "safe to
  change". A new same-file pass (`scope_resolved`) binds a bare call to the file's
  single module-level function or class of that name, refusing when an import,
  a nested definition or a second definition could shadow it. On this repo,
  proven call edges went from 6,120 to 9,482.

  Codex schema v5 adds `edges.bare_call`, because `f()` and `x.f()` share one edge
  row and `requests.get()` must not prove a dependency on a local `get`. The next
  `techne codex index` re-parses existing repos to fill it (reparse debt, as for
  v4). Under `--only-proven` the CLI and the MCP tool now say how many
  name-matched dependents they hid.

- **`techne vectors status` now says when a backfill is needed, and `capabilities`
  no longer calls an empty knowledge table 100% embedded** (#948, #942).

  `vectors status` printed `re-embed needed: False` with 25 promoted knowledge rows
  and no vectors. That line only means "the embedding backend changed". The command
  now shows how many rows in each vec table are not embedded (the same population
  `vectors backfill` acts on: live knowledge missing or `vec_stale`, and every
  observation and thread without a vector) and a separate `backfill needed:` line.
  `vector_channel_status()` returns the new `unembedded` and `backfill_needed` fields.

  `vectors:coverage` printed `0/0 live knowledge embedded and current (100%)` on a
  store with 35,096 embedded observations and no knowledge. With no live knowledge it
  now says `no live knowledge yet` and reports observation and thread coverage
  (`observations 35096/35096 embedded`), or `nothing to embed yet`. The verdict is
  unchanged: it still depends only on whether anything is unembedded.

- **A copied `TECHNE_HOME` no longer writes to the original store** (#947).
  `techne init` saved the resolved default `db_path` and `data_dir` into
  `config.yaml` as absolute paths, and a persisted path wins over `TECHNE_HOME`.
  Copy a home, export `TECHNE_HOME` at the copy, and every write still landed in
  the original, with no message. Found following a "work on a copy" instruction.

  `config.yaml` no longer records `db_path` or `data_dir` when they equal the
  `TECHNE_HOME` default, so they follow `TECHNE_HOME` at load. A path set
  elsewhere is still saved and still honoured. When `TECHNE_HOME` is set and a
  persisted path resolves outside it, loading warns once per process on stderr
  and in the log, never on stdout.

  **Upgrading:** existing config files still carry the absolute paths `init`
  wrote. If yours point at the default home, delete the `db_path` and
  `data_dir` lines, or re-run `techne init`, which now saves without them.
  techne does not re-root a persisted path on its own: the file cannot tell a
  copied default from a store kept on another volume on purpose.

- **`techne init` now says when search is keyword-only** (#944). A missing or
  mistyped extra (`techne[vec]` instead of `[vectors]`) is only a pip warning, so an
  install could succeed with no embedding backend and every search ran FTS-only,
  with nothing pointing at `techne capabilities`, which reports it. Both init paths
  now check the backend (an import probe and vec-table row counts; no model load): with none installed they
  say so and print the git install command for `[recommended]`; with a backend and
  nothing embedded they print `techne vectors backfill`. Standalone next steps also
  name `techne capabilities`. No warning was added to `search` itself.

- **Install docs no longer send people to the wrong PyPI package** (#943). PyPI
  `techne` is an unrelated 0.0.2 package, and `ARCHITECTURE.md` said "pip/pipx pkg
  `techne`" while naming capabilities ("vec search", "codegraph", "headless") instead
  of extras. It now gives the git install form and the real extra names; README
  says the PyPI name is a different project; `docs/modules.md`,
  `docs/four-functions.md` and README's codex lines no longer show bare
  `pip install "techne[...]"`.

- **Promoted knowledge now records the session it came from** (#949). Promotion left
  `knowledge.origin_session` NULL on every row, so the session was reachable only by
  joining `knowledge_candidates.promoted_to_id` or parsing `source`. Promote (reviewed
  and `write_to: live`) now stamps it for rows it creates from a real session. Schema v26
  backfills existing promoted rows from their candidates by the same rule. Left NULL: rows
  a `retire` pointed at (the survivor did not originate in that session), and merges,
  dream adds and conflict supersedes, whose candidates carry the `system:sleep`
  placeholder rather than a session.

- **`techne import` now persists threads, so a reflect backfill stops re-offering
  answered threads** (#954). Import wrote observations and stopped. With no
  `threads` rows, reflect's emit cursor keyed every imported thread on `prompt_hash`,
  which covers the existing-knowledge block and so changes on any knowledge write:
  promote one candidate and the next emit served threads already ingested. Measured
  on an external store: 35,096 imported observations, 0 threads, `thread_id` null on
  18/18 emitted items.

  Import now runs thread detection for each imported session (and so enqueues its
  reflection debt). A thread that still arrives without a persisted row is tracked
  by a hash of its observation ids instead of `prompt_hash`.

  **Upgrading a store you already imported:** re-run `techne import` over the same
  transcripts. Sessions skipped as "already has N observations" get threads if they
  have none; sessions the live hooks already detected are left alone (a `--force`
  import re-detects, because it appends observations). **The repair re-offers, once,
  every thread answered before it**: those answers carry no thread id, so nothing links
  them to the newly persisted threads. `techne consolidate` catches the duplicate
  candidates.

  Unpersisted threads (detection failed, or not yet repaired) are recorded as answered
  by `obs_key` at ingest and re-offered by the next empty emit if rendered and never
  answered, as #900 does for persisted threads. `techne_reflect_ingest` takes `obs_key`,
  names it in the tool description, and warns in its result when an answer arrives with
  neither `thread_id` nor `obs_key` — without the key the thread stays unclaimed and emit
  offers it again.

  Not fixed here: an external evaluator reported that a resumed session replays its
  parent's turns and imports them under both session ids (not reproduced here), which
  would offer those threads once per id.

- **The reserved lexical slot now goes to the curated row** (#831). #827 made the
  lexical channel reachable by reserving one tail slot; it did not make it useful.
  The curated row loses the slot on the CONFIDENCE-DECAY TIEBREAK. The two
  explanations this entry gave before — one shared BM25 pool, and BM25 favouring
  the shorter document — are both wrong; Prudence measured them dead (#934 r1).
  There is no shared pool: `_fts_match_rows` issues a separate query per table,
  each under its own `LIMIT candidate_limit` (`search.py:336`/`:363`/`:412`), so
  the curated layer has its own quota and the corpus ratio cannot starve it out
  of the candidate set. Raw BM25 magnitude is normalised away before the tables
  compete: `_rerank` min–max normalises `rank` WITHIN each `source_table`
  (`:252-272`, added by techne#56 for exactly this reason), so each table's best
  row arrives at `_combined` with `_norm_bm25 = 1.0`. What decides it is
  `:285-286`, where `_knowledge_decay_score` returns a flat `1.0` for any row
  carrying no `confidence_numeric` — so an observation collects the whole
  `confidence_decay_weight` (0.1) while a `medium`-confidence knowledge row
  collects 0.6 of it, and at equal recency and equal normalised BM25 the
  observation scores 1.1000 against the knowledge row's 1.0600, BOTH FRESHLY
  CONFIRMED. That qualifier is load-bearing and an earlier draft of this entry
  dropped it (Prudence, #934 r3): the decay term is
  `confidence_numeric * 0.5 ** (days_since_confirmed / half_life_days)` at a
  30-day half-life (`knowledge.py:652`, `config.py:616`), so it is confidence
  TIMES age, and age widens the margin rather than narrowing it — a `high` row
  30 days unconfirmed sits at 0.0550, WIDER than a fresh `medium`'s 0.0400. So
  `high` is not intrinsically finer than `medium`; a fresh `medium` outranks a
  stale `high`, which is exactly what the term is for.

  ON THIS STORE THE DECAY HALF IS DORMANT, which is why the 0.0100 figure below
  is stable rather than a day-zero snapshot. `last_confirmed_at` is declared
  `NOT NULL DEFAULT CURRENT_TIMESTAMP` in the CREATE TABLE
  (`database.py:303`), but the live column — added by ALTER on a migrated store
  — is a plain nullable `TIMESTAMP`, and 2,823 of 2,914 live knowledge rows have
  it NULL. Those rows take `decay_score`'s no-timestamp branch ("treat as
  fresh") and never decay. So a FRESH install and a MIGRATED store rank
  differently on this term; only 91 rows here decay at all. Filed separately.

  With that established: `medium` is merely the default (`knowledge.py:63`), and
  knowledge #1810 — the entry "issue 814" and "ae219b5" should reach — is
  `high`/0.9, collecting +0.090 against an observation's +0.100, a margin of
  0.0100. Measured on "issue 814": observation #35460 at 0.9988 against #1810 at
  0.9888, the whole delta in the decay term (Prudence, #934 r2). #1810's
  `last_confirmed_at` is NULL, so that figure does not drift. The fix works at
  any margin — `_curated_first` reorders unconditionally. The ~16:1 corpus
  ratio (measured 2026-09-15 on the live store: 46,489 observations vs 2,904 live
  knowledge rows) still pushes the same way, but as ORDER STATISTICS rather than
  as a shared pool — the best of 20 drawn from 46k is better-selected than the
  best of 20 drawn from 2.9k. So the one reserved slot landed on a command echo
  and the entry that answered the question never reached the caller.

  `_curated_first` in `core/search.py` orders the lexical tail so a knowledge row
  takes the reserved slot when one matched at all. Measured on six
  identifier-shaped queries against the live store: a curated row held the
  reserved slot in **1 of 6** before, **6 of 6** after — and `ae219b5` and
  `issue 814` both reach knowledge #1810 again, the entry #831 names as the one
  the shipped floor put out of reach.

  It **reorders, never filters**: an identifier that exists only in the raw trace
  still reaches the caller, and non-curated rows still spend the slack when the
  vector channel is thin. The floor stays at 1 — widening it to buy this was
  measured and rejected in #829 (paraphrase recall@10 1.0 -> 0.833).

  THE eval_vec GATE CANNOT SEE THIS CHANGE'S SCORED BEHAVIOUR, AND SAYING SO IS
  THE POINT (Temperance, pre-passes on 6769d73 and c185975). Two independent
  reasons, both measured rather than argued:

  1. 18 of the 24 `retrieval_gold.yaml` tasks pin a SINGLE-table `table:` value
     (`exact` knowledge x6, `paraphrase` knowledge x6, `filter`
     knowledge/observations/threads x2 each). For those, `_curated_first`
     partitions all-curated or all-raw and is a structural no-op, so "identical"
     is vacuous there — including for the paraphrase 1.0 / 0.546 figure, which
     rests on its OWN six untouched tasks and is not protected by anything here.
  2. Only the 6 `negative` tasks use `table: all`, and on the SEEDED GOLD DB the
     gate actually runs, `_curated_first` never reorders anything: 6 calls, 4
     with an empty lexical tail, **0 mixed tails, 0 reorders**. The three tasks
     carrying `expected_keys` return their gold at rank 1 with
     `match_source: fts+vec` — both channels, so it is served from `head`, and
     `fts_only` excludes every key already there. Their recall@10=1.0 holds with
     or without this fix.

     AN EARLIER DRAFT OF THIS ENTRY CLAIMED "6/6 saw a mixed tail and 6/6
     reordered", AND THAT WAS MEASURED AGAINST THE WRONG DATABASE — the live
     `~/.techne` store, using the gold set's *queries*. Different corpus,
     opposite answer. Temperance flagged the number RISKY on two passes for
     precisely the reason that it was not reproducible by reading code; running
     her command is what caught it.

  So the gate is evidence of NO REGRESSION and nothing more; it is not evidence
  the change works. That evidence is the four unit tests (teeth-checked by
  neutering the helper), which carry no marker and DO run in CI's default
  `pytest -q -rs -n 4` step, plus the live-store 1/6 -> 6/6 measurement, which
  does not. A gold task exercising the lexical tail at `table: all`
  is what would close the hole, and none exists — worth its own issue.

  Related, found the same way: the `negative` kill-gate asserts `fpr` only.
  `tests/eval/test_retrieval_gate_vec.py` runs recall/MRR floors for `exact`,
  `filter` and `paraphrase` alone, so a recall regression in those three
  `expected_keys`-carrying negative tasks would print but not fail CI.

- Thread detection no longer depends on `PYTHONHASHSEED` (#871).
  `_segment_dominant_repo` built its counts by iterating `ObsFeatures.repos` — a
  **set** — so the dict's insertion order was hash-seeded and `max()` returned the
  first of any tie. Which repo counted as dominant therefore varied per process,
  and that decides segment merges. Measured on one 521-observation session: the
  boundary **scores were byte-identical** across seeds while the thread count came
  out **13 or 15**. Thread ids are content-addressed on the resulting segments, so
  ids moved too — which is how a reflection-queue row stopped matching its own
  session's detection and read as `superseded` one run in five, and why
  `reconcile --apply`, which DELETES what that classifier returns, was deciding by
  coin-flip. Ties now break by name in all THREE dominance helpers — the third,
  `_segment_dominant_entity`, sits in the file that imports the other two and had
  the identical shape (8 distinct winners across 12 processes). `fallback_summary`
  and `_recent_files` also iterated sets into TRUNCATED lists, so hash order chose
  which five files and entities were shown, not merely their order — 12 distinct
  summaries from 12 seeds. All of these land in FTS-indexed columns
  (`threads_fts(label, summary, resume_points)`), and `link_threads` searches
  `label + summary[:200]` to pick a `parent_thread_id`, so a per-process value
  could select and persist a different parent FK.

  **Upgrading — no migration, but read this before running `reconcile --apply`.**
  Nothing is orphaned: `persist_detection`'s prune guards all three `NO ACTION` FK
  columns. What changes is that thread classification becomes *stable* instead of
  flickering, so a session whose rows were written under the old behaviour will
  surface its superseded queue rows as a batch rather than intermittently. Run
  `techne reflect reconcile` (dry) and read what it proposes before `--apply`,
  which deletes.

- A codex schema migration that adds a parse-derived column now forces a re-parse
  (#864). `codex index` skips files whose SHA-256 is unchanged, and adding a column
  changes no file's bytes — so the v3→v4 migration's own "a re-index populates it"
  was true of the resolver and false of the indexer. On any repo nobody had edited,
  the re-index skipped every file, `edges.to_module` stayed NULL forever, and
  `index-all` printed `0 symbols; resolved 0 calls, 0 external` and exited 0.
  Measured on the live rollout: **four of nine repos got zero benefit from #679 and
  reported success**, ladon and liminal at 0 of 455 and 0 of 635 import edges.
  The migration now records `codex_meta.reparse_owed_since` and the next index
  honours it once, says which schema version forced it, and clears it only once
  every stored file has actually been re-parsed — "nothing raised" is too weak,
  because a missing grammar, an unreadable file or a `--language`-limited run all
  skip files without failing. "Gone from disk" is decided by errno rather than
  `Path.is_file()`, which is also False for an unreadable directory and for a
  broken symlink — both of which still hold pre-migration rows. `--rebuild` on `codex index` / `codex index-all` is the manual
  equivalent, for extraction changes with no schema version to hang a debt on
  (a grammar or query fix). Verified against the real ladon index: 0 → 357 bound
  edges, 270 resolved and 89 external, matching the hand-rebuild #864 recorded.

- `hooks.per_turn_recall_chars` is no longer silently capped at 500 (#842). `search()`
  truncated every knowledge row to `_ROW_CHAR_CAP = 500` before any caller saw it, so the
  knob governed nothing above that — and the hook clamped its own request to 499 to keep
  the arithmetic consistent, which made the defect look like a contract. The second
  consequence was the worse one: a row cut to exactly the cap read as whole to
  `_truncate_for_injection`, so the `…` marking a withheld tail was suppressed at every
  cap, measured absent at 600, 500 and 200. `search()` now takes `row_chars`; the rail
  asks for `per_turn_recall_chars + 1`, and that +1 is what leaves the renderer the
  overflow it needs to tell a severed row from a whole one. Measured on a 2,399-char row:
  caps 200/500/600/1200 all honoured, default still 500, marker present at every one.
  The knob moves **knowledge only** — observation and thread rows stay on the fixed
  default, because those are the tool ledger and raising a recall cap should not grow the
  payload by transcript.

- **A resumed session no longer re-imports its parent's turns** (#962). Resuming writes
  a new transcript that REPLAYS the parent's messages: they carry the new `sessionId`
  but keep their ORIGINAL `uuid`, and import idempotency is per session. Measured across
  373 transcripts: 52 file pairs share message uuids, and of the 2 pairs where both
  halves had been imported, 44 observation rows were duplicated.

  Schema **v26 → v27** adds `observations.source_uuid`. Both importers — `techne import`
  and the sync adapter — stamp it and consult the guard. Keyed on uuid rather than a
  content hash deliberately: two concurrent sessions describing one event from different
  vantage points have different uuids, so a content key would discard a real second
  observation.

  **Known limit:** every pre-v27 row has `source_uuid` NULL, and NULL never matches, so
  a resume whose PARENT was imported before the upgrade still duplicates it. Tracked as
  #978.

- **Thread detection no longer loses a whole session to an unreadable path** (#963).
  Detection walks up from the cwd RECORDED IN THE TRANSCRIPT, which on a historical
  corpus may be gone or readable only by root. `find_repo` is the only filesystem call
  on that path, and it relied on `Path.exists()` swallowing the error — a guarantee that
  holds on 3.14 and NOT on 3.11, 3.12 or 3.13. One report lost detection for all 201
  observations of a session.

- **`techne doctor` no longer names commands that do not exist** (#936). Two of three
  rail remedies were built as `f"techne {rail} --help"`, and `techne consolidate` and
  `techne dream` are not CLI verbs. The remedy line is the one part of a check an
  operator acts on without reading the code.

- **The `TECHNE_HOME` mismatch warning prints once per key** (#964), not twice. Both
  emitters landed on the same stream.

- **The eval_vec gate can observe the fusion tail** (#935). 18 of 24 gold tasks pinned a
  single table and the rest returned gold through both channels, so `_curated_first`
  reordered nothing across the whole gate. A positive `table: all` task whose gold the
  vector channel does not return now exercises the reserved lexical slot.

### Upgrading

**Run `techne codex index <repo>` for each repo you query.** An index built before this
release carries no `indexed_head`, so the new `codex:freshness` check reports
UNOBSERVABLE — a mark on every session start until you re-index. It clears on the first
index; nothing is broken in the meantime.

**Have free disk space equal to your store.** Coming from the released v0.16.1 your store
is at schema v25, and opening it migrates v25 → v27 — both steps are in this release (#949
takes it to v26, #962 to v27). A consistent snapshot is written first, through SQLite's
online backup API rather than a file copy, so it is safe in WAL mode. It is written **once**
for the whole transition, not per step, so budget one copy of your store.

**Your codex indexes migrate too**, and each takes its own backup. `CODEX_VERSION` goes
3 → 5 in this release, and a codex database is per-repo, so running the `techne codex index`
step above opens one per repo you have indexed and copies each before migrating it. Budget
for those on top of the store copy.

The 0.9s figure quoted during development measured a single step on a ~220 MB store of
roughly 48,000 observations — the maintainer's store took v25 → v26 and v26 → v27 on
separate days, as two main builds. A tag-installer runs both step bodies inside one open, so
expect longer; that path has not been timed end to end.

**Nothing else is required.** No `techne init`, no `techne vectors backfill` for this
release.

### Things that change under you

**`techne --version` gained a suffix.** `0.16.1` becomes `0.17.0+<sha> (installed)`.
If you parse it, this will break you — and if you were identifying builds by probing
for features, that is what this change exists to replace.

**Conflict candidate ORDER changed** (see Changed). Groups already marked proposed
re-emit once where the match count exceeds `max_comparison_candidates`.

**`checks_green` reports `unbacked` if you fix your lint mid-session.** The gate is
scoped to a session window, not to the last run.

**A migrated store carries a decay inversion, and a fresh one may not.** Where
`knowledge.last_confirmed_at` is NULL, the row never decays while newly written rows do —
so on a store with many NULLs, new knowledge is progressively out-ranked by old. On the
maintainer's store that is 3,029 of 3,122 rows (97%).

**This may not apply to you.** A store created FRESH under v0.16.1 carries
`NOT NULL DEFAULT CURRENT_TIMESTAMP` and every row is stamped — no inversion. The
affected shape is one migrated up from v5 or earlier. Check with:

```sql
SELECT COUNT(*) FROM knowledge WHERE last_confirmed_at IS NULL AND superseded_by IS NULL;
```

Zero means you are unaffected. Tracked as #979; the backfill is deliberately deferred
because it re-ranks every install and belongs behind the eval gate.

## [0.16.1] — 2026-08-30

### Upgrading

Run all three. An upgrade replaces the code and nothing else — it does not rewrite your
hook chain and it does not embed anything.

```sh
techne init             # only `init` writes the SessionStart chain, including the
                        #   vectors-backfill segment this release depends on. It also
                        #   refreshes the bundled skills.
techne skills install   # for anyone who skipped init or passed --no-skills
techne vectors backfill # ONCE, from the CLI — see below
```

**The third one is the easy one to skip and the one that decides whether recall works.**
Embedding happens only through `techne vectors backfill`; nothing embeds on write. The
SessionStart chain runs it with `--no-download`, which is right for a hook — a cold model
cache degrades to "swept, embedded nothing" instead of blocking session start — but it
means the cache never warms itself. The bare CLI keeps the permissive default, so one
manual run primes it and catches up the backlog. An install that has never run it has an
empty vector index and no signal saying so.

**Step three is silent when it does nothing, so check the output rather than the exit
code.** It worked only if it printed a JSON counts block. A `vector search unavailable:
no embedding backend installed` line means it embedded nothing and still exited 0 — and
that same line appears for TWO different causes: the `vectors` extra absent, or the extra
present but the embedding backend unable to load (a cold model cache with no reachable
Hub). Do not diagnose from the cause; diagnose from whether you got counts.

**If you did not get counts:** `techne vectors status` says whether a backend resolved at
all, and re-running `techne vectors backfill` where the model cache can be fetched is the
fix for the second cause — the bare command downloads by default, and only the SessionStart
hook passes `--no-download`. That asymmetry is deliberate: a hook must never block session
start on a network fetch, which is exactly why the cache never warms itself and why this
step is manual.
`techne[recommended]` pulls the extra, which is what the README's **pipx** line uses — the
`git clone` block at the top of the README is core only.

**Pass `--posture` to `init` if you ever set one.** It defaults to `auto` and `init`
re-resolves it every run, so a bare `init` can flip a posture you chose — in either
direction, including out of `spool-primary`, which puts reflect back on auto-promotion.
The resolved value is machine-wide and applies to all techne projects, while the detection
that drives `auto` reads the *current repository*, so the same bare `init` can resolve
differently depending on where you run it from. `init` reports the resolved state rather
than announcing a change.

`docs/deployment.md` carries the same thing as an agent trigger, so an assistant working
in your repo raises it unprompted rather than waiting to be asked.

### Fixed

- A session is titled from its dialogue rather than from whatever wrapper the client
  injected first. Claude Code emits text-bearing `user` turns that no human typed —
  slash commands and their output, shell runs, task notifications — so 25 transcripts
  were titled `<local-command-caveat>Caveat: …` or `---BEGIN USER MESSAGES---`. Only
  an adapter can know its own client's markers, so core never guesses: the optional
  `dialogue_text` hook is read via `getattr` and forwarded to `derive_title`, and a
  source that omits it is byte-for-byte unaffected (#816, #817).
- `search()`'s fusion no longer discards the lexical channel. The vec-first union
  appended FTS-only hits after the whole vector channel and then truncated, so a
  vector channel that filled the page dropped every one — measured 108 of 108 across
  15 identifier-shaped queries on a live store, which is the normal case rather than
  an edge, because on any non-trivial store the vector channel always fills the page.
  A one-slot tail floor now survives truncation. **The size is measured, not chosen**:
  at limit 10 a floor of 2 or more costs a gold row, and only the limit-10 arm can
  show that cost, so re-run that arm before widening it (#827).
- The retrieval-quality gate now **also** diffs base against head on one runner, rather
  than relying on the absolute floors alone. The floors are still there and still fire;
  what they cannot do is notice a regression that lands above them, which is exactly
  what happened when they passed the #827 work green. A base-vs-head comparison has no
  such blind spot. Metrics emission is opt-in (`TECHNE_EVAL_METRICS_OUT`), so with no
  env var set the leg behaves exactly as it did before. The epsilon is zero because the
  leg is byte-deterministic — verified by running it twice and diffing the per-class means
  — so any delta is a real change rather than run noise (#832).
- A corrected document title now reaches rows that were already stored. Both dedup
  keys are content-derived and neither includes the title, so re-indexing with a
  fixed adapter produced byte-identical content, took the `document_unchanged`
  path, and left the old title in place — which made #816's junk titles
  (`<local-command-caveat>Caveat: …`) unrepairable by the one remedy #817's note
  invited people to try. `store_document` now updates the title in place when the
  content is unchanged, and `index_transcripts` reports a `titles_repaired` count
  so the pass is not indistinguishable from a no-op. **Re-index to heal existing
  rows.** A pending card's gist is repaired the same way, because a pending card's
  content *is* the title and recall reads the card rather than the document; a
  filled card's agent-written gist is never overwritten. The repair updates in
  place rather than superseding — the document did not change, only our reading of
  what it should be called. A repaired card is flagged for re-embedding, so the
  correction reaches semantic recall and not only FTS (#819).
- `techne doctor` and the SessionStart banner now report a pending document-card
  backlog. A card is created `pending`, holding the document title as a stand-in
  gist until the agent-assisted fill pass writes a real one — and it rides recall
  in the meantime, so recall could return a placeholder where a gist was promised.
  Nothing said one was waiting: 28 cards sat pending for twelve days and were found
  only because a consolidate cluster happened to surface some of them. The new
  `cards:pending` check keys on **age**, not count, because a card is pending for
  the whole normal gap between indexing and the fill pass, and a check that fires
  on the healthy state trains its reader to ignore it. A backlog whose age cannot be
  read reports UNOBSERVABLE rather than healthy, and the remedy names the backlog
  size because `cards emit` defaults to `--limit 50` (#824).
- Per-turn recall no longer severs every injected fact mid-word. `_build_context`
  cut each row with a bare `content[:200]` from the day the rail landed; measured
  over one real session, **58 of 58 injected facts ended mid-word** — a
  cross-reference cut at `CORRECTS techne#`, a remedy command cut mid-path. The
  cut now lands on a sentence boundary when one falls in the last 40% of the
  budget, else a word boundary, and a truncated row ends in `…` with one header
  line naming the cap, so a severed row announces itself instead of reading as the
  whole rule. **The 200-character default is unchanged**: five rows at 200 is
  ~250 tokens on every prompt and #808 measures per-session context as already the
  dominant cost, so raising it is an operator decision — now available as
  `hooks.per_turn_recall_chars` — the last fixed number in this rail's tuning
  surface. It is clamped to 499, because `search()` already truncates knowledge rows
  to 500 without marking them and an unclamped higher value would suppress the `…`
  and report a severed row as whole (#842). A non-positive value injects no block at
  all rather than removing the cap, matching the sibling `per_turn_recall_limit: 0`,
  since unbounded is the expensive direction to reach by accident (#826).
- Optional hooks on the `TranscriptSource` Protocol are no longer annotated, so an
  adapter that omits them satisfies `isinstance` again. `TranscriptSource` is
  `@runtime_checkable`, and `isinstance()` enforces every *annotated* non-method data
  member by `hasattr` — measured on 3.9 through 3.14, so on every Python this project
  supports, not the "3.12+" the old comments claimed. `dialogue_text` (#817) and
  `unreadable` (#629) were both annotated, so the minimal adapter shape they exist for
  had already stopped satisfying the Protocol. Both are now documented as docstring
  conventions read through `getattr`, matching the convention already recorded
  for `provides` on `SourceAdapter`. Only new-adapter authors were affected — the one shipped adapter
  declares both (#818).
- `ClaudeCodeTranscriptSource.dialogue_text`'s `staticmethod` binding is covered on the
  path production takes. Production reads the hook off an *instance*; every test added
  with #817 read it off the *class*, so dropping the binding left them all green and the
  only witness was an unrelated #629 test that happened to drive the real indexer. A
  test now drives the real adapter through the real indexer and asserts the title
  (#820).
- `techne.__version__` reports the version of the code that is actually imported.
  `importlib.metadata` reads the dist-info written at install time, and for an
  *editable* install that stamp never moves when the checkout does — so a checkout
  that had advanced to 0.16.0 kept reporting 0.15.0, and every surface built on
  `__version__` inherited it: the SessionStart banner paired a stale version with a
  live revision (reading authoritative while half wrong), and `backup_all_dbs`
  stamped the wrong `techne_version` into every manifest it wrote. For a checkout
  the version now comes from the checkout's own `pyproject.toml`, using the same
  build test as `core/health.py::_revision_info` so the two surfaces cannot
  disagree. A real (non-editable) install is unaffected — dist-info stays
  authoritative there. A checkout whose `pyproject.toml` cannot be read reports
  `0.0.0+unknown` and does **not** fall back to the dist-info: on a checkout that
  stamp is known-unreliable, so consulting it would silently restore the same
  stale-version-beside-live-revision lie under a `build: "checkout"` label
  (#825, recurrence of #763).

- `techne init` no longer deletes the `techne vectors backfill` step from the
  Claude Code SessionStart chain. The step had to be hand-wired into
  `settings.json`, and `register_hooks` strips every hook group whose command
  mentions techne and re-appends its own template — which did not contain it. A
  hand edit therefore verified clean and disappeared at the next init, with no
  error: one machine drifted 883 observations and 62 knowledge rows behind
  before the missing segment was traced. The template now owns the segment, so
  re-init preserves it (#704, #814).

  **Existing installs must re-run `techne init` to pick this up.** The fix ships
  in the template `init` writes, and nothing rewrites `settings.json` on upgrade.
  `techne doctor`'s hook-drift check compares `(event, matcher)` and never the
  command, so it reports no drift while the segment is missing — an upgraded
  install looks healthy and keeps drifting. The `vectors:coverage` warning is the
  only signal, and its remedy is a one-shot that fixes the number and leaves the
  hook absent.
- The SessionStart registration now carries Claude Code's per-hook `timeout`
  (120s; the default for a command hook is 600s). With `--no-download` below
  this is defence in depth rather than the primary guard — it covers a hung
  filesystem or a wedged interpreter, not a Hugging Face fetch, since the chain
  no longer makes one (#704, #814).
- The SessionStart backfill now runs with `--no-download`, a new flag on
  `techne vectors backfill`. Owning the hook chain is what made this possible:
  operator shell and hook previously invoked the identical string, so there was
  no signal to branch on. A cold model cache now degrades to "swept, embedded
  nothing" instead of blocking session start on a Hugging Face fetch. The bare
  CLI keeps the permissive default, so the documented cold-cache recovery is
  unchanged (#814).

## [0.16.0] — 2026-08-28

The release about addressing memory by name. 0.15.0 went after what the store is
willing to *claim*; this one goes after how you *reach* it — four domains you call by
name rather than by database file, every answer reporting what it resolved to, and one
command that tells you what everything is bound to. The vocabulary is the deliverable:
an operator learns *archivist, librarian, architect, codex* here and keeps those names
when the work grows into liminal and cabal, where the same call may resolve somewhere
else entirely.

Two things to read before upgrading: `techne backup --out DIR` changes meaning (see
**BREAKING** under Changed), and the core schema moves 24 → 25, which migrates on first
open and is not reversible — back up first.

<!-- AUDIT NOTE (#726/#748), 2026-08-26. Two merged changes are DELIBERATELY not listed here,
     recorded so a later audit does not read the absence as an oversight — the distinction this
     PR exists to make, since "excluded on purpose" and "missed" are indistinguishable in
     silence:

       #765 (techne#249) — widened a decay-drift test's tolerance from 1e-9 to 1e-6
       #768             — widened a debounce smoke test's window from 50ms to 500ms

     Both are TEST TOLERANCES with no user-facing behaviour change. Both were flakes whose real
     cause is tracked in #769: the CI runner is capped at --cpus=8, four xdist workers exceed
     it, and the kernel throttles the whole cgroup in 46% of scheduling periods — 116.8s of
     cumulative stall in one pipeline, measured. Neither tolerance change is a bar being
     lowered: each was verified to still fail on the defect it guards. -->


### Added

- **`techne_guide` — the guides are reachable without a shell** (#754). The server
  instructions tell every connected agent that techne ships guides, but the only way to
  read one was `techne guide <name>`, a **shell** command. A client connected purely over
  MCP got the pointer and could not collect what it pointed at, which is worse than not
  advertising them. Both doors now call the same accessor, so there is one copy of each
  guide and no second rendering path to drift. Costs ~218 tokens of schema per session
  (2.5% of techne's MCP footprint — see #808 for the rest).


- **The research sandbox ships too — `techne agents install`** (#754). `techne guide
  investigating` tells an agent to do its fetching inside a subagent restricted to
  web-only tools. That instruction is **unenforceable** without a definition to name: an
  operator who lacks one reaches for a general research agent, which has a shell, and an
  agent that can both read attacker-controlled pages *and* write to the pattern store is
  one injection away from writing whatever a page tells it to. Afterwards nobody can
  separate agent-authored content from injection-authored content.

  So `research-readonly` now ships in the wheel and installs alongside the procedures.
  **This is the one place the guide-stub trick does not apply**: a procedure can say "run
  `techne guide <name>` and follow it" because an agent reads it at run time, but an agent
  definition is configuration the *harness* parses before any agent exists — the security
  property is the literal `tools: WebFetch, WebSearch` line. Point it at a guide and there
  is nothing left to enforce.

  `techne agents status` reports absent / unchanged / edited / **widened**, and widened is
  its own state on purpose: a drifted procedure gives worse research, while a drifted
  sandbox gives the research agent tools the procedure assumes it cannot have. An edited
  definition is kept rather than overwritten, the same discipline the skills installer
  uses — techne would rather tell you your copy diverged than silently restore its own.


- **The two research procedures come home, and an agent can give itself the skill** (#754).
  `investigate-url` and `survey-field` lived in a separate repo an operator had to know
  about and install. They now ship as `techne guide investigating` / `techne guide
  surveying`, alongside the operational procedures — `techne guide` lists all eleven.

  **The generated skill is a stub, not a copy.** The MCP instructions tell an agent that if
  its host supports skills it can write itself one whose entire body is *"run `techne guide
  <name>` and follow it"* — never the procedure itself. A copy is a second spelling that
  goes stale in silence; a stub re-reads the installed version on every run, so it cannot
  drift. That is what makes the external repo retirable rather than merely duplicated, and
  it works on every platform `init --platform` advertises rather than the one that has a
  command directory.

  **The drift was not hypothetical — the external copy was already wrong.** It told agents
  `architect search` cannot match a source URL because the FTS index covers name and body
  only. #430 fixed that in this same release: `patterns_fts` is `fts5(name, body,
  source_url)` and all three triggers populate it. An external procedure had been quietly
  disagreeing with the code for a release, and nothing failed. `search` still cannot see a
  `skip-with-flag` (no pattern row), which is what `dedup` is actually for, and that
  correction is now pinned by a test rather than left in prose.

- **`techne architect dedup` and `techne architect snapshot`** (#754). The two store
  operations the absorbed procedures need. `dedup` answers "has this URL been investigated"
  across **both** tables — patterns *and* investigations — because a `skip-with-flag`
  records a verdict with no pattern row, and that is precisely the set worth catching:
  someone already decided this was not worth lifting. It folds URL spellings (case, scheme,
  `www.`, trailing slash) while keeping sub-paths distinct, and exits 0 for seen, 1 for new.
  `snapshot` hashes pattern **content**, so the read-only-subagent backstop catches an
  in-place edit and not merely an insert or delete — the more dangerous write, since a
  poisoned body under an already-reviewed id reads as reviewed.


- **The guides ship with techne, and the agent is told they exist** (#754). techne's agent
  education is markdown — the `Agent: raise this unprompted` blocks are how an agent names a
  silent failure before the operator concludes something from it. None of it shipped: the
  wheel held code, two skills and two grammars, so every trigger reached agents working *on*
  techne and reached an installed user's agent never. An `AGENTS.md` row pointing at
  `docs/reflect-guide.md` resolved to nothing on a machine with the package and not the repo.

  Seven operator-facing docs now ride in the wheel (`techne/docs/`), and **`techne guide`**
  lists them while `techne guide <name>` prints one — raw markdown, because an agent is the
  expected caller. There is still exactly one copy of each file: `pyproject.toml`'s
  force-include maps them in at build time, and a source checkout falls back to the repo
  rather than vendoring a second copy that would drift. `docs/arch/` and `docs/proposals/`
  deliberately do not ship — they describe seams to people changing them and carry anchor
  shas that mean nothing outside a checkout.

  **The MCP instructions now teach the four domains.** That field is techne's one automatic
  channel into every connected agent's context, and it still described the pre-0.16.0
  product: it named `pattern_search` (the mechanism) and never mentioned the archivist, the
  librarian, `binding`, or how to fill anything. It now names all four domains, says every
  answer carries the binding it resolved to, and names each feeder — because an empty domain
  and an unpopulated one are indistinguishable in the result, so the zero is the agent's to
  explain when it appears.

- **pi is a first-class client** (#57). `integrations/pi/` ships the pi capture
  extension — the same three-lane bridge the opencode plugin has, on a better
  host: pi's in-process TypeScript lifecycle events (`tool_execution_end` →
  per-tool observation, `message_end` user-role → `UserPrompt`, `agent_settled`
  → `techne session-end`, `before_agent_start` → `techne context` appended to
  the system prompt). Sessions are stamped `platform: pi` and keyed by the
  session-file UUID, so a resumed pi session keeps its techne session across
  restarts. The first two clients proved the capture surface; pi is the third,
  and it needed no new surface — one `techne observe` path, three clients.

  The probe follows: `techne probe --runtime pi` (a `PiAdapter` on
  `pi --print` in a throwaway workspace with `--no-tools`, so the extension's
  push injection is the only channel — closing the whole class of pull paths
  at once instead of a denylist against a moving target). pi has no native
  memory, so `off-test` isolates the only techne path, as on opencode. Wired
  into the probe CLI, exported from `techne.probe`, and covered by the same
  unit tests the other adapters have plus a `@pytest.mark.probe` end-to-end.

  One lane the non-Claude-Code clients still lack: pi (like opencode) has no
  compaction checkpoint/restore — Claude Code's `PreCompact`/`PostCompact`
  hooks have no pi equivalent, so pi has no compaction handling at all.

- **Agent triggers are indexed, and the claims they rest on are tested** (#756, #757). Parts of
  the docs are written as *triggers* — conditions under which an agent should speak up
  unprompted, because techne's failures are overwhelmingly silent and a document that waits to
  be consulted is the wrong shape for them. Two things were missing when that convention
  shipped.

  **A trigger that lives only in a document fires only if that document was already open** —
  the failure the documents were meant to fix, one level up. `AGENTS.md` now carries a table of
  *conditions and links*: the condition must be in loaded context or it never fires, the
  explanation is fetched once it has. Enforced three ways — a document with a trigger block and
  no row fails, a row pointing at a missing section fails, and a row pointing at a document that
  no longer carries a block fails.

  **And the claims are tested rather than trusted.** Four bullets *look* like statements about
  SQLite and are not: they hold because techne opens its databases in WAL mode, one line in
  `core/database.py`. Absences ("`techne doctor` does not check this", "nothing archives
  transcripts") cannot be proved by grep, so the relevant *surface* is pinned — the registered
  check names, the whole command tree — and any change trips the test and forces a re-read. Each
  bullet names the test that witnesses it, both directions enforced.

- **The docs teach how to FILL each domain, not only how to ask it** (#753). The 0.16.0
  tutorial taught four names against four stores it never explained how to populate, which
  reads as broken on day one for exactly the audience the release is aimed at. `four-functions.md`
  gains a **Getting content in** section: per domain, the enable knob, the populate command,
  and where the filler lives when it is not in this repo.

  Two things it states that were true and undocumented. **Nothing in techne itself indexes
  transcripts** — `index-transcripts`' only in-repo caller is the CLI command, and a
  4%-coverage search answers exactly like a complete one, so a partial index is invisible in
  the result. (Deployments can wire a hook as a shell string, which no import graph sees;
  techne#704 forced that same correction on `vectors backfill`.) And
  **the archivist's write path**: bound `local` it *is* your own knowledge, so `techne_store`
  fills it and there is no separate import.

- **`docs/modules.md` was missing the archivist entirely** (#753). It introduced three of the
  four names *as database files* — the layout framing this release exists to demote them out
  of — and `grep -ci archivist` returned **0**. All four are now headed by function with the
  db file as subtitle, matching the README's treatment.

- **`techne backup --keep N` — retention** (#742). `backup` wrote a full set every run
  and pruned nothing, which made it safe to run by hand and unsafe to put on a timer: a
  nightly job with no ceiling is a disk-filling cron job dressed as a backup story.
  `--keep N` deletes all but the newest N sets in the destination, **after** the new one
  lands, ordered by each set's manifest `created_at` rather than by mtime — reading a
  backup directory rewrites mtimes and would evict a recent set because something *read*
  it. A directory is only a set if it carries a manifest listing databases, the same
  identity test `restore` applies; anything else under `backups/` is left alone.

  **A run that captured nothing exits 4.** With no domain database present — a typo in
  `TECHNE_HOME`, a timer installed before `techne init`, a service running as the wrong
  user — `backup` used to write an empty set, print `backed up [(none)]` and exit `0`,
  nightly, forever. A green light and no backups, discovered on the day someone needs to
  restore. Separate from `1` because nothing failed: every step did what it was asked and
  there was nothing to capture.

  **Retention failure exits 3**, distinct from a failed backup (`1`) and from success
  (`0`). A timer surfaces exit status and nothing else, so at `0` a permanently broken
  prune reads green while the disk fills — the failure `--keep` exists to prevent, one
  level up. A set that cannot be *ordered* — a non-canonical or future `created_at`, or a
  symlink — is never pruned and is reported the same way, because a set retention will
  never reclaim is the same problem to the operator whether the cause was a failed delete
  or a refused one.

  **This also puts `restore`'s safety net on a clock.** `techne restore` snapshots the
  current state first and prints `prior state snapshotted at: <path>`, which reads as
  durable. It is an ordinary set under `backups/` — deliberately, so retention can reclaim
  it — so `--keep N` counts it as one of the N and it ages out like any other. If you want
  one kept, move it out of `backups/`.

- **Scheduling that ships** (#742). `deploy/techne-backup.{timer,service}` and
  `deploy/ai.tenish.techne-backup.plist`, both passing `--keep 14`, with install steps in
  `docs/deployment.md`. The systemd path documents `loginctl enable-linger` as required
  and says plainly that the verification step will not tell you it is missing — without
  lingering the user manager stops at logout and the timer never fires, while
  `list-timers` shows it healthy for as long as you stay logged in.
- **`techne architect add` — the feeder that ships** (#754). techne teaches four domain
  names, and on a fresh install the architect — *"what is worth borrowing"* — had no way in
  that came with the tool. `ingest` takes a markdown corpus, and when this landed the
  procedures that actually fill the store lived in a separate repo, so an operator installed
  techne, read the tutorial, learned to ask the architect, and the thing that fills it
  shipped from somewhere we never mentioned. **Later in this same release those procedures
  came home** — see *The two research procedures come home* below; the paragraph that
  follows records why `add` was built as a command first, which is still why it is a
  command.

  `add` takes the fields directly — `--name`, `--source`, `--surveyed`, and a body from
  `--body`, `--body-file` or stdin — and writes through the same store API `ingest` uses
  after parsing. **A command rather than a bundled skill**, because `techne init
  --platform` advertises five platforms and a skill installs into Claude Code's
  `commands/` and nowhere else — a CLI command works on all five, which is the same
  reasoning that later made `techne guide` the delivery mechanism for the procedures
  themselves. `add` is the smallest thing that gets a pattern in; it was never meant to be
  the research procedure, and it still is not.

- **`techne architect ingest` says which sections it dropped** (#754). A section missing
  its `- **Source**:` line was skipped with a log line nobody sees, so the command printed
  `"patterns": 0` and exited 0 on a file the operator had just written by hand. The bold
  markers are load-bearing — `- Source:` parses as body text — and that is not guessable,
  so the realistic first attempt failed in the one way that is hardest to notice: silently,
  shaped exactly like success. Skipped sections are now reported with the reason and the
  missing marker named, and a run that ingested nothing points at `architect add`.

- **The four knowledge domains are addressable by name** (#724). `archivist_search`,
  `librarian_search`, `architect_search` and `codex_search` — the archivist, librarian,
  architect and codex answer to their own names, in one envelope carrying the `binding`
  each resolved to.

  The recall ladder has answered across all four for months; what was missing was
  *address*. The librarian had no tool at all (a test asserted its absence) and the
  architect answered only to `pattern_search`, the mechanism rather than the function —
  so operators learned `techne_*`, the one vocabulary that does not survive the move to
  liminal and cabal.

  **Every answer says what it resolved to**, which is the point rather than a nicety:
  the same call reports `local` today and would report `store` — or whatever a cabal
  persona or a deployed service resolves to later — without the caller changing. An
  unreadable domain returns its *reason*, not an empty list.

  `techne_recall` is unchanged and remains the default for "what do we know about X";
  it escalates across all four rungs. These calls are for when you already know which
  function you want. No liminal dependency is added — dot-namespacing stays liminal's
  job.


- **`techne bindings` — one question, four domains** (#552 part 4). What is the
  archivist / librarian / architect / codex bound to, and can I reach it, asked
  identically of each and answered in one shape. This is the "same feel" ask, answered
  on the axis the sibling projects answer it on: liminal's `Target` makes transport a
  field and never changes the call shape, and cabal already ships `patterns`, `code`,
  `documents` and `archivist` as peer handlers. Neither fuses its domains into one
  query.

  **It deliberately does not make patterns and codex recall rungs**, which is what #552
  part 4 literally proposes. Measured first: pattern FTS falls back to OR and answers
  "archivist binding" with "Late-binding task definitions"; the codex index was 83 of
  249 files stale as of 2026-08-21 (a snapshot — a reindex moves it) and returned zero
  hits for a symbol merged that day; the durable finding is dilution, 45% of hits on
  realistic topics being test symbols. A rung that returns noise on every recall
  is worse than no rung, and open-sourcing freezes the response shape. Revisit when
  #550 and codex freshness make them answerable — `test_the_recall_ladder_is_unchanged`
  is the guard, and says so.

- **`techne archivist query` and the `techne_archivist_query` MCP tool** (#554) — a
  guarded read-only SQL surface over team memory. The Archivist could hand you things
  but you could not interrogate it: `archivist.db` was reachable only through
  federation fan-out inside recall, which pushes results at you for a topic string.
  "Which decisions touch cabal?", "what was written last week that superseded
  something?", "who wrote what?" are plain reads, and every one was impossible without
  opening the sqlite file by hand. Pairs with #553 — a query surface without writer
  provenance answers *what do we know* but never *who says so*.

  **Distilled only**: `knowledge`, `knowledge_deps`, `knowledge_fts`. The raw
  observation tier is not readable here, deliberately and separately from whatever the
  distilled-only egress decision concludes. Enforced by SQLite itself, not by
  convention — a `?mode=ro` connection, a `set_authorizer` callback checked during
  prepare (which denies `ATTACH`, because a read-only connection can otherwise attach
  and read the *personal* store out through the team surface), a work budget that
  stops a runaway query, and one statement per call. No write verbs on this path ever,
  for any caller.

  Three outcomes are distinguishable on purpose: `rows`, `no-rows` (the query ran and
  matched nothing — an answer), and `store-absent` (no team store is bound — *not*
  "the team knows nothing"). Collapsing the third is how a standalone install comes to
  report that the team knows nothing; it is the same failure the recall ladder's
  `loose-match-only` exists to prevent.
- **`techne custodian run|status` — the emit→ingest rails can run headless** (#555). The
  passes that are actually custodial — dedup, merge/supersede, the faithfulness check on
  rewrites, contradiction classification — all sat on the resident path, which requires an
  agent sitting there to answer emitted prompts. A custodian that only works while you are
  watching is not a custodian. Rails now take an optional model binding and run unattended;
  unbound (the default) every rail behaves exactly as before.

  Provider-agnostic by construction: the binding is a `str -> str` callable resolved through
  the same `_make_generate_fn` as the operator-model path, so any LiteLLM identifier works —
  local Ollama, Vertex via ADC, a proxy. The credential question lives in config, not in the
  code path.


- **Writer provenance on federated knowledge** (#553) — a shared Archivist could tell you
  *what the team knows* and never *who says so*. Knowledge rows now carry `written_by`,
  `origin_source` and `origin_session`, stamped **in the same INSERT** as the row rather
  than by a follow-up UPDATE, so a crash between the two cannot leave a row that claims an
  author it does not have. Takes core schema to **v25**. Pairs with #554, which is the
  query surface over the same store.

  **Existing rows are deliberately NOT backfilled, and the two entries read together
  promise more than that.** #554 ships in this same release and answers *"who says so"* —
  but for every row written before the upgrade the answer is `NULL`, permanently. The
  local `source` was discarded at write time and was never stored, so filling it with the
  only instance we have ever run would encode a guess as a fact, in the one table whose
  job is to record who said what. New writes are attributed; old ones cannot be.

  **And it needs `instance_name` set to do anything.** Unset — which is the default —
  every federated row lands unattributed. `techne doctor` reports that as STALE with a
  remedy rather than leaving it silent; the qualified `<agent>@<cabal>` form is in the
  config reference.

- **`pattern_outcomes` gets a write path** (#551, #426) — `techne architect realized` /
  `outcomes` / `unrealized`, plus `record_outcome()` and `unrealized_patterns()`. The
  code had been written since 2026-07-28 and blocked on a version number: #426 shipped
  `ARCHITECT_VERSION = 4` while main had since taken v4 for #674's liminal
  canonicalisation. **Both steps are kept at their own rungs, taking `architect.db`
  to v5.** Verified on a copy of the live store rather than on fixtures — 520 patterns
  preserved, and `pattern_outcomes` compared column by column against the 11 rows
  already there, since `IF NOT EXISTS` would otherwise mask a divergent shape.

- **The recall ladder states itself** (#552, #695) — `techne_recall` now reports **every
  rung** on every call, named and in order, and each row carries the rung it came from. A
  rung that is **unconfigured** stays distinct from one that is **empty**: "I have no
  librarian" and "the librarian had nothing" are different answers, and collapsing them is
  how an absent result becomes indistinguishable from a working one. New `core/tiers.py`;
  a response-shape change to the most-used entry point.

- **A running MCP server notices when the code moved underneath it** (#696 part B). A
  `stat` of `<runtime>/.git/logs/HEAD` behind a 30s TTL, then a walk of the `techne.*`
  entries already in `sys.modules`. The observation it rests on: *the process that needs
  to report is the one that already knows* — answering "am I running the code on disk"
  from outside costs `ps` plus git; from inside it costs one `stat`.

- **SessionStart learns what a live server already knows** (#696 part A). A server
  publishes its finding as one small JSON file per pid under `runtime-state/`, written
  whole then renamed, and **only when there is a real finding** — so the presence of a
  file means something is wrong rather than merely that a server ran. `check_runtime_torn`
  reads them with a directory listing and `os.kill(pid, 0)` to drop the dead, which keeps
  it an *ordinary* check and lets it reach the banner through the path that already
  existed.


- **The librarian answers to its own name** (#744) — `techne librarian`. Three of the four
  domains were addressable by name and the librarian was `techne library`, which is the
  *storage* word: it names the document store, and that is the layout framing this release
  demotes the four names out of. An **alias, not a rename** — every existing script keeps
  working — and resolved in `get_command` rather than registered twice, because a `--help`
  listing offering two words for one domain teaches the ambiguity the fix exists to remove.

### Changed

- **The architect borrows; it does not steal.** The domain's tagline was *"what is worth
  stealing"* in thirteen places, including two in-product MCP tool descriptions
  (`architect_search`, `pattern_search`) and the schema comment that ships in every user's
  `sqlite_master`. Now *"what is worth borrowing"*, and `pattern_search` describes patterns
  as **borrowed** from other projects rather than "lifted". A wording change, listed because
  it is visible in tool descriptions an integrator may have pinned.


- **BREAKING (0.16.0): `techne backup --out DIR` now names the backups *root*, not the
  set** (#742). The version is stated here rather than left to the heading because
  `README.md` and `docs/db-per-domain.md` both say "changed in 0.16.0", and until this
  section is dated at release an operator who greps the CHANGELOG for `0.16.0` would
  otherwise find nothing (Prudence, #755 round 3 Nit 6). `--out`'s help text has always called it the root; the code treated the path
  you passed *as* the timestamped set. The two disagreed for as long as the flag has
  existed, and it stayed harmless only because nothing else read that path — until
  `--keep` did.

  **If you script `backup --out X` followed by `restore X`, that now needs
  `restore X/backup-<stamp>`.** `--out` yields a directory containing timestamped sets;
  `restore` takes one set, as it always has.

  The mismatch is what turned `--keep` into a deletion: `--keep` prunes
  `res.dest.parent`, which under the old reading was the directory *above* the one you
  named. `backup --out ~/techne-backup --keep 1` was reproduced deleting an unrelated
  sibling project directory that happened to contain a `manifest.json`. The same
  mismatch also made `--keep` useless where it was aimed — every `--out` run wrote to
  the same directory, so there was never more than one set there to prune. Fixing the
  behaviour to match the documentation was the choice over bolting a guard onto
  `--keep`, because a flag whose documented meaning and behaviour disagree is what
  produced the deletion.

- **The archivist is a call, not a deployment mode** (#552). `standalone` was a *lane
  selector*: it forked both halves of the loop — write at
  `lifecycle._thread_summary_target`, read at `archivist_connection` — so a default
  install neither read nor wrote an archivist, which is the opposite of the intent.
  Six call sites each decided for themselves what the archivist was, and the read path
  and write path could disagree.

  One function decides now: `core.archivist.archivist_binding(config)` returns
  `local` / `store` / `absent`, and read, write, the query surface and the recall
  ladder all ask it. The vocabulary is not new — #695 introduced it as a *reporting*
  concept derived inside `_techne_recall`; this gives it authority. `standalone` is
  deprecated but still read, and still means what it meant: it describes a **topology**
  ("I am a lone agent"), not an absence — and a lone agent still makes the archivist
  call. A named `archivist_db_path` now outranks it, so the previously contradictory
  config (a path set while `standalone: true`) does what it says.

- **`local` resolves to the knowledge you already have — it does not copy.** Measured:
  the local archivist was a 98.9% mirror of local knowledge (1,104 live rows against
  1,109, 1,105 byte-identical by content). #695 measured that mirror and deliberately
  stopped *reading* it; writing it is pointless by the same argument and was not free —
  `push_knowledge_to_archivist`'s own docstring records the copy as **5.15s of a 5.52s
  session teardown** (760 enqueues, 2,306 SQLite connections) against a 5.0s hook
  budget it then blew. On the author's box this change takes that teardown from ~1,100
  enqueues to **0**. Nothing depended on the copy: the egress path reads local
  `knowledge`, never the mirror.

### Fixed

- **The frontmatter splitter treated an indented `---` as a document boundary** (#812,
  Prudence r2 on #807). The same `strip()`-discards-indentation root cause as the round-1
  finding, moved one function over — and it failed both ways. A legitimate definition whose
  `description:` block contains a `---` rule got cut short, so `agents status` reported
  **widened** over a good sandbox, which turns the loud path into noise and makes a real
  widening read like more of the same. And with the grant re-declared after that inner
  rule, the truncated slice saw the narrow grant while a compliant parser saw the wide one.
  The fence must now sit at column 0, reusing the shape `scripts/check_arch_cards.py`
  already had — which also brings BOM tolerance the hand-rolled loop lacked.

- **`architect dedup` folded too much of a GitHub path** (#811). Case-folding is a property
  of the host, but only `owner/repo` lives in GitHub's namespace; everything deeper is
  served out of git, where tree paths and refs are byte-exact. Folding the whole path
  relocated the very defect it fixed — `blob/main/Setup.py` and `blob/main/setup.py` became
  one entry, as did two differently-cased refs, and the query string was swept in with them.
  The fold is now bounded to the two segments the measurement actually covered.

- **`architect snapshot` omitted `pattern_outcomes`** (#811). The exclusion argument holds
  for the derived graph tables — `backfill_graph` rebuilds those from covered content — and
  does not hold here: `record_outcome` writes this table with a real INSERT, exposed as
  `techne architect realized`. A recorded outcome left the digest unchanged.

- **`agents install` dropped its ownership record without a prior `init`** (#812). The
  manifest lives in `techne.db` and its writer early-returned when the store was absent —
  and nothing requires `techne init` first. On that path `agents remove` then refused to
  remove techne's own byte-identical file, telling the operator they had modified it, and
  the `outdated` reporting added earlier in this release could never fire at all. Install
  now ensures the store exists.


- **The sandbox verifier failed open on four frontmatter shapes** (#812, Prudence r1 on
  #807). `_sandbox_intact` scanned lines instead of parsing the document, so a `tools:`
  key nested under `metadata:`, a `tools:` line in the markdown **body**, a decoy inside a
  `description: |` block scalar, and a duplicate `tools:` key all reported **sandbox
  intact** over a definition granting a shell — while the YAML sequence spelling reported
  a false *widened*. Two of those need no adversary: an operator restructuring their own
  frontmatter, or documenting the tool line in prose, got a green report from the command
  `AGENTS.md` tells an agent to check before investigating. It now slices the frontmatter,
  parses it, and compares the grant as a **set** against what a YAML parser resolves —
  which is what the harness reads, rather than a string that resembles it.

- **`techne agents status` called our upgrade your edit** (#812). It loaded the ownership
  manifest and never read it, so a file byte-identical to what techne last wrote — the
  state of every install that upgrades without re-running `agents install` — reported as
  `kept-user-copy`. Now reports `outdated`, using the comparison `register_agents` already
  made. `status` also names the directory it is talking about, which matters on a platform
  where `~/.claude/agents` is not the obvious answer.

- **`architect snapshot` covered 4 of 15 columns and no investigations at all** (#811).
  The digest is read for its converse — *no difference means nothing wrote* — so every
  uncovered column was a write the operator was told did not happen. It missed
  `status → superseded`, which is a soft delete that leaves the row out of search while
  every hashed column stays identical, and missed a whole new `skip-with-flag`
  investigation, the population `dedup` exists to protect. Now digests every column of
  both tables; the derived graph stays out by design and the CLI help says so.
  *(Superseded within this release — see the `pattern_outcomes` entry above: the
  digest covers three tables, not two.)*

- **`architect dedup` folded path case and could suppress a real investigation** (#811).
  Lowercasing the whole URL made `wiki/Python` and `wiki/python` one entry, so `dedup`
  exited 0 and printed "Seen before" for a page nobody had surveyed. Case-folding is now a
  property of the **host**, established by measurement rather than assumption: GitHub
  returns 200 for both casings of a repo path, while Wikipedia returns 301 for the
  lowercase variant — a different URL that redirects. A fragment terminates the URL and a
  `.git` suffix folds; query strings deliberately do not, since they can select content.


- **`techne architect add` stamped a stranger's patterns with one of our persona names**
  (#754). `--surveyed-by` defaulted to `"raven"`, hardcoded at the CLI, with three more
  `"raven"` defaults in `core/patterns.py` and a schema comment listing `raven | rook |
  architect` that shipped in every user's `sqlite_master`. `core/architect.py` called the
  field "persona-agnostic", which was true of the schema and false of the default.

  Not cosmetic: `investigations` is `UNIQUE(url, surveyed_by, lens)`, so the value is part of
  row identity — which is also why the fix is a **config key** rather than a changed literal.
  Moving the default outright would mint a second investigation for every URL an existing
  corpus already held. `architect.surveyed_by` / `architect.lens` now default to `operator` /
  `infra`, and an operator with history sets them once to what their store already uses.
  `ingest` reads the same values as `add`, because the two feeders writing different halves
  of that key is how one URL becomes two rows — a regression the existing
  `test_add_and_ingest_agree_on_one_investigation_per_url` caught during this change.


- **A session's threads accumulated one superseded partition per re-detection** (#646).
  Heuristic thread ids derive from `bucket_hash(session_id, first_obs_id, boundary_signal)`,
  and `run_session_end` — which fires once per turn — deleted the boundary table before
  re-saving but never the thread table. A shifted boundary therefore minted a *new* id, so
  the insert never conflicted under any verb, and nothing removed the row it replaced. Measured on a real store: **86 rows in `threads`
  against 51 threads in the current detection**.

  Reflection read the two apart. `load_session_for_reflection` re-detects from scratch, so the
  reflection queue held ids no current detection produces — real debt whose prompt is
  impossible, which is what techne#646 had been reporting. On that store **26 of 57 queued
  threads (46%) could never be drained**, so the SessionStart banner overstated owed work by
  almost half. No memory was lost: every current thread above the observation floor had already
  been reflected.

  `lifecycle.persist_detection` is now the single replace-and-restore path, called by both
  `run_session_end` and `redetect_threads` — they had separate implementations that drifted on
  exactly the bookkeeping half. **`techne reflect reconcile`** (new, dry-run by default) retires
  the queue rows of partitions that no longer exist and reports what it left alone.

- **That replace then broke `run_session_end` outright on any store with history** (#797).
  Four columns reference `threads(id)` and only `reflection_queue` cascades; deleting a
  session's threads raised `IntegrityError: FOREIGN KEY constraint failed` as soon as another
  session's thread lineage or a knowledge candidate pointed at them — 1249 and 547 such rows
  respectively on the store where it was found. The hook caught it and exited 0, so nothing
  crashed and observations kept flowing, but per affected session the thread write, the
  heuristic summary and the debt enqueue were all silently lost.

  Replaced by a **prune**: only threads that are absent from the detection about to be saved,
  unreferenced by any of the three NO ACTION columns, and not carrying a paid queue row. Referenced
  threads are history and stay. `save_threads` became a single upsert at the same time, which
  fixed a second defect — under `INSERT OR IGNORE` a thread whose id was stable had its
  `label`, `summary` and `resume_points` frozen at the first detection of the session, so
  `techne_resume` served the opening minutes of a long session indefinitely. The legacy
  `INSERT OR REPLACE` path carried the mirror defect — it is DELETE+INSERT, and with
  `recursive_triggers` off the implicit delete never fired `threads_ad`, so the FTS shadow kept
  indexing the old label. One upsert closes both.

- **`architect ingest` blamed the heading when a no-lift bullet was malformed** (#787). A bullet
  under `## Surveyed (no patterns worth saving)` that was one word wrong — `— RE-SURVEYED
  2026-08-26 —` for `— surveyed 2026-08-26 —` — was dropped without a trace. #754 and #758 had
  already made the command speak up when nothing lands, but only pattern *sections* recorded a
  reason, so `skipped` stayed empty and the command fell into its heading-level branch:

  > no pattern sections found in x.md — sections must start with `## ` (two hashes) at the top level.

  Said about a file whose heading was exactly that. **"nothing was ingested" still fired**, so no
  write was lost silently — the cost was being sent to fix something that is not broken, which is
  the failure `_parse_pattern_body` already guards against for misplaced markers and the no-lift
  path never got.

  The bullet now reports itself, quoted back with the required form. The predicate is **a bullet
  whose URL comes first**, optionally decorated — bare, `<angle-bracketed>`, `[link-wrapped](…)`,
  or wrapped in `` ` ``/`*`/`_`/`~` — because each is a spelling of a genuinely malformed entry.
  Anchoring on the URL's *position* rather than matching a URL anywhere in the line matters more
  than it looks: `architect ingest` gates its heading-level advice on `if not skipped`, so a
  bullet wrongly flagged *suppresses* a correct diagnosis.

  **Two limits, stated plainly because the alternative is a wrong accusation you were not
  warned about.** The decoration list is *not* exhaustive — a quoted or parenthesised URL is
  still not reported, and an uncovered spelling falls back to the pre-fix behaviour, which is
  the misdiagnosis this entry is about. And a bullet whose *first* token is a link is reported
  even when it is prose: `- [the tracker](…) explains why.` is structurally identical to
  `- [foo](…) — surveyed … — r` up to that token, so no prefix rule separates them. Link-*later*
  prose stays out — `- Context lives in [the notes](…)`, `- [ ]` / `- [x]` task items — as do
  lines inside a fenced block, so a "what not to write" counter-example is not accused of being
  the thing it warns about.

  **Internal API change**: `SkippedSection.heading` is now `.label`, and the field gained a `kind`
  (`"section"` / `"no-lift bullet"`). `ingest_patterns_md`'s `skipped` dicts carry `label` + `kind`
  instead of `heading`. A key named `heading` holding a bullet line is a field whose name is a lie.
  The stderr line is unchanged for pattern sections — `kind` renders as `section`, so it still
  reads `skipped section "My pattern": …`.

- **A hand-authored manifest could name a database outside the backup set** (#782).
  `read_manifest` accepted `{"file": "/etc/passwd"}` and `{"databases": {"../../evil": {}}}`,
  validated the set as complete, and `restore_all_dbs` then staged a file from outside the set
  over a live database. The first works because `src_dir / "/etc/passwd"` **discards `src_dir`
  entirely** — pathlib semantics for an absolute right-hand side, not a bug in the join. The
  second needs no `file` key at all: with it absent the name falls back to `f"{domain}.db"`, so
  the domain key alone carries the traversal.

  `_manifest_db_filename` — the single resolver all three readers go through since #759 — now
  resolves the name against the set directory and refuses anything that lands outside it, so
  `read_manifest`, `_classify_backup_set` and `restore_all_dbs` are covered by one predicate.
  The check **resolves and compares** rather than matching on `".."`: a plain basename inside
  the set can still be a symlink pointing out of it, which no spelling check sees.

  **Scope, stated plainly.** Nothing techne writes produces either shape — the writer always
  emits `{"file": <plain basename>, "size_bytes": <int>}` — so this needs a manifest that was
  hand-edited or supplied by someone else. It is not a privilege escalation either: restore
  runs as the operator and reads what the operator could already read. What it fixes is a live
  database being overwritten from an unexpected source while the manifest claims the set is
  self-contained, which is the promise `docs/deployment.md` makes for restore.

  **Some refusals changed their stated cause.** Named as a class rather than counted, because
  the count was wrong: three distinct inputs now report differently, and they do not all come
  from the same guard.

  - A `file` of `".."` or `"../"` names the set's *parent*, so it is refused as a malformed
    entry ("escapes the backup set") rather than as a missing file ("backup set is incomplete").
  - A `file` that reaches outside the set by any other route — an absolute path, a traversal
    part-way along, a basename that is a symlink out — refuses the same way, where before it
    was either accepted or reported as missing.
  - A `file` containing a **NUL byte** now refuses with "contains a NUL byte" **on the
    restore path**. This one is not a containment refusal at all: `Path.is_file()` silently
    swallows the `ValueError` a NUL raises, so on previous releases such a set reported
    "backup set is incomplete". *Retention still shows the generic cause* — `_classify_backup_set`
    records the domain and discards the reason, so `techne backup --keep` reports "lists
    databases in a shape techne cannot read" and does not name the NUL. Both refuse; only one
    says why. Naming it there waits on #785.

  **The second bullet is the one that was not merely mislabelled.** For the first and third,
  the set was already refused and never took a retention slot — `".."` and `"../"` name a
  directory and a NUL makes `is_file()` answer false, so both read as missing — and what
  changed is only the stated cause. But an escaping name that resolves to a file that *exists*
  was **accepted**: the set validated as complete, took a retention slot, and could evict a
  genuine backup. That is the bug this entry fixes, not a wording change. The directory
  spellings that resolve to the set directory *itself* (`"."`, `"./"`, `".//"`, `"./."`) are
  not escapes and refuse exactly as before.

- **`architect export` → `architect ingest` accreted a near-duplicate every cycle** (#770).
  `export_patterns_md` writes a `---` separator after each entry and the parser absorbed it
  into that entry's body. A pattern id is a content hash of `(name, source_url, body)`, so
  every round trip minted a **new** id and the store grew — 1 → 2 → 3 → 4 patterns for one
  pattern, each body carrying one more trailing rule than the last. Anyone who round-tripped
  their corpus to reformat it, review it, or move it between machines grew it instead.

  **What changes for an existing store, because this is not invisible.** The strip removes
  *all* trailing rules rather than one, which makes the fix convergent: a damaged body
  normalises back to the clean body and its **original** id. Two consequences follow, and
  both are yours to expect rather than to discover:

  - **Ids change.** For a corpus round-tripped before this release, the next
    `architect ingest` produces different ids than the store holds today — the *original*
    ones, which is the point, but different from what is there now.
  - **A damaged store gains one row, once.** The clean pattern appears alongside the
    accreted ones; the accreted rows are not deleted. So `architect ingest` reports one more
    pattern than expected, which is this project's signature failure shape — it looks like
    success. It stops there; the growth does not resume.

  **There is no automatic cleanup, deliberately.** Deciding which of several near-identical
  rows is canonical is a curation judgment, and silently deleting rows from a store whose
  whole job is to remember is the wrong default. `techne architect tag <id> --superseded-by
  <clean-id>` is the tool — it stamps `status`/`superseded_by`/`absorbed` atomically and
  writes a resolved `supersedes` edge, so the history stays legible instead of vanishing.

  A trailing **setext heading underline** is not mistaken for a separator: `export` always
  writes the rule after a blank line, while a setext underline is attached to the line above,
  so only the former is stripped.

- **A malformed backup manifest is refused, not tracebacked** (#759). `read_manifest` decides
  whether a directory is a restorable backup set and its contract is to raise `RestoreError`.
  For **fifteen** shapes it raised `AttributeError` or `TypeError` instead of
  `RestoreError` — every non-object shape at the top level and at each `databases`
  entry, plus every non-string `file`. (Fifteen by JSON *kind*: `number` counts once,
  not `int` and `float`. That count has been got wrong twice.) A raw stack trace
  reaching the operator **mid-restore**, which is when it is least useful and most alarming.
  `cli.py` catches `RestoreError` and nothing else, so none of these were handled.

  Two levels were unguarded, and the second one mattered more. `_classify_backup_set` carried
  its own copy of the same `meta.get("file", ...)` expression, so a non-string `file` raised
  out of `prune_backup_sets` **entirely** — retention died for *every* set rather than the
  unreadable one, and a scheduled `techne backup --keep N` reported it as exit `1`, which
  `docs/deployment.md` defines as *"the backup itself failed"*, for a backup that had in fact
  succeeded. On the only channel a timer has, that named the wrong half of the run and paged
  someone for a failure that did not happen. It is exit `3` and a named set now.
  Measured: with `--keep 0` and one bad set present, a perfectly good set was left on disk.

  **One thing is now refused that was previously accepted**, rather than merely
  raising a different exception: a `file` naming the set directory instead of a
  database. `{"file": ""}` was accepted before this, because `src_dir / ""` *is*
  the set directory and it exists — so a set whose "database file" was a folder
  passed validation as restorable.

  That mattered beyond a confusing restore. Such a set took a **retention slot**:
  at `--keep 1` it counted as the one set to keep, the genuine backup was deleted,
  and nothing was reported on the `failed` channel — a real backup destroyed by a
  run that exited as a success.

  `docs/deployment.md`'s exit-3 table is updated to match — **both** rows, because
  the two guards split the spellings between them. The row for `lists a database
  file that is not there` now names the directory case as a cause and tells the
  operator to check the manifest's `file` values before hunting for a deleted file,
  because the two causes have opposite remedies. The row for `lists databases in a
  shape techne cannot read` gains the empty/whitespace `file` case, and its remedy
  no longer leads with salvage-by-hand: for that cause the databases may be fine and
  the fix is one string in `manifest.json`. That row also warns that the phrase is
  reported *ahead of* the missing-file check and therefore masks one, so "the
  manifest is wrong" must not be read as "the set is intact".

  Three guards cover it now, and they are worth naming separately in a release whose
  subject is guards drifting between call sites. `""` and whitespace-only are
  refused in the shared resolver, as a malformed `file` value. `..` and `../` are
  refused in the resolver too — see the #782 entry above; they name the set's parent,
  so containment reaches them before any reader does. `.`, `./`, `.//` and `./.`
  reach the readers and are refused there, by asking the question directly — *is this
  a file?* — rather than by matching spellings, which is what an earlier attempt did
  and what left four of those six directory spellings open.

  All three readers now resolve entries through one shared helper, so a guard cannot be added to
  one and missed by the others again. An unreadable set is **reported** through retention's
  existing `failed` channel and never pruned, and `restore` refuses the same set — the two
  agree by contract rather than by coincidence.

- **An injected `Config` was silently discarded** (#766). `main` called
  `ctx.ensure_object(dict)` — which reads as "honour what the caller passed in" — and then
  unconditionally overwrote `ctx.obj["config"]`. So `CliRunner(...).invoke(main, args,
  obj={"config": cfg})` ran against the **ambient store**, with no error and exit 0.

  Found the hard way rather than by inspection: a reviewer believed they were scoped to a temp
  store and four `techne backup` runs went against a live one under a mutation setting
  `--keep 1`. Nothing was lost, verified independently. It matters most for exactly the commands
  where it is least recoverable — `backup` writes, `--keep` deletes, `restore` overwrites — and
  a test author who believes they are sandboxed is the person most likely to exercise a
  destructive path. Nothing caught it because every scoped-config test patches `Config.load`,
  which works *precisely because* it patches the call that was doing the overwriting.

- **A codex read from inside a `git worktree` resolves to the primary index** (#719,
  tenishai/agents#10). `db-per-repo` silently meant *db-per-checkout*: a linked worktree
  has its own `.git`, so every query from a review worktree or a worktree-per-session
  checkout answered "no codex index for the active repo". Measured on a review agent's
  own history: its impact pass ran on 6% of reviews, because its instructions correctly
  say to fall back to grep when the codex returns nothing — it was following them
  against a tool pointed at a path that had never been indexed. Read path only; the
  write path still resolves to the worktree, or a PR branch's symbols would land in the
  base graph other sessions query.

- **`codex_blast_radius` no longer exceeds the response token ceiling** (#722). A hot
  symbol returned 500 nodes / ~175,000 characters — about 44k tokens — and came back as
  an error rather than an impact set. Capped at 50 closest-by-depth, with `returned`,
  `walked` and `truncated` stating what was cut, including the *depth* of the cut
  ("450 more at depth 1" is the difference between a truncated radius you can reason
  about and one you cannot). It also reports `walked` as a **floor** when the graph walk
  hits its own 500-node bound, which had always been indistinguishable from a complete
  walk.

- **Raw operational memory no longer crosses the team seam** (#552). A thread summary
  is raw; the Archivist federates only the distilled, but nothing enforced that at
  write time, so `sink:archivist` carried raw summaries across for every non-standalone
  instance. Measured before the change: `archivist.db` held **17,819 observations
  against 1,104 knowledge rows — raw was 94% of the team store.** Summaries now always
  take the sync lane. The sink is *not* deleted and registration is *unconditional*:
  11,442 delivered rows sit in that lane on the author's box, and an unregistered
  target is what `outbox prune-orphans` marks permanently dead. The gate belongs on
  enqueue, not on registration.

  **Operator-visible after upgrade:** `techne doctor` grows an
  `outbox:sink:archivist — dormant — retired by techne#552` line on any box that ran
  `standalone: false`. That line is the fix, not a symptom: the lane's delivered rows are
  kept readable on purpose, and marking it retired is what stops it reporting a dead
  producer forever with no operator action able to clear it.

- **Lane retirement follows the binding, not just the release** (#715). The same false
  red existed one lane over: `sink:archivist_knowledge` is fed only under a `store`
  binding, so under `local` — the new default — it has no producer either, but it is not
  globally retired and a static set could not express it. Measured on the author's box
  minutes after #714 merged: 2,208 delivered rows, `pending=0`, due to report `STALE`
  seven days later. `retired_producer_targets(config)` is a predicate for that reason;
  retirement-by-release is the special case where the answer is the same for every
  config. The `pending == 0` gate is kept — retirement suppresses the *producer*
  question and nothing else, so an undrained backlog still reports.

  The same question, asked once per *binding* rather than once per lane, turns up a
  **third**: `sync:thread_summary`'s only producer is gated on `sync.adapter != "noop"`,
  and `noop` is the default. Including it also resolves a self-contradiction the check
  carried — the declared-but-never-enqueued branch asked "does this lane have a
  producer" and the discovered-rows loop did not, so the *same lane under the same
  config* reported `dormant/ok` with zero rows and `stale/ok=False` with three drained
  ones. Both branches now answer from one predicate.

- **The automatic session-end reflect rail had never produced a single candidate** (#555).
  Not since a regression — *ever*, since 2026-06-20. Measured on the author's store: all 444
  candidates carry `model_used='agent-assisted'` (the resident route); zero came from the
  operator-model path, which was raising `AuthenticationError` on every session because
  `config.model` is an Anthropic id and the operator holds no Console API key.
  `reflect_on_session` caught it, incremented a counter nobody reads, and let teardown
  proceed — for two months.

  It stayed invisible because `check_maintenance_rails` reported the rail **live** the whole
  time: that check keys on `knowledge_candidates.origin`, the rail's *output*, and `origin`
  does not record which of the two producers wrote the row. The liveness subsystem built to
  stop "an absent result is indistinguishable from a working one" contained that exact hole.

  Every automatic and headless run now records `rail:<name>:last_run` — ok / partial / error /
  noop, with the exception *type*, because `AuthenticationError` says *needs a credential it
  was never given* where a generic failure says only *broken*. New `check_model_bound_rails`
  reads those markers. No marker reads dormant, not broken: an install that only uses the
  resident route has not opted in and must not be nagged.

- **Candidate rows now name the model that judged them** (#555). Three insert sites
  hardcoded `model_used='agent-assisted'` in their SQL, so the first working headless run
  wrote six rows claiming a resident agent had judged them — the feature would have
  manufactured, for five rails at once, the exact blindness it exists to fix. The label is
  now a required keyword on the private inserters (a missed forward is a `TypeError`, not a
  silent lie) and defaults to the resident value on the public entry points, so the resident
  path is unchanged. Making it required immediately surfaced a third producer nobody had
  threaded: `federation_conflict_ingest` has six insert sites of its own.

- **`_make_generate_fn` accepts an optional per-call timeout**, unset for every existing
  caller. A call that never returns is not an exception, so a per-item `try/except` cannot
  catch it — an unattended runner would hang forever and never report. The defect holds at any
  latency; a CPU-bound dev box merely made it visible (a real conflict prompt had not returned
  after 240s there, against 11.6s for the same prompt on the inference host).


- **A declined re-ingest no longer overwrites the survey date** (#429, #711) —
  `surveyed_at` moved unconditionally while `verdict` sat behind a guard, so a re-ingest
  that *kept* the stored verdict still stamped the new date. The row then asserted that
  the old judgement was reached on the new date, with nothing on the outside able to
  detect it. The date and the verdict are one fact and now move together. **This fixes
  new writes and cannot repair old ones from the table alone** — though the snapshots and
  transcripts can, and did: 7 rows carry the signature, 0 are corrupt (#741).

- **A pattern is findable by its source URL** (#430) — `patterns_fts` indexed only
  `(name, body)`, so the dedup step that runs *before* spending a subagent could not
  structurally match a pattern by its source and returned a confident "no patterns
  matched". An already-surveyed repo got re-investigated and the caller was told the
  corpus had nothing. **Takes `architect.db` v5 → v6** — the second renumber this
  cycle, rebuilding the FTS table with `source_url` in it.

- **The reflect emit cursor was keyed on a hash that drifts** (#733) — keyed on
  `thread_id` now.

  **The effect was silence, not noise**, which is why it went unnoticed:
  `_marker_has_unemitted_threads` answered `len(emitted) < total`, so an over-full cursor
  read as *fully emitted* and every corrupted session dropped out of the SessionStart
  nudge. Measured: **126 owed queue rows were reported to the operator as 50.** If you
  have acted on those counts, they were understated.

  **Your cursor state is rebuilt on upgrade.** A pre-#733 marker is unrecoverable, so the
  delivered set is re-derived from the authoritative record — drained queue rows — rather
  than trusted or discarded.

- **The automatic reflect pass requires an explicit binding** (#731) — it inherited
  `config.model`, which is how the rail came to be bound to a credential plane the
  operator does not hold. An absent binding must read as absent, never as bound to
  something unreachable.

- **A test run behind a transparent prefix now contributes evidence** (#545) — a suite
  invoked through `nice`, `nohup`, `timeout` or `stdbuf` was invisible to the evidence
  gate, so `techne_check_evidence` could not confirm a test claim that had actually been
  run.

- **The capability report's enable snippet could be read as current state** (#638) —
  every capability was handed a bare `hooks:` block with the flag set true, including the
  ones that were OFF. That string is valid YAML describing a config, so a reader took it
  as status rather than as a change to make. Not hypothetical: an agent narrating the
  report quoted the snippet for an off flag and the operator concluded it was on. The
  frame now travels inside the payload, and a capability that is already on returns no
  snippet at all.


- **A cold model cache degrades to FTS instead of hanging the turn** (#708) — `search_vec`
  ran on **every prompt submission** and called the resolver with `allow_download=True`. On a
  cold HF cache that is a Hub fetch, and a blocking HTTP download is **signal-deaf**, so
  `bounded_hook`'s SIGALRM could not preempt it: the turn *hung* rather than degrading.
  `backend_available()` could not catch this — it is an import probe, so it passes with the
  package installed and the weights absent, which is exactly what `pip install techne[vectors]`
  leaves behind. `backfill_vectors` deliberately keeps the permissive default; guarding it too
  would leave nothing able to fetch the weights.

  **And a dead vector channel no longer records `outcome="ok"`.** Fixing the hang converted a
  reported failure into a clean marker, in the subsystem whose stated job is to make that
  impossible. That matters more than it sounds: `user_prompt_submit` is a `UserPromptSubmit`
  hook and exits 0, and Claude Code discards a zero-exit hook's stderr — so the marker is not
  the second half of the signal, it is the whole of it.

- **A rail switched off no longer reports LIVE forever** (#735) — `check_model_bound_rails`
  derived its verdict from a marker's outcome alone, and a marker records a run under a
  configuration that may no longer exist. Measured across four configurations, one `ok`
  marker: reflect **disabled** read LIVE, reflect **unbound** read LIVE, and **all five
  custodian rails** read LIVE — none of which had any retraction path at all.

  Now **no verdict is derived from a marker without first asking whether the rail is currently
  bound**. An unbound rail reads DORMANT and its payload is kept and shown. The direction is
  the point: unbound + stale `error` read BROKEN — noisy, wrong, self-announcing, so someone
  fixes it; disabled + stale `ok` read LIVE — silent, wrong, and comfortable.

### Docs

- **A guide for Reflect, and six agent triggers** (#791). `docs/reflect-guide.md` explains the
  verb at a learner's altitude — the two switches operators routinely confuse (`reflect.enabled`
  is the feature and the resident route; `reflect.model` binds only the unattended pass, and
  empty is a normal posture), where candidates land, how to drain a backlog, and a
  copy-pasteable block for an agent's own context.

  It also carries an `Agent: raise this unprompted` block, indexed in `AGENTS.md` and witnessed
  by tests: sweeping the reflection queue by hand, disabling the rail to quiet it, asking
  whether reflection is running, editing `config.yaml` under a live MCP server (which pins
  config at startup), draining a large session through the MCP tool rather than the CLI, and
  reporting a session drained when one `emit` call returned only its first slice.
  Reflect had the largest silent-failure surface of any subsystem, and its only guide-level documentation
  was its architecture card, which is written for someone changing the seam rather than
  learning it.

- **Sessions and threads are not the same thing, and now the docs say so** (#762). A session is
  a container the *client* hands techne — it starts when you open a client and ends at `/clear`
  or when the process dies. A thread is a unit of *work* techne derives afterwards. One session
  usually holds several threads; one thread usually spans several sessions. Measured on a real
  store: **534 sessions produced 2,209 threads, 1,242 of them chained to a parent in an earlier
  session.** `docs/threads.md` covers the scoring signals against the 0.7 threshold, the four
  merge rules, the content-addressed thread id and the FK hazard behind it (#92), and the
  `active → completed → stale` lifecycle.

- **Scoped the "nothing auto-runs `backfill_vectors`" claim to this repo** (#704) — it
  was true of the source and false of any deployment that schedules it, which is a claim
  about the world made from a grep of one tree.
- **Re-measured the disk cost against v0.15.0, one row only** (#691) — the fastembed row
  is a fresh measurement; the model2vec row is deliberately still the v0.14.0 figure,
  because a py3.13 venv is a *different construction*, and two numbers measured
  differently do not belong in one column.


- **A tutorial for the four functions** (#726) — [`docs/four-functions.md`](docs/four-functions.md).
  Ask each domain by name, read the `binding` it resolved to, and watch the **same call**
  answer from a different store. The escalation is *demonstrated* rather than asserted, and
  every transcript in it was captured by running the code. Carries an honest limitations
  section: the vocabulary is techne-only, cabal's Librarian currently means something
  narrower (#724), the codex is manually indexed **and resolves from your working directory**,
  and `techne_recall` is still the right default.
- **The README promotes the four names** from a storage-layout row to the mental model, and
  **`docs/welcome.md` gains the archivist**, which was previously absent from it entirely.
- **The release notes described half the release** (#726) — audited `[Unreleased]` against
  `git log v0.15.0..main` before the version bump: ten of twenty-one merged commits had no
  entry, including a schema renumber. `[Unreleased]` also carried two `### Added` and two
  `### Fixed` blocks, so content was present and invisible to anyone scanning; `[0.13.0]`
  repeats four headings inside a `<details>` archive — checked, and
  deliberate rather than a second instance, so it is exempted rather than merged. `tests/test_changelog.py` now pins the structural
  half.

## [0.15.0] — 2026-08-19

The release about what the store is willing to claim. 0.14.0 stopped the instruments
lying about whether rails *ran*; this one goes after what they assert when they do —
a search result that reported its source table as its kind, a memory we were on record
as not believing that got ranked rather than filtered, a call graph asserting 19,345
edges it had resolved none of, a version stamp certifying migrations that never ran,
and a health check that had been red since June with no way to clear it. Plus the half
that makes any of it reach you: techne can now introduce itself. **29 changes.**

### Added

- **`techne capabilities` and the `techne_capabilities` MCP tool** (#638) — what is
  switched on *on this install*, what each thing off would buy you, and the exact
  config line, grounded in counts from your own store. Measured on a fresh install:
  **8 of 8 capabilities report OFF**, because nearly everything is opt-in. It also
  reports bundled workflows absent from disk, which no config flag can see. A
  one-line invitation fires at session start **once per version change** and never
  again — deliberately, since #562 measured how fast a recurring banner becomes noise.
- **`self-care` and `techne-promote` ship with techne** (#641) — installed by
  `techne init`, versioned *with* the release because they drive its CLI and know its
  store layout. `techne skills install` / `status`; never overwrites a copy you edited.
- **First-prompt startup directive** (#643), gated on pending work and once per session.
- **Relevance floor for per-turn recall** (#414) — "inject nothing" is a legal outcome
  now. Ships the mechanism with the **value deliberately unset**: the threshold should
  come from the replay harness (#415), which is unbuilt, so behaviour is unchanged
  until someone sets it from evidence. The config comment carries the measured anchor.
- **`techne cards repair`** (#684) and a `remedy` field on fsck's results.
- **Rescue@10 eval gate** (#583) — does a supersede actually reach the ranker? Includes
  a test pinning the *known limit*: a rewrite that drops the original cue terms is not
  rescued.
- **`docs/mcp-tools.md`** — the complete agent-facing surface, pinned by a test so a
  tool cannot ship undocumented.

### Fixed

- **`search()` reported the source TABLE as `row['type']`** (#579, #566) — every
  consumer reading it was misled. The row now says which table *and* which kind.
- **`status='uncertain'` ranked instead of filtering** (#501) — we were injecting what
  we are on record as not believing. "Written down but not trusted" is now expressible.
- **The per-turn rail re-sent what the session already had** (#576) — **46.1%** of
  deliveries (3,227 of 7,004, re-measured 2026-08-18; #576 was filed at 45.4%), with
  compaction-aware suppression now in place.
- **SessionStart spent 74.5% of its budget on sessions that produce nothing** (#562 —
  re-measured 2026-08-18; the issue title's 76% is the original, smaller sample).
  No classifier can spot them in advance — project path, identity and lifetime were all
  measured and rejected — but 94% never submit a prompt, so the fix is **placement**:
  the model's copy waits for a first prompt that may never come. The operator banner is
  unaffected; it rides a separate carrier.
- **The code graph asserted calls it could not resolve** (#606). All 19,345 call edges
  have `to_symbol IS NULL`; 57% name-match some symbol and 99 ambiguous names cover
  4,177 edges. Rows now carry `resolution: exact | name-inferred`, `only_proven` returns
  what can be proved, and blast radius reports the split. The default stays permissive
  because flipping it would empty the call graph on every store today — the fix is
  honesty, not suppression. The indexer-side fix is #679.
- **A store ahead of its code migrated silently** (#676) — `current >= target` folded
  "already there" together with "ahead of the code". `architect.db` sat at v4 against a
  code constant of 3 for six days, **skipping every migration with no error, no warning,
  no log line**. Now loud on stderr, plus a `techne doctor` check per store.
- **fsck was red since June with no way to clear it** (#684) — #633 fixed the producer
  and left four rows behind. `maintenance.CheckResult` had no `remedy` field, so fsck
  could name a fault and offer nothing. A check that can never go green trains you to
  ignore it.
- **The applies graph could not see new patterns** (#674) — 141 of 505 patterns linked,
  a ceiling rather than a lag. Plus the `limenal`/`liminal` split, canonicalised at
  extraction and migrated.
- **461 silent codex index failures** (#610), a disk walk that swallowed errors (#599,
  #600), grep-assist answering a stale index with silence (#602).
- **The CLI reflect path left no record** (#642, #644, #645, #646) — ingest never
  drained the queue, emit never set the cursor.
- Also: #575, #590, #591, #593, #607, #608, #609, #620, #626, #629, #630, #631, #632,
  #635, #637.

### Docs

- **`docs/welcome.md`** gains an up-front "Don't read this — ask it" section pointing
  at `techne capabilities`, because a guide is a snapshot and your install is the
  truth. Adds the shipped workflows and this release's honesty features.
- **`docs/mcp-tools.md`** is new; the README's CLI reference gained `capabilities`,
  `audit`, `profile`, `prompts`, `deposit` and `cards repair`.
- **The counterfactual is #682** — four source sites claimed it was tracked, by two
  different numbers, neither of which was it. One was in `techne audit`'s
  operator-facing output.
- **Corrected the import-model claim from #606** (#679): from-imported *names* are
  recorded; what is missing is the source **module**, and therefore the binding.

### Upgrading

`pipx install --force "techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.15.0"`
— the `--force` is required; a plain `pipx install` over an existing techne refuses.

**Core schema is unchanged — `SCHEMA_VERSION` stays at 24, so from 0.14.0 there is no
techne.db migration.** Coming from 0.11/0.12 you still cross the 0.14.0 ladder; see that
release's notes, which remain accurate.

**`architect.db` migrates 3 → 4** — one step, collapsing the misspelled `limenal` target
into `liminal`. It is `INSERT OR IGNORE` + `DELETE`, not `UPDATE`, because the primary key
is `(pattern_id, target)` and a straight update collides on every pattern carrying both
spellings. `guarded_migrate` takes a backup first.

**If your `architect.db` is ALREADY stamped at 4, the step is skipped and the `limenal`
rows survive.** `guarded_migrate` returns at `current == target` before any step runs, so
a store that reached 4 by another route never sees this one — and because the code
constant has now caught up, `techne doctor` will report `schema:architect` **healthy**,
because the stamp finally matches. That is the honest state of the stamp, not of the data.
Check with `sqlite3 ~/.techne/architect.db "SELECT target, count(*) FROM pattern_applies
GROUP BY target"`; if `limenal` is present, reset the stamp to 3 and re-open techne to let
the step run. See #676.

**A merged fix is not a deployed fix.** If you run techne from an editable checkout
(`~/.techne/runtime`), `git pull` it too — otherwise the CLI keeps running the old
extractor and re-derives the rows this step just removed. That happened on the machine
that cut this release; see #690.

**If `techne doctor` reports `schema:<store>` BROKEN after upgrading, read it.** That
check is new in this release and it can only fire on a store *ahead* of its code —
meaning migrations are being skipped. Behind is normal and reports healthy.

**Run `techne skills install` after upgrading.** An upgrade does not re-run `techne init`,
so the bundled workflows are not refreshed automatically.


## [0.14.0] — 2026-08-17

The release where the instruments stopped lying. Nearly every fix below is the same shape:
a rail that was enabled, registered, and reporting healthy while doing nothing — a
grep-assist that never once fired, a code index that certified its own pollution, a
zero-result search that couldn't describe its own reliability, a CI guard that failed open
on a typo, a completeness check satisfied by a prefix, a glob that skipped files without
saying so. None of them looked broken. That is what made them worth a release. 24 changes.

**Upgrading:** `pipx install --force "techne[recommended] @ git+https://github.com/tenishai/techne.git@v0.14.0"`
— the `--force` is required; a plain `pipx install` over an existing techne refuses. What
you cross depends on where you start:

| from | schema | what runs |
|---|---|---|
| 0.13.0 | 24 → 24 | **Nothing.** Pure code upgrade, no migration. |
| 0.11.0 / 0.12.0 | 18 → 24 | Six steps, including one history rewrite — see below. |
| 0.9.0 / 0.10.0 | 17 → 24 | The above, plus step 18. |

From **0.11/0.12** the six steps are: **19** adds `knowledge.vec_stale` (additive, defaults
0); **20** builds a covering index on `outbox` — no data change, but its cost scales with
accumulated synced rows, so the oldest store pays the most; **21** namespaces existing coord
read receipts under `uuid:` and switches new ones to `name:<display-name>`, so a relaunched
**named** session stops re-reading the backlog — *after draining it one final time*, because
old UUID-keyed receipts deliberately do not transfer (the UUID→name map lived in per-session
sidecars that are gone, and inventing one would fabricate the very "who read what" claim a
receipt exists to record); **22** bumps the version so the additive `reflection_queue` table
actually reaches existing databases — the bump *is* the load-bearing part, since an additive
schema change at an unchanged version reaches new databases only; **23 rewrites
`observations.timestamp` and `.event_at` in place across your entire history**, normalising
three concurrently-written formats to one — the only step that touches existing data at
scale; **24** backfills historical `sessions.ended_at` NULLs from last known activity.

Coming from **0.9/0.10** you also cross **18**, which redeploys the `observations_*` FTS
triggers in their content+tags+kind form and **rebuilds `observations_fts`** over your whole
history — on those databases `tags`/`kind` were silently never indexed.

**That one-time backlog drain in step 21 is expected and is indistinguishable from a fault
unless you know.** If a named session re-reads the coord backlog once after upgrading, that
is the migration working as designed, not a failure.

**IF YOU RUN A LOCKED OPERATOR LAYER AND SET `sources` IN `config.yaml`, MOVE IT INTO THE
OPERATOR LAYER BEFORE UPGRADING.** As of #585 `sources` is a governed destination, so a
locked layer now *discards* agent-supplied source paths instead of honouring them. **The
drop is silent** — nothing logs it — so the symptom is "techne stopped ingesting my
transcripts" with nothing pointing back at the upgrade. Installs with no operator layer
(the default) are unaffected.

The migrator takes an automatic backup before migrating (`guarded_migrate(backup=True)`),
and since #574 that backup is reused within a transition and pruned afterward rather than
accumulating. Taking your own copy of `~/.techne/` first is belt-and-braces, not required.

### Added

- **Citation-attribution for the knowledge rails** (#329, #560) — closes the last open
  finding of the utilization work: whether an injected memory was actually *used*, not merely
  retrieved. Without it, every change to what gets retrieved is unfalsifiable — there is no
  way to tell an improvement from a regression. Behavioral proxy first, deliberately: free,
  deterministic, no model key, CI-runnable.

- **Executable predicate for the evidence length test** (#541, #567) — the gate asserted a
  property it described in prose. It now runs the probe it claims to run.

- **Archived `.jsonl.gz` transcripts are readable** (#621) — gzipping old transcripts is a
  4.5x saving (14.0 MB → 3.1 MB measured on a real one, byte-identical round trip) against
  366 MB of transcripts older than a fortnight. But a `.gz` was **invisible** to both
  transcript globs — no error, just skipped — and the reader died on one with a
  `UnicodeDecodeError`. So the corpus could shrink silently: archive to save space, and the
  archived sessions quietly stop existing as far as import and search are concerned. The
  reader now opens `.gz` transparently and both globs match it, so archiving is no longer a
  form of deletion.

- **Crash-recovery fault-injection gate** (#388) — techne is a stateful daemon over SQLite,
  and the interesting bugs live in *interrupted transitions*, which unit tests never reach
  because a unit test always gets to finish. A new `crash` suite SIGKILLs a child mid-write
  and then proves the store: an uncommitted batch leaves no trace, a committed one survives,
  a hot WAL replays, an interrupted migration does not stamp its version *and converges on
  re-run*, and an interrupted flush never marks an undelivered row synced. **The child kills
  itself at a chosen line** rather than racing a timer from the parent — a crash test that
  fails randomly teaches people to re-run CI until it is green, which is how a real
  regression gets waved through. What is tested is *our* usage, not SQLite's atomicity. Run
  by a dedicated `crash-recovery` job, because a suite excluded from the default run and
  executed by nothing decays quietly.

- **A go-public runbook** (#556) — `docs/go-public-runbook.md`. Not yet executed; the flip is
  deliberately paused. Its load-bearing finding: **stopping the runner does not shorten the
  dangerous window between "public" and "approval policy applied", it closes it** — a job
  whose labels match no *online* runner queues rather than executes.

### Fixed

- **grep-assist was unreachable in practice, and granted permission when it did fire**
  (#596, #601) — the rail was enabled, registered, and fired *zero times* across a full
  session of searching this codebase, because its matcher was `Grep` while every real search
  was `grep` inside a `Bash` call. The guard's precondition was "the agent chose the Grep
  tool", and falsifying it was free, accidental and invisible. Widening the matcher to
  `Grep|Bash` is one line; the rest is parsing a shell command safely enough to deserve it —
  per-head option tables measured against the actual binaries, unlisted options declining
  rather than guessing. **Also removed an unconditional `permissionDecision: "allow"`**: dormant
  and near-harmless while the matcher was `Grep` alone, a live hole the moment it matched
  Bash, where a grant covers the entire command line while the parser only has to like one
  segment. A test had pinned that grant as *intended*.

- **The code index certified its own pollution** (#604, #605, #613, #615) — virtualenv
  contents were indexed, so 96% of this repo's graph was vendored code: 114,412 foreign
  symbols against 5,137 real ones, with `codex_blast_radius` returning `aiohttp/web.py` as a
  dependent of a techne hook. Indexing is incremental and its only DELETE is scoped to the
  file being re-indexed, so the polluted rows were never revisited — and `index_freshness`
  reported `coverage: complete` over them, because those files really were present and
  unchanged on disk. They were not stale; they should not have been there. Skip-list
  enforcement now runs **on every index** rather than once in a migration, so a skip-list
  change heals itself instead of needing a new migration each time.

- **A zero-result code search described itself as authoritative** (#595, #598) — a found
  result carried a staleness flag and a refresh notice; a zero result carried nothing. "I
  cannot see it" rendered as "it is not there".

- **grep-assist option tables** (#611, #616) — `--group-separator` now declines rather than
  asserting an arity that differs between ugrep and GNU grep, where it would consume the
  pattern. Provenance recorded for the table whose missing provenance caused the defect.

- **Per-turn recall showed the source table as the row's kind** (#603), and the per-turn PUSH
  rail injected pull-only rows (#561, #565).

- **Health reported a guess about the install kind** instead of the revision (#568, #569).

- **Migration backups grew without bound** (#574, #578) — now reused within a transition and
  pruned after.

- **MCP accepted a stale env-derived session identity** (#573, #581), and the reflect-ingest
  surface pre-judged an empty response (#485, #563).

- **Architect pattern search never got the OR fallback** (#70, #557, #558).

- **A capture-rail test was measuring the CI runner** rather than the rail (#543, #564).

### Security

- **`sources` is now a governed destination — THE ONE ENTRY HERE THAT ASKS SOMETHING OF YOU**
  (#585). `sources.claude_code_transcripts.paths` names arbitrary filesystem paths whose
  *contents* are ingested into the observation ledger, and the rationale that already
  governed `connections` applies to it word for word. **This changes enforcement, silently:**
  under a locked operator layer, governed keys in `config.yaml` are *dropped*, so a locked
  install now discards agent-supplied source paths where it previously honoured them.
  Nothing logs the drop. **Action: if you run a locked operator layer with `sources` in
  `config.yaml`, move it into the operator layer before upgrading** — see "Upgrading" above.
  Default installs (no operator layer) are unaffected.

- **Unlisted sync adapters are refused by default** (#389) — `load_adapter` would import any
  class registered under the `techne.sync_adapters` entry-point group and hand it every
  outbox entry, i.e. full session and memory content. Loading arbitrary registered code that
  receives memory is an egress decision, not a plugin lookup. Now gated behind
  `sync.allow_unlisted_adapter`, and the refusal *precedes* the import, because import-time
  side effects are code execution. Within one OS user this is not a wall — it makes an
  unlisted adapter impossible to load *silently*. Filed here rather than under Added: the
  audit it came from found techne has **zero network-capable imports**, so there was no POST
  to gate and this is hardening, not a feature.

- **Fork PRs could reach the self-hosted runner** (#556, #570) — `pull_request` plus a
  self-hosted pool is remote code execution on the runner host. The guard now fails *closed*;
  the previous one failed open on a typo'd property, and for `pull_request` GitHub evaluates
  the workflow from the PR's own ref, so an in-workflow guard is attacker-editable. Forking
  is disabled at both repo and org level as the load-bearing control.

- **Every Config field now forces a destination judgment** (#509, #582) — egress keys were
  opt-out by omission, so a new field defaulted to leaving the machine.

- **A dotted-key prefix was accepted as that judgment** (#588) — `reflect.model` in
  `EGRESS_KEYS` marked the *entire* `reflect` block classified, so every other field inside
  it forced no judgment at all. Measured: adding a nested LLM endpoint override left the
  suite byte-identical. Classification is now exact and recursive.

- **The completeness guard checked a surface the gate does not use** (#586) — it resolved
  entries against the *dataclass* surface while the gate acts on the mapping the loader
  walks; the two agree only by convention. It is now behavioural: load a config with each
  key and without it, and require the result to differ. Found on the way and filed
  separately (#620): `Config.load` reads a `conflict` block that `save` never writes, so a
  hand-written one is dropped by any load→save cycle.

- **A recycled pid read as a present session** (#587) — presence was TTL-free and pid-only,
  the exact combination the neighbouring code warns against. Demotion now requires *positive*
  evidence of a mismatch, because the obvious implementation would manufacture false absents,
  and a false absent refuses a session that is genuinely working. The `ps` snapshot is cached
  (measured: 31.9 ms → 0.041 ms warm), since cost was why this was deferred rather than an
  oversight.

### Docs

- Architecture cards for the **utilization seam** (#597) and the **codex** store, the latter
  now anchored to both files it makes claims about (#617). The card gate catches drift only
  where an anchor exists, so an unanchored claim is an unguarded one. The cost is stated at
  the anchor site: `source.py` churns for reasons unrelated to the card, so the gate will
  fire on edits that do not concern it — **bump the sha, do not delete the anchor to make it
  quiet.**

- The PreToolUse protocol shape is **observed rather than inferred** (#612, #614) — settled by
  dogfood, because telemetry structurally cannot see it: `record_hook_run` fires on printing,
  not delivery, so a rail emitting into the void still reads healthy.

## [0.13.0] — 2026-08-11

The milestone where the memory rails got their integrity checked, the evidence gate learned
to read the commands operators actually type, and CI started telling the truth about what it
ran. 39 issues closed. Three of the fixes below were only reachable *because* another fix in
this same release landed first — those interactions are called out where they matter.

**Upgrading:** schema goes 21 → 24 in three steps, applied automatically on first `connect()`.
Step 22 creates `reflection_queue`, 23 normalises every on-disk timestamp to one shape, 24
derives `ended_at` for sessions that died unclean. The steps are ordered and gated; a store
that has run any of them will not re-run it.

### Added

- **Durable reflection debt** (#514) — `session_end` counted the debt, logged "403 obs, 19
  threads", and enqueued nothing, so reflection survived exactly one exit path: the polite
  one. Every crash, kill or Remote Control timeout dropped it silently, and the
  instrumentation made the rail read as healthy while the work evaporated. One
  `reflection_queue` row per *emittable* thread now records what is owed, and the
  SessionStart banner reads that ledger instead of re-deriving from recency — so debt that
  ages past the recency window is no longer permanently invisible. Idempotent by schema
  (`UNIQUE(thread_id)`), so two sessions racing cannot double-queue.

- **Subsystem liveness — `techne doctor`** (#468 steps 1–2, #469, #487, #491) — "is techne
  installed" and "is techne WORKING" are different questions and only the first had an
  answer. Each check reports LIVE / DORMANT / STALE / UNOBSERVABLE **with a remedy the
  operator can act on**; a verdict nobody can clear teaches people to ignore the check, so
  UNOBSERVABLE is reserved for a rail that *should* be emitting and isn't, never one
  correctly idle. Includes `runtime:loaded`, which compares a long-lived process's start time
  against the deployed HEAD — a conservative staleness warning, not a tear detector, and it
  says so.

- **Operator-owned config layer for egress-selecting keys** (#393) — see README
  §"Operator-owned config". Honoured only while provably not writable by the process reading
  it; a writable layer **fails closed** to built-in defaults rather than deferring to
  `config.yaml`. Installs that deploy no layer are unaffected.

- **`techne architect ingest --verdict`, `reverdict`, `sweep-legacy-verdicts`** (#428) —
  record the operator's actual judgment (`skip` / `skip-with-flag` / `patterns-only` /
  `full-eval`) instead of a hardcoded constant. `reverdict` leaves `surveyed_at` alone:
  correcting a judgment is not a re-survey.

- **Retrieval-correctness gates** (#385, #399, #398/#403) — a regression gate for retrieval,
  a hybrid/vector gate, and deterministic eval legs per PR plus a py3.14 `eval-vec` job.

- **Per-hit retrieval provenance** (#413) and **capture / per-turn-recall rail
  instrumentation** (#479) — recorded in `context_injections`, so "is what we inject
  worthwhile?" becomes answerable from data rather than argued.

- **Banner honesty** (#488, #452/#457) — deferred (expensive) checks are named rather than
  silently omitted, and hook-registration drift is surfaced at session start.

### Fixed

- **Outbox retention that preserves the dedup ledger** (#518, #484) — 130,266 synced rows
  were never pruned. The obvious fix is the dangerous one: synced rows on the
  `archivist_knowledge` lane **are** the de-facto export ledger (the content-hash pre-filter
  reads them), so deleting them re-enqueues the entire knowledge base on the next sweep. That
  lane is now *collapsed* to one row per hash; the plain `archivist` lane ages out. Two
  tombstones keep one row per target so `COUNT`/`MAX(synced_at)` stay honest and a healthy
  lane cannot report "never synced".

- **One on-disk timestamp shape** (#515) — `observations.timestamp` held three formats
  written concurrently across the whole history. TEXT columns compare lexically, so `T`
  (0x54) sorted after `' '` (0x20) and every ISO row read as later than every space-separated
  row on the same date. Measured on the live store: a range query returned 782 rows where
  770 were correct — 12 phantom rows, ~1.5%, silently. Backfilled, and every
  writer normalises at the boundary — including the public `event_at` parameter, which the
  MCP tool schema and `--event-at` flag both document as ISO-8601, i.e. the contract *asked*
  for the shape being eliminated.

- **`sessions.ended_at` for sessions that died unclean** (#516) — 79% NULL and degrading
  (52% of June, 19% of July, 7% of August). With `ended_at` NULL the interval is open-ended,
  so "which session was live at T" matched every session ever started before T. Derived from
  last activity, with `ended_at_source` ('clean' | 'derived') as the discriminator — because
  after this change "has an ended_at" no longer means "ended cleanly", and counting derived
  ends would report a healthy store as STALE.

- **`env` is a transparent prefix, not a read-only utility** (#524) — `env` headed the
  read-only set, so `env … git commit` was dropped as navigation noise and, worse, an
  unrecognised command discarded the whole ledger row: a real 2,620-test run with one failure
  was thrown away while a 5-test run certified the suite green and *backed* a `tests_green`
  claim.

- **The evidence gate reads what actually ran** (#437, #439, #444, #445, #441) — keeps the
  tail of tool output so verdicts survive capture; stops reading a missing failure count as
  zero failures; reads totals only from commands that really run tests; sees through
  `bash -c` without changing what is captured; and treats a redacted legacy row as bounded,
  since `<redacted:…>` can shrink a row below the length threshold and an unflagged truncated
  row can otherwise contribute to a green verdict.

- **Vector hygiene** (#408, #382) — `threads_vec` accumulated one orphaned embedding per
  summary update, unbounded, because threads rotate rowid on `INSERT OR REPLACE`; and
  content-edited rows are re-embedded via a `vec_stale` flag instead of silently keeping a
  stale vector. `vectors:coverage` also stopped counting a population its remedy could not
  act on (#496).

- **CI ran fewer tests than it appeared to** (#530, #535) — a stacked PR got **no CI at
  all**, and six vector-dependent files ran in **neither** leg: they skipped in the job that
  would select them and were deselected in the job that could run them. Both closed, with
  static guards against the workflow edits that would silently reintroduce either.

- **`techne watch` no longer crash-loops forever on a missing `[watch]` extra** (#498) — a
  configuration error was being retried as though transient, every 5 seconds, forever. Exits
  78 (`EX_CONFIG`); the shipped unit sets `RestartPreventExitStatus=78`. **Both halves are
  required** — either alone still loops.

- **Coordination** (#492, #494, #446, #380) — a DORMANT twin is now a rival for the name;
  `send(to=<name>)` reads the sidecars once; read receipts key on the durable identity rather
  than the per-launch UUID; dead PIDs are reaped from `live_sessions`.

- **`fastconfig` import guards are a cost allowlist, not a techne blocklist** (#489) — the
  guard enumerated modules that must not be imported, which cannot catch a new expensive one.

- Also: atomic liveness guard on knowledge supersede/retire (#390); codex OR-fallback for
  multi-term symbol search (#392); vec-first union replacing RRF fusion (#401); archivist no
  longer enqueues redirected types into a lane that rejects them (#450); real git-worktree
  detection instead of a path convention (#434); interpreter validated before being baked
  into `~/.claude` (#458); MCP server registered through the self-healing launcher (#370); a
  discarded reflection no longer reports as a successful empty one (#465); `techne doctor`
  asks for `--posture auto` on a machine that was never initialised (#480).

### Performance

- **Hook fast path — 100ms → 50ms per hook** (#470) — stdlib-only config read.
- **Archivist stops re-enqueuing the whole knowledge base every teardown** (#475).
- **Suite runs under `pytest-xdist` — 654s → 317s of runner time** (#463), with isolation
  dirs carved out of `tmp_path` rather than `mktemp` (#466), and the suite no longer writes
  into the operator's live store (#477).
- **CI moved onto the self-hosted pool** (#424, #461).

### Documentation

- **Three suites are documented as deliberately local-only, and the claim is testable**
  (#453, #456, #546; part of #537, which stays OPEN) — `probe`, `eval_llm` and the two
  `RUN_LSP_INTEGRATION`-gated LSP
  tests each need a toolchain CI does not carry, and each would SKIP rather than fail if
  wired up naively, buying a green result that means less than it looks.
  `tests/test_local_only_suites.py` fails if repo and policy drift apart in either direction.
  Also records why `hooks.capture_read` is off: volume, not maturity. **#537 is only half
  closed and deliberately stays open**: its LSP-documentation half is addressed here, but
  its other half — the `model2vec` backend branch being exercised by no CI leg — is
  untouched by this release.
- **Cold-storage tier proposal** (#405) — graduate aged knowledge to Librarian documents.
- The arch-card freshness gate is wired into CI (#383).

### Removed

- **`search.mmr_lambda` config knob** (#420) — it defaulted to 0.3, round-tripped through
  `config.yaml`, and was never passed to any search function, so every live path ran at 0.0
  (MMR off) and setting it was a silent no-op. Removed rather than plumbed: turning it on
  would have been an unmeasured retrieval change on installs whose 0.3 was a serialized
  default nobody chose. Existing configs carrying the key still load; it is ignored, as it
  was in effect before.

<details>
<summary>Earlier detail from this cycle, kept verbatim</summary>

### Added

- **Operator-owned config layer for egress-selecting keys** (#393) — the keys that choose where memory can GO (`sync`, `connections`, `llm_api_base`, `llm_api_key`, `model`, `reflect.model`, `conflict.model`, `archivist_db_path`, `standalone`, `stores`) can now be pinned in a `config.operator.yaml` that techne honors only while the file is provably not writable by the process reading it. Resolved from `/etc/techne/config.operator.yaml` → `$TECHNE_OPERATOR_CONFIG` → `<techne_home>/config.operator.yaml`, gated once in `Config.load()`. A layer that exists but is writable by us FAILS CLOSED to the built-in default destinations rather than deferring to `config.yaml` — and `config.yaml` keeps its own values on disk either way, so an enforcing layer never destroys the file it overrules. Only `/etc/techne/config.operator.yaml` is a real lock; the other two tiers are conveniences an agent can bypass from its own environment — `$TECHNE_OPERATOR_CONFIG` with `env -u`, and the `<techne_home>` tier via `TECHNE_HOME` — and the check names the tier in force together with the lever that actually applies to it. **Operators pinning a layer must declare the WHOLE set** — an omitted key reverts to its built-in value rather than falling back to `config.yaml`. Watch `stores` in particular: an omitted store DSN does not error, it opens a new empty database at `<data_dir>/<domain>.db`, so domain memory looks intact and is not. **Installs with no operator layer are unaffected** — `config.yaml` keeps full authority, and the new `config:trust` check reports that state rather than implying a boundary nobody deployed.

- **`techne architect ingest --verdict`** (#428) — record the operator's actual judgment (`skip` / `skip-with-flag` / `patterns-only` / `full-eval`) for the URLs an ingest covers.
- **`techne architect reverdict URL --verdict X`** (#428) — correct one investigation's verdict without re-ingesting its pattern block. Leaves `surveyed_at` alone: correcting a judgment is not a re-survey.
- **`techne architect sweep-legacy-verdicts --before DATE [--apply]`** (#428) — retire pre-#428 auto-assigned verdicts to `unknown`. Dry-run by default and date-scoped, so a genuine post-fix verdict is never clobbered.

### Removed

- **`search.mmr_lambda` config knob** (#420) — it defaulted to 0.3, round-tripped through `config.yaml`, and was never passed to any search function, so every live path ran at `search_fts`'s own 0.0 (MMR off). Setting it was a silent no-op. Removed rather than plumbed because turning it on would have been an unmeasured retrieval change: every live caller passes λ=0, so `_mmr_reorder` returns at its λ=0 guard and never reaches the scoring loop. The deterministic `eval`/`eval_gate` legs install `.[dev,codex]` and lack numpy, so they cannot observe MMR at any λ — but the `eval-vec` leg CAN (it installs `[vectors]` on 3.14 with `potion-base-8M` pre-cached, and its `search(use_vectors=True)` path runs through `_mmr_reorder`), which is where a re-plumbing measurement belongs. Plumbing it would therefore have been an unmeasured retrieval change, on installs whose 0.3 was a serialized default nobody chose. Existing configs carrying the key still load; it is ignored, as it was in effect before. The `mmr_lambda=` parameter on `search_fts`/`_mmr_reorder` is unchanged and still tested.

### Documentation

- **Three suites are now documented as deliberately local-only, and the claim is testable** (#453, #456, #537) — `probe` (3 tests, #94 memory-attribution), `eval_llm` (1 test, #118) and the two `RUN_LSP_INTEGRATION`-gated LSP tests each need a toolchain CI does not carry (the `claude`/`opencode` CLI, a reachable model, `pyright-langserver` i.e. Node for two tests), and each would SKIP rather than fail if wired up naively — so covering them buys a green result that means less than it looks. ARCHITECTURE.md now states this as a decision rather than a "gotcha", and `tests/test_local_only_suites.py` fails if the repo and the policy drift apart in either direction. Also records WHY `hooks.capture_read` is off (`config.py`): volume, not maturity — `Read` is far more frequent than `Bash`, so capturing it inflates the ledger without a matching gain. It is shipped and symmetric with `capture_bash`; the only defect was that the reason lived in someone's head.

### Fixed

- **`techne watch` no longer crash-loops forever on a missing `[watch]` extra** (#498) — `deploy/techne-watch.service` shipped `Restart=on-failure` + `RestartSec=5s` with no `RestartPreventExitStatus`, while `watch()` raised a plain `RuntimeError` for the missing `watchdog` import. An operator who installed techne without the extra got a respawn every 5 seconds forever, and a journal full of tracebacks that read like a runtime fault when the fix was one `pip install`. A configuration error was being retried as though it were transient, and it never becomes non-transient. `techne watch` now exits `78` (`sysexits.h` `EX_CONFIG`) on a startup precondition that restarting cannot satisfy, and the shipped unit sets `RestartPreventExitStatus=78`, so systemd stops on the FIRST failure — no retry counter, no backoff ladder, no coordination between app and supervisor beyond one agreed constant. **Both halves are required**: either line alone still crash-loops, which is why the test derives the unit's value from `techne/exit_codes.py` rather than retyping `78`. The new `StartupConfigError` subclasses `RuntimeError` deliberately, so existing `except RuntimeError` callers are unaffected and the exit contract is opt-in at the seam that can act on it.

- **`runtime:loaded` — a long-running process holding pre-deploy modules is no longer reported as healthy** (#491) — `runtime:revision` reads git HEAD from disk, so it rendered a torn MCP server as LIVE while `techne_reflect_emit` died on an `ImportError` for a symbol that was present in both the repo and the runtime. The new check compares each long-lived techne process's start time against the deployed HEAD's committer time and reports STALE with a restart remedy. Deliberately a **conservative staleness warning, not a tear detector**: a runtime pull under a live server was measured NOT tearing it, so the check reports the ordering it observed and explicitly disclaims the inference.

- **The investigation verdict is no longer hardcoded** (#428) — `ingest_patterns_md` wrote `full-eval` for any URL group carrying patterns and `skip-with-flag` for a no-lift bullet, and the CLI could not set one, so the column recorded *whether the markdown had patterns* rather than a judgment and `patterns-only` was unreachable by any code path. The live corpus had 202 `full-eval` / 70 `skip-with-flag` / 0 `patterns-only` against 19 eval docs on disk. Verdicts are now validated against a vocabulary, and an ingest that does not state one records `patterns-only` (the documented modal outcome) instead of `full-eval`.

</details>

## [0.12.0] — 2026-07-24

### Added

- **Inter-session coordination mailbox** (#376, #378, #379) — live Claude sessions on one host can message each other over a `coord` bus (`techne_send` / `techne_inbox` / `techne_threads`): broadcast + direct addressing, session aliases, threaded replies, and a collision warning when two sessions claim the same branch or worktree. Phase 1 (core mailbox) + Phase 2 (aliases, threads, collision warning).
- **Architecture cards** (#383, #384) — an in-repo repo card (`ARCHITECTURE.md`) plus per-seam cards (`docs/arch/*.md`) with a git-anchored freshness gate and `AGENTS.md` wiring, so an agent grasps a subsystem from its card before reading source.
- **Session-end Bash backfill** (#371, #375) — capture can backfill Bash activity at session end behind a shared read-only denylist, restoring partial visibility for otherwise-uncaptured Bash-only sessions.

### Changed

- **Honest edit-rail attribution** (#377) — utilization attributes edits at symbol granularity, turn-windowed, for truthful read-vs-write accounting.

## [0.11.0] — 2026-07-16

### Added

- **Base Archivist — closed team-memory loop** (#346 PR 2) — a non-standalone
  instance now egresses live durable knowledge to the Archivist (`sink:archivist_knowledge`)
  at session end, and recall automatically federates over its `archivist.db` as a
  `team` connection. So a fact stored by one agent is read back as team memory by
  another sharing the Archivist — locally, no cabal, no git. Re-pushing the same
  fact doesn't duplicate it (the handler skips one already live in the Archivist).
  Known v1 gaps, both closed by the #223 export ledger: superseded facts leave a
  stale revision live in the Archivist (append-only, no supersession propagation),
  and every session re-enqueues all live knowledge (the outbox's partial unique
  index only collapses still-pending duplicates).

- **Base Archivist — write target + topology lane selector** (#346 PR 1, #224) — a
  `standalone` config flag selects the outbox lane for thread summaries: `true`
  (default, behaviour-preserving) keeps the `sync:` adapter lane; `false` routes to
  `sink:archivist`, a techne-native Archivist that writes pushed team memory into a
  techne-schema `archivist.db` (`archivist_db_path`, default `<data_dir>/archivist.db`).
  `_has_live_consumer` is now lane-aware (sync adapter vs. registered sink). techne
  never touches git in either lane. The federation read-back loop is PR 2.

- **Operator-editable maintenance prompts** (#219, completes the Coherence track of
  #212) — the reflect / consolidate / dream prompts that drive knowledge maintenance
  are now overridable per-install: drop a file at `<data_dir>/prompts/<name>.md` and
  the change is picked up on the next run — no code edit, no restart. A broken
  override (bad `{placeholder}`) falls back to the built-in default with a warning,
  so it can never break the maintenance loop. `techne prompts export` scaffolds the
  defaults to edit from; `techne prompts path` prints the directory. Contract in
  `docs/prompts.md`.

- **Codex edit-rail telemetry hygiene** (#342, #343) — two tuning fixes so the
  edit-rail data is trustworthy for the fix-or-cut read (#338). (1) Per-symbol
  -per-session throttle: a symbol's blast radius is injected at most once per
  session, so re-edits don't spam context or inflate utilization counts. (2) The
  injection log now records `total_hits` — the true pre-cap blast-radius size —
  alongside `hit_count` (the shown/cost count), so `techne audit` shows a
  `radius` column distinguishing a 252-dependent hub from a 10-dependent leaf.
  `context_injections` self-migrates the new nullable column.

- **Recall returns memory + librarian documents** (#340) — `techne_recall` now
  folds the top library documents (from `librarian.db`, resolved locally) into
  its result set alongside memory. Every row is labelled `origin` (`memory` vs
  `library`) so results stay clearly sourced; documents come as capped snippets
  with `source_url` for full retrieval. Capped by `search.recall_documents_limit`
  (default 3; 0 disables). Best-effort — a missing/broken `librarian.db` degrades
  to memory-only. New `librarian.recall_documents()`. Previously the 62-document
  store was reachable only via the `techne library` CLI, invisible to recall.

- **`techne audit`** (#329) — a utilization audit with two descriptive panels:
  the injection read-path (per-source events / hits / tokens / sessions /
  empty-fires over `context_injections`) and the ledger read/write ratios per
  memory domain (store-vs-recall, codex-reads-vs-edits, architect + librarian).
  `--since N` windows to the last N days; `--json` for machine output.
  Attribution ("was the injected item used") is out of scope — see #105.

- **Codex edit rail** (#335) — an opt-in PostToolUse hook (`hooks.codex_edit_rail`,
  default off) that, after an Edit/MultiEdit, maps the edit to its enclosing
  symbol and injects that symbol's blast-radius (transitive dependents — "what
  breaks if I change this") into context. Threshold-gated
  (`codex_edit_rail_min_deps`, default 3) so low-risk leaf edits stay silent, and
  capped (`codex_edit_rail_limit`, default 10, closest-first by depth). Logs every
  injection to `context_injections` (`source=codex_edit_rail`) so its utilization
  is measurable against the other read-paths (#329). New `codegraph.symbol_at()`
  maps a file+line to the innermost enclosing symbol via its span.

- **Config-driven tag inference threshold — tag revival** (#352) — `tag_threshold`
  is now a `Config` field (default `0.42`). The old hardcoded `0.6` sat above the
  model2vec (potion-base-8M) cosine ceiling (~0.59), so `infer()` tagged 0% of
  rows — the embedding stack was revived (#356) but tags stayed empty. `infer()`
  resolves the threshold from config when none is passed explicitly; the ceiling is
  backend-dependent, so it's a dial. Re-running `vectors backfill --tags` took the
  live store from 147 to 8.4k / 11.4k tagged. (Prototype over-fire on `todo` /
  `context` is tracked in #368.)

- **Per-category memory fill-state in the banner** (#334) — the session-start banner
  breaks its opaque `knowledge` total into per-type counts (+ identity) on a
  `memory:` line, so the agent can see WHERE memory accumulates and self-regulate
  what it writes. `db_stats` gains `knowledge_by_type`; non-canonical types are
  appended after the canonical ones so the line always accounts for the full total.

- **`techne profile`** (#359) — a repeatable hot-path profiling command (read-only,
  never mutates). `profile_import_cost()` measures the cold `import techne.config`
  price every hook pays before any work (#351); `profile_thread_detection()` times
  heuristic detection over the largest session and surfaces the top functions by
  cumulative time (#350). Latency-at-scale had no harness; now it does.

- **Hook budget-burn is observable** (#362) — `bounded_hook` hard-killed on timeout
  and otherwise exited 0 with no signal, so a hook creeping toward (or blowing) its
  budget was invisible — the #350 pathology where thread detection sat at 4.45s
  under a 5s budget and silently dropped detection every turn. It now warns on
  stderr at `warn_ratio` (default 0.7) of budget, and more loudly on an actual
  timeout, naming the hook + elapsed/budget.

- **Vector-channel degradation in the banner** (#358) — a missing embedding backend
  silently downgrades recall to FTS-only and a backend/model swap leaves the vec
  index stale, both with zero signal (this is how the embedding stack sat dead in
  prod, #352). `collect_health` folds `vector_channel_status` into a `vectors`
  summary and the banner alarms (⚠️) only when recall has actually degraded —
  stored vectors but no usable backend, or a stale index after a swap.

- **Schema-drift detector + v18 migration heal** (#364) — reconcile is deliberately
  additive (`CREATE IF NOT EXISTS`), so a redefinition under an existing name
  applies silently never and drift goes unnoticed. A detector now compares live
  trigger/index definitions (from `sqlite_master`) against canonical via DDL
  normalization and reports mismatches, and a v18 migration heals the two drifts
  surfaced in the perf review — without overturning the additive convention.

### Fixed

- **Codex blast-radius direction + fixture noise** (#325, PR #336) — `blast-radius`
  now defaults to dependents (`direction=in`, "what breaks if I change this")
  instead of dependencies, and `tests/fixtures/**` name-collision phantom nodes are
  excluded by default (`--include-fixtures` / `include_fixtures` to opt back in).

- **Evidence gate: passive inspection can't back a completion claim** (#105, PR #354)
  — the completion check backed on `any_tool_call`, set by ANY captured tool
  including read-only inspection (Read/Grep/Glob/WebFetch), so an agent that only
  *looked* at a file and claimed "done" got a green "backed" — the gate affirming
  work that never happened, the worst failure direction for a "no unbacked word"
  check. Split out `active_tool_call` (a deed — anything outside a new
  `PASSIVE_TOOLS` set) and back completion on `active_tool_call or artifacts`.

- **Federation: relative connection db_path resolves against TECHNE_HOME** (#332) —
  a federation connection's `db_path` had to be absolute, making a committed
  `config.yaml` machine-specific. A relative path now resolves against
  `techne_home()` at open time (absolute paths and `~` unchanged), so a shared
  config can carry `db_path: team-mirror.db` and each machine resolves it locally.
  Resolved at the open site, not config load, so the portable string survives
  save round-trips.

### Performance

- **Hook hot-path fixes + embedding/tag revival** (#356, staff review) — steady-state
  hook cost: `find_repo()` gained a per-pass path cache (thread detection was making
  ~126k `stat()` calls on a 1695-obs session by re-walking the same recurring
  paths), and feature extraction now runs once per pass. Also restored the embedding
  stack — the pipx runtime was missing `model2vec` / `sqlite-vec`, so recall had
  silently degraded to FTS-only and observations stored `tags=NULL`; vectors are
  backfilled here (the tag half lands via #352).

## [0.10.0] — 2026-07-13

### Added

- **Codex LSP-grounded call graph** — a reusable LSP client foundation (#320, PR
  #326) plus pyright-resolved call edges so callers are ground-truth rather than
  heuristic (#321, PR #327). Symbol resolution now accepts bare names and errors
  hard on an unresolvable symbol id (#300, PR #302), applied to the MCP
  blast-radius tool as well (#303, PR #304). The codex index reindexes the edited
  file on Write/Edit for mid-session freshness (#301, PR #314).

- **Architect curation** — topic-index + supersession curation (#308, PR #313),
  validity-windowed supersession (#306, PR #317), and the relation graph rendered
  as Mermaid/DOT (#305, PR #316).

- **Per-turn memory-seed nudge (#277, PR #309)** — closes the write half of the
  recall loop begun in 0.9.0. SessionStart injection is instrumented and unified
  with per-turn recall into a single `context_injections` surface, and per-turn
  recall injections are logged so the READ path is auditable (#329, PRs #330/#331).

- **Memory/skill eval harness — engine + seed corpus (#118, Phase 1, PR #312).**

- **Content-addressed spawn contracts (prototype, #318)** — handoff drift
  detection between spawner and spawned agent.

### Fixed

- architect: create the topic index in the migration, not the base schema (#315).

### Docs

- README "run vs develop" install note documenting the pinned pipx path (#297).

## [0.9.0] — 2026-07-10

### Added

- **`techne doctor` — core-DB health audit (#295, PR #296)** — a new top-level
  command that flags silent-accumulation bugs (duplicate rows a table shouldn't
  have) and reports reclaimable freelist space, with `--vacuum` to reclaim it in
  place. Exits non-zero on duplication debt, so it's safe to run in CI or a
  scheduled audit. Extensible via a `_DEDUP_CHECKS` registry of `(table, key)`.

- **Per-turn recall injection (#109 / #277)** — an opt-in `UserPromptSubmit` hook
  (`hooks.per_turn_recall`, default off) that runs a knowledge search scoped to each
  submitted prompt and injects the most relevant durable facts via
  `hookSpecificOutput.additionalContext` — the **dynamic** per-turn complement to the
  **stable** SessionStart injection. Strictly best-effort: never blocks the turn
  (always exit 0), no-ops on flag-off / empty prompt / no match / timeout, and
  excludes soft-archived rows (mirrors egress discipline). `per_turn_recall_limit`
  (default 5) caps rows per turn. The stdin prompt-field name is undocumented in the
  CC hooks reference, so the hook defensively accepts `prompt` / `user_prompt` / `text`
  and no-ops safely otherwise. (Note: whether Claude Code exploits Anthropic prompt
  caching across the stable/dynamic boundary is harness-controlled and undocumented —
  this delivers the per-turn recall capability regardless; any cache benefit is a bonus
  techne can't set from a hook.)

- **Federation brick #1: deposit-and-drain (#221)** — the first step toward
  one-distiller-many-sources memory federation.

  **Pattern:** an ephemeral producer (a short-lived agent, a CI job, a sidecar)
  deposits raw observations into a techne store it doesn't own by calling
  `record_observation(config, ...)` with a `Config` whose `db_path` points at
  the foreign store.  A single distiller (`drain.py`) then drains that store
  asynchronously — no LLM call, no blocking on the deposit path (accept-fast /
  distill-async).

  **Primitives:** the deposit and drain primitives pre-existed; this brick
  proves the round-trip with an acceptance test suite and adds a discoverability
  CLI surface.

  **Idempotency (two signals):**
  - *Signal A* — `knowledge_candidates.source_session_id`: once a session has
    been distilled (candidates written), drain.py's `NOT IN` worklist query
    excludes it on subsequent fire-ups.  Tested end-to-end.
  - *Signal B* — `.drained.json` fingerprint: drain.py filesystem state for
    empty-yield sessions (no candidates written).  Handled by drain.py; noted
    in the test comments.

  **Sovereignty:** deposit writes raw observations only; distillation stays
  the distiller's async job.  The local caller's store is never touched.

  **New CLI command:** `techne deposit --target <foreign techne home>
  --session-id <sid> --content "..."` — the explicit "deposit into a store you
  don't own" surface.  Mirrors `techne observe` in option shape; targets an
  independent foreign store.

  **Follow-up:** candidates land in the foreign store; Archivist-promotion
  (surfacing them to the owning agent's curated memory) is tracked in #222.

- **Codex db-per-repo (#209, PR #286)** — each indexed repo now gets its **own**
  code-graph db at `<data_dir>/codex/<slug>-<hash8>.db`, derived deterministically
  from the repo's resolved absolute root, replacing the single shared `codex.db`.
  Indexing a second repo can no longer collide with, overwrite, or bleed into the
  first. A pre-#209 single `codex.db` migrates to its per-repo home automatically
  (WAL-checkpointed move) the first time any consumer — CLI, hook, or MCP — resolves
  a codex db. New API: `repo_db_path` / `codex_db_for_repo` /
  `resolve_repo_db_for_query` / `list_codex_repos` / `migrate_legacy_codex_db`. New
  CLI: `techne codex index-all` (refresh every indexed repo) and
  `techne codex stats --all|--repo`. Read consumers (MCP query tools, grep-assist)
  scope to the repo under cwd, or `--repo`.

- **grep-assist — graph-augment the agent's own Grep (#206, PR #208)** — an opt-in
  `PreToolUse` hook (`hooks.codex_grep_assist`, default off) that runs a Codex symbol
  search for a Grep's pattern and injects the matching symbols (name, signature, file,
  freshness marker) into the model's context via `additionalContext`. Never blocks the
  tool; degrades to a no-op on any error, a missing/empty index, or a wall-clock
  timeout. Scopes symbols to the repo actually being searched (via the #209 db-per-repo
  router), fixing the single-global-graph signal/noise problem. `codex_grep_assist_limit`
  (default 8) caps injected symbols. Claude Code loads hooks at session start, so it
  fires from the next session after `techne init`.

- **Hook-registration drift check (#287, PR #289)** — `check_hook_drift(config)`
  compares the techne hooks enabled in config against what's actually wired into Claude
  Code's `settings.json`; `techne status` surfaces a `⚠ Hook registration drift — run
  'techne init' to reconcile` warning when they diverge (e.g. grep-assist ON in config
  but the `PreToolUse` group missing after a re-init or stray edit).

- **Test suite no longer clobbers the real `~/.claude` (#288, PR #289)** — a
  `conftest.py` guard points hook-registration tests at a throwaway home, so running the
  suite can't strip or rewrite a developer's real `settings.json`.

### Fixed

- **`thread_boundaries` duplicate-row debt (#295, PR #296)** — a schema **v17**
  migration collapses the duplicate `(session_id, obs_index_a, obs_index_b)` rows
  (keeps the most recent per triple) that had accumulated because `run_session_end`
  re-inserts a session's full boundary set every turn, and no backfill had shipped
  after the delete-before-insert write-path fix. A new `UNIQUE` index makes any future
  non-atomic regression fail loudly instead of silently re-accumulating; the redundant
  prefix index is dropped (the unique index serves `load_boundaries` as a prefix). On
  the reference DB this collapsed 76,343 rows → 9,438 and reclaimed ~26% after `VACUUM`.

## [0.8.0] — 2026-06-28

The "memory that maintains itself, honestly" release: a self-curating memory loop
that runs on the agent's own model, a matured db-per-domain architecture, a full
data-integrity audit, and a leaner, self-teaching install.

### Highlights
- **Memory hygiene on the agent's own model** — reflect, consolidate ("sleep"), and
  claim-evidence reconciliation now run via a render-prompt / answer / ingest loop using
  the model already in the room. No API key, no extra inference.
- **litellm is now optional** (`[llm]` extra) — the minimal `pip install techne` drops from
  ~100 MB to ~10–15 MB.
- **MCP self-teaching** — the server now ships an `instructions` block (the memory loop) so
  any MCP client's agent knows *when* to store/recall, not just that the tools exist.
- **Full data-integrity + robustness audit** (#165–#170).

### Added
- **Memory hygiene loop (agent's own model, no key):** resident-model fact extraction over
  MCP (#19), reflect candidate-model foundation + decision provenance (#108), consolidate
  "sleep" dedup/merge pass (#108), claim-level evidence reconciliation (#107).
- **Curated-memory drain (#107):** ephemeral memory doctor (file-reference reconciliation),
  persistent drain, and an init coexistence posture (`spool-primary`).
- **Knowledge freshness:** 4-state graded staleness for git-anchored memories (#90);
  cascade-to-uncertain dependency staleness + a recall gate that annotates and down-ranks
  uncertain rows (#117).
- **Codex code-graph domain — EXPERIMENTAL (#89):** tree-sitter symbol/edge extraction
  (Python + TypeScript), `callers`/`callees`/reachability/blast-radius graph queries, and
  `codex_search` / `codex_blast_radius` MCP tools. Indexing is manual (the graph can go
  stale; re-run `codex index`). Behind the `[codex]` extra.
- **Architect:** pattern relation/applies graph + `pattern_search` / `pattern_related` MCP
  tools (#87).
- **Operations:** cross-DB `fsck` + all-DBs-together `backup`/`restore` (#139).
- **Security:** shell-execution gates — command-chaining block + fail-open CVE pre-install
  audit (#120, `[security]` extra).
- **Eval harness:** deterministic MeME task taxonomy + observability plumbing, plus an LLM
  answerer/judge for negative-polarity tasks (#118).
- **MCP server `instructions`** (the four-step memory loop) + trigger-led `store`/`recall`/
  `search` tool descriptions.
- **sessions.platform read path (#81):** `techne status` platform breakdown and per-thread
  provenance surfaced at injection (cross-platform by design — surfaced, never filtered).
- **Deploy/runtime:** Dockerfile + `.dockerignore` (#123), systemd user unit + inotify
  watch-limit docs (#122), `TECHNE_HOME` to externalize the root (#114), honor
  `CLAUDE_CONFIG_DIR` when reading host files (#115).
- `docs/modules.md` (per-module tour) and `docs/context-architecture.md` (CC-coupling /
  durable-context decision record).

### Changed
- **litellm → optional `[llm]` extra** (was a core dependency). The default hygiene loop uses
  the agent's own model; `[llm]` is only for autonomous/headless operator-model runs. Dropped
  the unused `[anthropic]` extra. Minimal install ~100 MB → ~10–15 MB.
- **db-per-domain matured:** unified migration engine across core/librarian/architect/codex
  (#136, closes #138); `StoreAdapter` is SQLite-only — dead portability surface removed
  (#135, closes #87); personal/team data boundary enforced at write time (#137).
- Retired the hand-edited markdown patterns corpus in favor of `architect.db` (#121).
- Shared resident-model JSON-parse + prompt-hash util `core/_resident.py` (#164);
  `_get_embedding_model` moved to a neutral `core/embeddings.py`.
- Holistic README rewrite (technē framing, full extras map, agent's-own-model model section).

### Fixed
- **Data-integrity + robustness audit (#165–#170):** thread-ID construction unified through
  `thread_row_id` (#165); hook/process hardening against config + input failures (#166);
  `config.save` security + injection/staleness persistence (#167); retrieval robustness —
  empty-FTS, superseded-vec over-fetch, egress band, prototype cache (#168); migration
  idempotency + redeployed COALESCE knowledge FTS triggers (#169); codex edge corruption +
  three SELECT→INSERT TOCTOUs + sync correctness (#170).
- Empty-FTS-query crash closed in one shared guard (#143).
- CC auto-memory dir resolved via git-root slug + `autoMemoryDirectory` (#175).
- Migration cross-process race + anchor too-large degradation, now tested (#141).
- Outbox: bare pending targets migrated to the `sync:` namespace (#80); v10 migration guarded
  when the outbox table is absent (#127).
- Hooks: bounded SIGALRM timeout + exit-0 degradation on all sidecars (#126).
- `enable_load_extension` guard broadened to catch AttributeError/sqlite3 errors (#125);
  reflect persisted-thread FK + candidate-insert guard (#106); config thread fallbacks read
  dataclass defaults (#82); health capture-gap alarm keyed on last observation; staff-review
  bug sweep — supersede, reconciler, FTS, redaction (#131–#134).
- **Low-severity backlog cleanups (#188):** removed dead `hook_error`/`session_id`; documented
  the process-global `_migrated` memo; cached the fastembed availability check in `infer()`;
  `format_resume_context` trust-boundary note + closing-tag neutralisation; filled two test
  gaps (dead-row purge, mixed-batch flush). Trackers #140/#56/#83 triaged.

### Security
- `shell_guard`: anti-command-chaining gate plus an optional `pip-audit` CVE pre-install audit
  that fails open (the basic gate holds even without the auditor) — #120.

### Known limitations
- **Codex is experimental:** indexing is manual; the graph goes stale as code changes
  (auto-refresh is planned). Cross-file edges resolve for ordinary calls; some remain
  unresolved.
- **Vector/semantic recall** requires `[vectors]`; on Python 3.14 it is not yet installable
  (no `onnxruntime` wheel) and degrades to FTS-only.
- The curated-memory drain (#107) is inert when Claude Code auto-memory is disabled.

---

## Earlier releases

Condensed; see the linked issues/PRs and git history for detail.

- **v0.7.0** — db-per-domain: one db file per domain behind a `StoreAdapter` (librarian/
  architect/codex), store registry (#98), Option-D document cards, staff-review batch
  (#100–#104).
- **v0.6.2** — reliability + recall: FTS5 hardening + Porter stemming (#74/#77), SessionStart
  health banner (#76), flock-serialized WAL-safe migrations (#78), outbox gate + purge (#79),
  eval-discipline harness (#68).
- **v0.6.1** — correctness/hygiene (Fable review): FTS double-quote crash fix, search
  provenance separation, supersede guard, atomic redetect/session-end, public-API contract
  tests, `schema_version` race fix, `llm_api_key` chmod 0600.
- **v0.6.0** — embedding techne in custom runtimes: `llm_api_base`/`llm_api_key`,
  caller-supplied session IDs, `init_session_for_external_id`, `cabal` platform.
- **v0.5.1** — review pass: 2 blockers + 5 high + 8 medium fixes, +22 tests.
- **v0.5.0** — JSONL backfill, dual-column session summary (schema v4),
  user-prompt-anchored thread detection, `threads redetect`, platform package consolidation.
- **v0.4.1** — hooks rewritten to Claude Code's wrapped `{matcher, hooks:[...]}` shape.
- **v0.4.0** — heuristic thread detection (no `ANTHROPIC_API_KEY` for the default install);
  schema v3 `thread_boundaries`.
- **v0.3.1** — `init` writes `sys.executable`; `load_adapter` drops `None` kwargs.
- **v0.3.0** — argument-order standardization on `should_capture`/`add_observation` (breaking).
- **v0.2.2** — `capture_read` / `post_tool_use=False` fixes; symmetric `FilesystemAdapter.pull()`.
- **v0.2.1** — hook test coverage, `contacts.md` parser hardening, public API surface.
- **v0.2.0** — LiteLLM as default LLM router, lazy schema init, `techne.lifecycle` module.
- **v0.1.0** — initial release.

[0.8.0]: https://github.com/tenishai/techne/releases/tag/v0.8.0
