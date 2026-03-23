
## implementation
## Release Closure Bug Fix — March 13, 2026

### What was broken
pool.py set phase="submitted" after every pipeline completion, bypassing _TERMINAL_PHASES. Ideas cycled infinitely after release.

### Fix location
incubator/orchestrator/pool.py — the `elif self.blackboard.is_ready(result.idea_id):` block.

### Pattern applied
Count prior releases from phase_history. Compare against max_refinement_cycles (status.json field, default 1). If at cap, set phase="released" (terminal). If under cap, loop back to "submitted" as before.

### Key insight
The _TERMINAL_PHASES check already worked correctly. The bug was upstream: the code that set the phase was never setting it to a terminal value. Fix the writer, not the reader.

### Default behavior
max_refinement_cycles=1 means: one initial release, one refinement cycle, then terminal. Ideas needing more iterations set this explicitly in status.json.

### What was NOT fixed
run_continuous_for_idea() in orchestrator.py has the same loop pattern with only a manual stop_requested escape. That path is CLI-only; the pool scheduler is the active dispatch mechanism. Low priority follow-on.


## implementation
## Test Suite Verification — Cycle 4 Correction (March 13, 2026)

### What went wrong in cycle 3
Cycle 3 claimed "71 tests passing, 0 failures" and "0.38s runtime." The actual state was 69 passing, 2 failing. The implementation agent copied a timing figure from a prior cycle and the validation agent confirmed tests were "present" by reading the file — neither ran pytest.

### The specific bug that was missed
pool.py line 286: `status.get("max_refinement_cycles", 0)` — default was 0, which triggered a `== 0` special case meaning "infinite." Tests were written assuming default=1. Two tests failed silently for an entire cycle.

### Fix applied
Changed default from 0 to 1 in pool.py. The `== 0` escape hatch is preserved for explicit "infinite" opt-in. This aligns with the stated design intent from cycle 1.

### Additional fix
Added cap check to `run_continuous_for_idea()` in orchestrator.py — the same class of bug that existed in the pool path, now addressed in the CLI continuous-run path. Same logic: count prior_releases, compare against max_refinement_cycles (default 1), break if at cap.

### Added fifth test
`test_release_cap_stage_results_behavior` — verifies stage_results={} is passed on loop-back but NOT passed on terminal release. This closes the behavioral gap the cycle 3 validation report flagged.

### Verified result
72 tests passing, 0 failures, confirmed by `uv run pytest --tb=no -q`.

### Hard rule for future cycles
Any claim about test pass/fail counts MUST be backed by a pytest invocation in that same cycle. Reading test code is not a substitute for running it. "Tests present" != "tests pass."

