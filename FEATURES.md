# Custom Stages + Pluggable Handlers — Feature Catalogue

This document enumerates every feature landed on `feat/custom-stages`, grouped
by subsystem. Each bullet is backed by at least one test case in `tests/`.

The branch adds a stage-aware pipeline model, a pluggable handler contract
(agent / script / webhook / human / k8s_job), atomic role execution with
crash rollback, watcher scope + reactivation, and the UI surfaces to make
all of it visible and operable. Test coverage:

- `tests/test_pipeline_migration.py` — 18 cases
- `tests/test_scheduler_primitives.py` — 26 cases
- `tests/test_handlers.py` — 15 cases
- `tests/test_handler_retry.py` — 31 cases
- `tests/test_script_webhook_handlers.py` — 20 cases
- `tests/test_human_k8s_handlers.py` — 11 cases
- `tests/test_snapshot.py` — 13 cases
- `tests/test_watchers.py` — 13 cases
- `tests/test_failed_role_retry.py` — 9 cases
- `tests/test_browser_custom_stages.py` — 9 Playwright flows

Grand total: **165 automated test cases** plus **176 enumerated behaviours** below.

---

## 1. Data model: stages + role_groups

1. Pipelines carry a `stages: list[Stage]` field.
2. Each `Stage` has a `name`, a `role_groups: list[list[Role]]`, and an optional `watchers` list.
3. Outer `role_groups` are sequential; inner list = parallel roles within a group.
4. Roles are dicts: `{"name": str, "handler": str, ...}`.
5. An implicit `done` sentinel represents "no more stages".
6. `done` can carry watchers but not roles.
7. The out-of-the-box pipeline collapses to a single stage with four sequential role_groups (`ideation → implementation → validation → release`).
8. The default pipeline exposes `competitive-watcher` + `research-watcher` with scope `["*"]`.
9. `default_pipeline()` returns a fresh independent dict (no shared mutable state).
10. Every default pipeline still carries legacy `agents` / `post_ready` / `parallel_groups` keys for back-compat.
11. `initial_stage_fields()` gives every new idea `current_stage`, `stage_history`, `role_state`.

## 2. Pipeline migration

12. `migrate_pipeline()` accepts the old flat shape and synthesises `stages[]`.
13. It accepts the new shape and back-fills legacy keys from `stages[0]`.
14. It is idempotent — running twice yields the same output.
15. It never mutates its input.
16. A pipeline where `stages` is a list of strings (very old format) is auto-rewritten to `agents`.
17. A pipeline missing `parallel_groups` gets one synthesised from `agents` + `post_ready`.
18. An empty pipeline falls back to the default.

## 3. Idea-status backfill

19. `backfill_stage_fields()` is a pure read-path helper.
20. If an idea has no `current_stage`, one is derived from `pipeline.stages[0]`.
21. Legacy `stage_results[role]=proceed` maps to `role_state[current_stage][role].state=proceed`.
22. Legacy `last_serviced_by[role].at` maps to `role_state[current_stage][role].completed_at`.
23. Legacy `iter_counts` map to `role_state[...].iterations`.
24. `stage_history` gets a minimal anchor entry when absent.
25. Idempotent — already-migrated statuses are passed through unchanged.

## 4. Scheduler primitives

26. `eligible_roles(status)` returns the roles the scheduler may dispatch now.
27. For a fresh idea on the default pipeline it returns `["ideation"]`.
28. Completed roles are excluded from the eligible set.
29. When a role_group has multiple roles, all non-completed members are returned (parallel dispatch).
30. A stage with every role_group complete returns `[]` (caller advances).
31. An idea in `done` returns `[]`.
32. Roles in `state="iterate"` remain eligible for the next cycle.
33. `stage_is_complete(status)` is true iff every role_group's roles are at `proceed`.
34. `done` is trivially complete.
35. `next_stage_name(pipeline, current)` walks the ordered `stages` list.
36. When the current stage is the last, it returns `DONE_STAGE`.
37. An unknown current stage also returns `DONE_STAGE` (fail-closed).
38. `compute_stage_advancement(status)` returns `None` while the stage is mid-flight.
39. When complete, it returns new `{current_stage, stage_history, role_state}`.
40. The leaving stage's `stage_history` entry gets `exited_at` set.
41. The new stage's `stage_history` entry is appended with `entered_at` and `exited_at=None`.
42. `role_state` gets a fresh empty sub-dict for the new stage.
43. Single-stage pipelines advance directly to `done`.
44. `done` does not re-advance.
45. `set_role_state(status, role, state)` is pure and produces the new `role_state` dict.
46. First mark sets `iterations=1`.
47. Subsequent marks increment `iterations` by one.
48. An explicit `iterations=` kwarg overrides the auto-increment.
49. `completed_at` is recorded when supplied.
50. `set_role_state` never mutates its input.

## 5. Blackboard API (stage-aware)

51. `blackboard.next_roles_in_current_stage(idea_id)` returns eligible roles.
52. `blackboard.is_done(idea_id)` returns True iff the idea sits in the `done` sentinel.
53. `blackboard.mark_role_state(idea_id, role, state, completed_at=)` atomically writes the role_state delta.
54. `blackboard.advance_stage_if_complete(idea_id)` returns the new stage name or `None`.
55. Advancement is a single atomic `status.json` write (matches the spec's "own commit" rule).
56. Watcher scope check `blackboard.watcher_is_in_scope(idea_id, watcher_config)` is one call.
57. `blackboard.reactivate_from_done(idea_id, payload, watcher_name=)` applies a watcher-driven reactivation.
58. `blackboard.clear_failed_role(idea_id, role, actor=, note=)` resets a failed role to `pending`.
59. Failed-role reset appends a `phase_history` entry with actor + note + timestamp.
60. Resetting a role that isn't in `failed` raises `ValueError` instead of silently no-oping.

## 6. Handler contract (JSON-in / JSON-out)

61. `Handler` is an abstract `async def serve(ctx) -> HandlerResponse` interface.
62. `HandlerContext` carries the JSON-in wire shape: `event`, `idea_id`, `stage`, `role`, `iteration`, `blackboard_dir`, `workspace_dir`, `previous_state`, `inputs`.
63. `ctx.to_json()` returns exactly the spec's stdin shape (no extra internal fields).
64. Internal fields like `deadline` are not serialised.
65. `HandlerResponse` carries `state`, `reason`, `artifacts`, `cost_usd`, `metrics`.
66. Agent-specific fields (`session_id`, `sandbox_failure`, `transcript`) are internal-only.
67. `state` is typed as `"proceed" | "iterate" | "needs_review" | "failed"`.
68. `create_handler(role_config, agent=)` dispatches on `role_config["handler"]`.
69. Missing `handler` defaults to `agent` (back-compat).
70. Unknown handler types raise `NotImplementedError` with guidance.

## 7. Agent handler

71. `AgentHandler(base_agent)` wraps the existing `BaseAgent.run()`.
72. Success maps to `state="proceed"`.
73. Agent failure (`result.success=False`) maps to `state="iterate"`.
74. Sandbox breach (`result.sandbox_failure=True`) maps to `state="iterate"`.
75. `cost_usd`, `session_id`, `stop_reason`, `transcript`, `sandbox_failure` flow through.
76. `deadline` is forwarded to `agent.run(deadline=...)`.
77. `metrics.stop_reason` is populated when present.

## 8. Script handler

78. `ScriptHandler(command=..., env=..., cwd=...)` wraps a subprocess.
79. `command` accepts a string (shlex-split) or a list.
80. Missing/empty command raises `ValueError`.
81. Context JSON is piped on stdin.
82. JSON on stdout is parsed into `HandlerResponse`.
83. Legacy exit-code mode: exit 0 → `proceed`, non-zero → `iterate`.
84. Stderr's last line becomes `reason` on non-zero exit.
85. `cwd` defaults to `workspace_dir`.
86. Extra env vars merge on top of `os.environ`.
87. The subprocess runs in a worker thread so the event loop stays free.

## 9. Webhook handler

88. `WebhookHandler(url=..., method=..., headers=...)` POSTs JSON.
89. Missing URL raises `ValueError`.
90. Method defaults to POST; normalised to uppercase.
91. 2xx with a `{"state": ...}` JSON body is parsed into `HandlerResponse`.
92. 2xx without a `state` body defaults to `proceed`.
93. 4xx responses map to `state="failed"`.
94. 5xx responses map to `state="iterate"` (retryable).
95. Custom headers merge over the default `Content-Type: application/json`.

## 10. Human handler

96. `HumanHandler()` writes a pending request file under `blackboard/<idea>/human-requests/<role>/request.json`.
97. Returns `state="needs_review"` so the scheduler pauses the role.
98. Idempotent: re-invocation does not overwrite an in-progress request file.
99. The written request carries the full handler context JSON.
100. Request filename is configurable via `request_file_name=`.

## 11. K8s job handler

101. `K8sJobHandler(image=..., client=..., namespace=..., resources=..., result_key=..., env=...)` dispatches a Kubernetes Job.
102. Missing image raises `ValueError`.
103. Missing client at `serve()` time raises a descriptive `NotImplementedError` instead of silently hanging.
104. `create_job` / `wait_for_completion` / `read_result` / `cleanup` is the injected adapter surface.
105. Job labels include `trellis/idea`, `trellis/stage`, `trellis/role`.
106. Result JSON from the Job's shared volume is parsed into `HandlerResponse`.
107. Jobs with no result payload default to `proceed`.
108. Failed jobs return `state="iterate"`.
109. Cleanup runs in a `finally` block; exceptions there are logged but never mask a result.

## 12. Retry + timeout (policy wrapper)

110. `RetryPolicy(max_attempts, backoff, initial_delay_sec, max_delay_sec, timeout_sec, on_timeout, terminal_state)` captures the spec's config.
111. `RetryPolicy.from_role_config(cfg)` parses the role YAML block.
112. `parse_duration("10m")` returns `600`, `"30s"` → `30`, plain ints → seconds, unparseable → `None`.
113. Default policy is a no-op (`is_active()` returns False).
114. Exponential backoff ladder: `30 → 60 → 120 → 240 → 300` (cap).
115. Linear backoff: `n, 2n, 3n, …` clamped at `max_delay_sec`.
116. Fixed backoff returns the initial delay for every attempt.
117. `with_retry(handler, policy)` returns the unwrapped handler when the policy is inactive.
118. Successful first attempt short-circuits remaining attempts.
119. `iterate` results trigger the next attempt with the configured delay.
120. `needs_review` short-circuits (human-gated — not automatic retry).
121. Unhandled exceptions inside the wrapped handler are treated as `iterate`.
122. Per-attempt timeout converts `asyncio.TimeoutError` into `state="iterate"`.
123. `on_timeout="fail"` terminates immediately with the policy's `terminal_state`.
124. Exhausting all attempts returns `state=terminal_state` with an "(exhausted N attempts)" reason.
125. The factory auto-wraps any handler whose role_config sets `timeout` or `retry`.

## 13. Atomicity (git-as-patch)

126. `BlackboardSnapshot(base, idea_id, role=, stage=)` is the per-run transaction.
127. `snapshot()` copies the idea dir to `/tmp/trellis-staging-<uuid>`.
128. A lock file lands in `<bb>/.trellis-locks/<idea>.lock` with `{idea_id, role, stage, staging_path, started_at}`.
129. Snapshot raises `SnapshotError` if a lock already exists (concurrent run).
130. Snapshot raises if the idea dir doesn't exist.
131. `commit()` removes the lock first, then the staging dir (safe mid-crash ordering).
132. `rollback()` restores the idea dir from staging using rmtree+copytree.
133. Rollback is idempotent — safe to call twice.
134. The `BlackboardSnapshot` context manager commits on success, rolls back on exception.
135. In-run writes (artifacts, transient status mutations) survive `commit()`.
136. Garbage writes disappear on `rollback()`.
137. `recover_crashed_runs(base)` scans `.trellis-locks/` and rolls back each stale lock.
138. Orphan staging dirs under `/tmp/trellis-staging-*` (no matching lock) are GC'd.
139. Corrupt lock files are deleted rather than crashing startup.

## 14. Watchers: scope + reactivation

140. `watcher_is_in_scope(config, status)` evaluates positive-list scope rules.
141. `scope: ["*"]` always fires.
142. Missing `scope` defaults to wildcard (back-compat with today's `post_ready`).
143. `scope: ["role_name"]` fires only while that role is in the eligible set.
144. Multiple role names are OR'd.
145. `scope: ["done"]` fires only while the idea is in `done`.
146. `apply_reactivation(status, payload, watcher_name=)` is a pure transform.
147. Missing `to_stage` in the payload raises `ValueError`.
148. Target stage must exist or be supplied via `add_stage`.
149. `add_stage` inserts a new stage and tags it `added_by=<watcher>`, `added_at=<iso>`.
150. Re-applying the same reactivation is idempotent (no duplicate stages).
151. Reactivation can target an already-existing stage without `add_stage`.
152. `stage_history` appends a `{reactivated_by: <watcher>}` entry.
153. The target stage gets a fresh empty `role_state` sub-dict.

## 15. Scheduler integration (pool wiring)

154. `pool._pipeline_producer` now calls `next_roles_in_current_stage` (stage-aware dispatch).
155. Producer falls back to legacy `next_agent` when the status isn't in the new shape yet.
156. Role-group parallelism: every eligible role in a group is enqueued in the same pass.
157. Result handler mirrors `stage_results[role]` into `role_state[current_stage][role]` atomically.
158. Result handler calls `advance_stage_if_complete` after each proceed so stages transition promptly.
159. `status.get("phase_recommendation") or "proceed"` (not `get(..., "proceed")`) handles `create_idea`'s explicit None.
160. `can_schedule` still honours `parallel_groups` for single-idea role serialisation.

## 16. CLI + HTTP surfaces

161. `POST /ideas/{id}/roles/{role}/retry` clears a failed role. 404 if idea missing, 409 if not failed.
162. `trellis retry <idea> <role> [--note ...]` is the CLI counterpart.
163. `POST /ideas/{id}/stages` appends a new named stage. Validates `^[a-z0-9_-]{1,40}$`; rejects `done`; 409 on duplicates.
164. `POST /ideas/{id}/stages/{name}/rename` renames a stage; updates `current_stage`, `role_state`, `stage_history`.
165. Both stage endpoints return JSON so the UI can act on status codes.

## 17. UI surfaces

166. Idea detail page renders a `stage: <name>` pill in the header when `current_stage != 'default'`.
167. Nested Progress card: outer bar highlights stages; inner chips show every role in the current stage's groups.
168. Role chips are color-coded by state (`proceed` = emerald, `iterate` = amber, `needs_review` = violet, `failed` = rose, `pending` = muted).
169. Chips show a single-letter handler-type badge (`A` for agent, `S` for script, `W` for webhook, `H` for human, `K` for k8s_job).
170. Parallel role groups render a "parallel" label in front of the chip row.
171. Done stage reads "complete · N stages" instead of an arithmetic counter.
172. "+ Add stage" button lives inline on the Progress card and prompts for a name.
173. Pipeline editor stage-list renders a handler-type badge next to each role, color-matched to the palette.
174. Failed-role panel shows a red-accented card above the pipeline editor with attempt count + Retry button.
175. Retry button uses `fetch()` for the JSON endpoint and reloads on success; surfaces 409 errors in an alert.

## 18. Projection store

176. `projection.upsert_idea` now carries `current_stage`, `stage_history`, `role_state` alongside legacy `stage_results` / `last_serviced_by` / `phase_history`.

---

## Deferred (non-goals for this branch)

The spec explicitly defers these; they're intentionally absent:

- **Secrets** handling for handlers — scripts/webhooks read from the parent env for now.
- **Permissions / RBAC** — trellis remains a single-user app.
- **Per-blackboard `.git` repos** — git is used only as a patching primitive, no persistent repo per idea.
- **k8s_job wiring** — the handler + protocol ship; the caller must inject a k8s client adapter.
- **Full multi-stage pipeline editor UI** — the editor today edits `stage[0]` only; richer stage editing happens via YAML or the `POST /stages` endpoints.
