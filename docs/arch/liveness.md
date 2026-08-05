---
card: arch-card:techne-liveness
updated: 2026-08-04
anchor: techne/core/checks.py sha=2264dbbbd51666f7740a2ece8b3fdba6c3328a11
---
# liveness — subsystem health checks (techne#468/#469)

WHAT: answers "did each rail actually RUN?", as distinct from "is the store internally consistent?" (storage checks) and "is it even WIRED?" (hook-registration drift). Born from 2026-08-02/03, when six built-but-never-wired rails were found cold — `reflect` for four weeks — under a `techne doctor` printing "All checks passed", because doctor only looked at duplicate rows and reclaimable space.
SEAM: a registry (`register_check` / `registered_checks` / `run_checks`) of `CheckFn(config) -> CheckResult | list[CheckResult]`, plus the marker-writing side in `techne/hooks/_liveness.py` (`record_hook_run`, one `hook:<name>:last_run` / `:last_error` row per hook in `extension_state`).
VERDICTS (`Verdict`, ordered worst-last so `max()` finds the headline): live · dormant · stale · unobservable · broken · unknown. `CheckResult.ok` is true for live and dormant ONLY.
CONSUMERS: `techne doctor` (`cli.py:_render_liveness`), the SessionStart banner (`core/health.py` — `checks_worst` / `checks_summary` / `checks_error`), and `techne status`.

INVARIANTS — the ones a diff will not show you:

- **UNOBSERVABLE IS A FAILING STATE, NOT A WARNING.** A rail that cannot report is unfinished. "It might be fine" must never render as passing; that ambiguity is the whole reason this subsystem exists. (techne#468, Richard: "i want to know that when i run something it works, or it doesnt.")
- **CHECKS REPORT, THEY NEVER SELF-HEAL.** Print the remedy command; do not run it.
- **NEVER CLAIM MORE THAN WAS OBSERVED.** `failing == 0` is not "everything is live". A store where every check is DORMANT gets "0 of N confirmed live", never "All N confirmed live" — confirming the absence of evidence is the headline bug this subsystem was built to remove, and it has been reintroduced by accident more than once.
- **SESSION-SCOPED HOOKS ARE JUDGED AGAINST THE LAST SESSION, NOT THE CLOCK.** For a hook that only fires when a session starts, "days since last fire" measures how recently the OPERATOR WORKED. The comparison uses the SECOND-newest settled session (`LIMIT 1 OFFSET 1`), because the newest one is the session currently asking.
- **A MARKER IS STAMPED AT COMPLETION, NOT AT ENTRY.** `last_run` written from the hook's start time can read older than the session it just registered, which renders STALE on a working hook. (One deliberate exception, commented at the site: the session-registration failure path, where a recorded `hook_error` short-circuits to BROKEN before the comparison is reached.)
- **_NEW_STORE_GRACE (7d).** Under it, "no sessions yet" is DORMANT — nothing has run. Past it, the same absence IS the finding and escalates to UNOBSERVABLE. Tests that want the failing branch must age the store past the grace; tests that want the dormant branch must stay inside it.
- **LIVENESS AND DRIFT ARE COUNTED SEPARATELY.** `_render_liveness` returns `(liveness_failures, drift_failures)`. Adding them made one run print "Nothing failing, but 0 of 12 subsystems confirmed live" and "5 subsystem(s) not confirmed working" seven lines apart.
- **DRIFT IS CLAUDE-CODE ONLY.** `check_hook_drift` does NOT self-guard — every caller must gate on `config.platform != CLAUDE_CODE` itself. Off that platform there is no settings.json mechanism to drift from.
- **A REMEDY IS BUILT, NEVER SPELLED.** `init_remedy(config)` is the single builder: `None` off claude-code, and `--posture` always interpolated from `config.posture.mode`. A hardcoded `--posture observe` DEMOTES a spool-primary operator who follows it.
- **A CHECK THAT RAISES BECOMES UNKNOWN CARRYING THE EXCEPTION — NEVER AN ABSENT RESULT.** An absent check reads as "nothing wrong", which is the failure mode this module exists to prevent. Same rule on the banner (`checks_error` is rendered, not swallowed) and in doctor's drift block.

FAIL-MODES:
- A marker rail is only as live as its DEPLOYED code: markers stop being written entirely if the runtime is pinned to a revision without the `record_hook_run` calls. Merged is not deployed.
- Tests that invoke `doctor`/`collect_health` without pinning `Config.load` read `TECHNE_HOME` first and grade the DEVELOPER'S REAL STORE, not their fixture — assert the fixture's db path appears in the output.
- Registration drift and liveness are blind to each other by design: drift compares by `(event, matcher)` presence only, so a hook registered under the WRONG INTERPRETER is caught by neither. Known, open gap.
