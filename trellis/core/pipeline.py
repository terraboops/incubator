"""Pipeline data model + migrations.

Per the custom-stages spec, pipelines use `stages -> role_groups -> roles`
hierarchy instead of the legacy flat `agents`/`post_ready`/`parallel_groups`
layout. Both shapes coexist during the transition — this module owns the
dict-level migration that synthesizes one from the other so old callers
(`pipeline["agents"]`, `pipeline["post_ready"]`) keep working.

The idea-status migration here runs in-memory on read: old ideas with a
`phase` enum but no `current_stage` get derived fields computed so the new
scheduler has what it needs without a batch migration pass.
"""

from __future__ import annotations

from typing import Any

from trellis.core.phase import Phase

DEFAULT_STAGE_NAME = "default"
DONE_STAGE = "done"

# Flat list preserved for legacy call sites. Don't read from this directly in
# new code — use `get_pipeline(idea_id)["stages"]` instead.
LEGACY_AGENTS = ["ideation", "implementation", "validation", "release"]
LEGACY_WATCHERS = ["competitive-watcher", "research-watcher"]


def default_pipeline() -> dict[str, Any]:
    """Return the out-of-the-box pipeline (single stage, four sequential roles)."""
    role_groups = [[{"name": name, "handler": "agent"}] for name in LEGACY_AGENTS]
    watchers = [{"name": w, "handler": "agent", "scope": ["*"]} for w in LEGACY_WATCHERS]
    return {
        "name": "default",
        "stages": [
            {
                "name": DEFAULT_STAGE_NAME,
                "role_groups": role_groups,
                "watchers": watchers,
            }
        ],
        # Legacy keys retained for back-compat readers.
        "agents": list(LEGACY_AGENTS),
        "post_ready": list(LEGACY_WATCHERS),
        "parallel_groups": [list(LEGACY_AGENTS), list(LEGACY_WATCHERS)],
        "gating": {"default": "auto", "overrides": {}},
        "preset": "full-pipeline",
    }


def migrate_pipeline(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Return a pipeline dict containing BOTH legacy keys and `stages`.

    - Old shape in → legacy keys preserved, `stages` synthesized as single stage.
    - New shape in → `stages` preserved, legacy keys derived from stage 0.

    Idempotent. Non-destructive: does not mutate the input dict.
    """
    p = dict(pipeline)

    # Very-old-format detection: `stages` used to be an alias for the flat
    # agents list (i.e. list of role-name strings). If stages looks like a
    # list of strings rather than stage dicts, treat it as legacy `agents`.
    raw_stages = p.get("stages")
    if isinstance(raw_stages, list) and raw_stages and not isinstance(raw_stages[0], dict):
        p.setdefault("agents", list(raw_stages))
        p.pop("stages")

    if "agents" not in p and "stages" not in p:
        # Totally empty pipeline — bail to default.
        return default_pipeline()

    if "stages" not in p:
        p = _synthesize_stages_from_legacy(p)
    else:
        p = _synthesize_legacy_keys(p) if "agents" not in p else p

    # Ensure parallel_groups exists (older configs could miss it)
    if "parallel_groups" not in p:
        agents = p.get("agents", [])
        post_ready = p.get("post_ready", [])
        groups: list[list[str]] = []
        if agents:
            groups.append(list(agents))
        if post_ready:
            groups.append(list(post_ready))
        p["parallel_groups"] = groups

    p.setdefault("gating", {"default": "auto", "overrides": {}})
    return p


def _synthesize_stages_from_legacy(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Build the `stages` list from legacy agents/post_ready/parallel_groups."""
    p = dict(pipeline)
    agents = p.get("agents", p.get("stages_legacy", []))
    post_ready = p.get("post_ready", [])
    parallel_groups = p.get("parallel_groups")

    if parallel_groups:
        # Find the group that matches the sequential agents list; use it for
        # role_groups if it's the first group (sequential-per-group). Groups
        # beyond the first that aren't post_ready are ignored in this
        # synthesis — if the user actually had parallel agents today, migrate
        # into the explicit role_groups shape going forward.
        seq = next((g for g in parallel_groups if set(g) == set(agents)), None)
        if seq is not None:
            role_groups = [[{"name": name, "handler": "agent"}] for name in seq]
        else:
            role_groups = [[{"name": name, "handler": "agent"}] for name in agents]
    else:
        role_groups = [[{"name": name, "handler": "agent"}] for name in agents]

    watchers = [{"name": w, "handler": "agent", "scope": ["*"]} for w in post_ready]

    p["stages"] = [
        {
            "name": DEFAULT_STAGE_NAME,
            "role_groups": role_groups,
            "watchers": watchers,
        }
    ]
    return p


def _synthesize_legacy_keys(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Flatten a stages-based pipeline back to the legacy agents/post_ready keys.

    Legacy keys reflect stage[0] only. Good enough during the transition:
    today every idea has a single stage. Multi-stage ideas are a new feature
    and by definition callers that want to read them should use `stages`.
    """
    p = dict(pipeline)
    stages = p.get("stages") or []
    if not stages:
        return p
    first = stages[0]
    role_groups = first.get("role_groups", [])
    # Flatten: agents = every role name across role_groups, sequential order
    agent_names: list[str] = []
    for group in role_groups:
        for role in group:
            name = role.get("name") if isinstance(role, dict) else str(role)
            if name:
                agent_names.append(name)
    watchers = first.get("watchers") or []
    watcher_names = [w.get("name") if isinstance(w, dict) else str(w) for w in watchers]
    p.setdefault("agents", agent_names)
    p.setdefault("post_ready", [n for n in watcher_names if n])
    return p


# ─── Idea status migration ────────────────────────────────────────────────


# Legacy phase → role name (for mapping old `phase` enum into `role_state`).
_PHASE_TO_ROLE: dict[str, str] = {
    Phase.IDEATION.value: "ideation",
    Phase.IMPLEMENTATION.value: "implementation",
    Phase.VALIDATION.value: "validation",
    Phase.RELEASE.value: "release",
}


def initial_stage_fields(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Fresh `current_stage`/`stage_history`/`role_state` for a new idea.

    Called by `create_idea` so the new fields are there from day one.
    """
    from datetime import datetime, timezone

    stages = pipeline.get("stages") or []
    first_stage = stages[0]["name"] if stages else DEFAULT_STAGE_NAME
    now = datetime.now(timezone.utc).isoformat()
    return {
        "current_stage": first_stage,
        "stage_history": [{"stage": first_stage, "entered_at": now, "exited_at": None}],
        "role_state": {first_stage: {}},
    }


def backfill_stage_fields(status: dict[str, Any]) -> dict[str, Any]:
    """Return status with `current_stage`/`stage_history`/`role_state` set.

    Idempotent. For ideas created before the stage fields existed, derive
    them from `phase` + `stage_results` + `last_serviced_by`. Callers may
    persist the result or use it read-only.
    """
    if "current_stage" in status and "role_state" in status:
        return status  # already migrated

    result = dict(status)
    pipeline = result.get("pipeline") or {}
    stages = pipeline.get("stages") or []
    stage_name = stages[0]["name"] if stages else DEFAULT_STAGE_NAME

    result["current_stage"] = stage_name

    # Derive role_state from the legacy stage_results + last_serviced_by.
    serviced: dict[str, Any] = result.get("last_serviced_by", {}) or {}
    stage_results: dict[str, str] = result.get("stage_results", {}) or {}
    iter_counts: dict[str, int] = result.get("iter_counts", {}) or {}

    per_role: dict[str, dict[str, Any]] = {}
    for role, info in serviced.items():
        state = stage_results.get(role, "pending")
        entry: dict[str, Any] = {
            "state": state,
            "iterations": iter_counts.get(role, 1),
        }
        if isinstance(info, dict):
            if "at" in info:
                entry["completed_at"] = info["at"]
        per_role[role] = entry
    for role, state in stage_results.items():
        per_role.setdefault(role, {"state": state, "iterations": iter_counts.get(role, 0)})
    result["role_state"] = {stage_name: per_role}

    # Synthesize a minimal stage_history anchored on created_at if missing.
    if "stage_history" not in result:
        created = result.get("created_at") or result.get("updated_at") or ""
        result["stage_history"] = [{"stage": stage_name, "entered_at": created, "exited_at": None}]

    return result


# ─── Scheduler primitives (pure functions; no I/O) ────────────────────────


def _role_names_in_group(group: list) -> list[str]:
    names: list[str] = []
    for role in group:
        name = role.get("name") if isinstance(role, dict) else str(role)
        if name:
            names.append(name)
    return names


def _find_stage(pipeline: dict, stage_name: str) -> dict | None:
    for stage in pipeline.get("stages") or []:
        if stage.get("name") == stage_name:
            return stage
    return None


def _group_is_complete(group: list, role_state: dict) -> bool:
    """A role_group is complete when every role in it reached `state=proceed`."""
    for name in _role_names_in_group(group):
        entry = role_state.get(name) or {}
        if entry.get("state") != "proceed":
            return False
    return True


def eligible_roles(status: dict) -> list[str]:
    """Roles that the scheduler may dispatch right now.

    Returns the subset of the first incomplete role_group that hasn't yet
    reached `state=proceed`. All returned roles may run concurrently.
    Returns [] when the stage is complete (caller should advance) or when
    the idea is in `done`.
    """
    stage_name = status.get("current_stage", DEFAULT_STAGE_NAME)
    if stage_name == DONE_STAGE:
        return []
    pipeline = migrate_pipeline(status.get("pipeline") or {})
    stage = _find_stage(pipeline, stage_name)
    if not stage:
        return []
    role_state = (status.get("role_state") or {}).get(stage_name, {}) or {}
    for group in stage.get("role_groups") or []:
        if _group_is_complete(group, role_state):
            continue
        # Return only the roles within this group that still need to run.
        return [
            name
            for name in _role_names_in_group(group)
            if (role_state.get(name) or {}).get("state") != "proceed"
        ]
    return []  # every group complete → stage done


def stage_is_complete(status: dict) -> bool:
    """True when every role_group in the current stage has all roles at proceed."""
    stage_name = status.get("current_stage", DEFAULT_STAGE_NAME)
    if stage_name == DONE_STAGE:
        return True
    pipeline = migrate_pipeline(status.get("pipeline") or {})
    stage = _find_stage(pipeline, stage_name)
    if not stage:
        return False
    role_state = (status.get("role_state") or {}).get(stage_name, {}) or {}
    return all(_group_is_complete(g, role_state) for g in (stage.get("role_groups") or []))


def next_stage_name(pipeline: dict, current: str) -> str:
    """Given the pipeline and the current stage, return the next stage name
    (the one to advance to). Returns `DONE_STAGE` when there are no more stages.
    """
    stages = pipeline.get("stages") or []
    for i, stage in enumerate(stages):
        if stage.get("name") == current:
            if i + 1 < len(stages):
                return stages[i + 1]["name"]
            return DONE_STAGE
    return DONE_STAGE


def compute_stage_advancement(status: dict) -> dict | None:
    """Return the fields to write when advancing the stage, or None to stay.

    Pure function: the caller is responsible for persisting the returned
    dict atomically.
    """
    from datetime import datetime, timezone

    if not stage_is_complete(status):
        return None

    current = status.get("current_stage", DEFAULT_STAGE_NAME)
    if current == DONE_STAGE:
        return None

    pipeline = migrate_pipeline(status.get("pipeline") or {})
    nxt = next_stage_name(pipeline, current)
    now = datetime.now(timezone.utc).isoformat()

    history = list(status.get("stage_history") or [])
    # Mark the current stage's exit_at.
    for entry in reversed(history):
        if entry.get("stage") == current and entry.get("exited_at") is None:
            entry["exited_at"] = now
            break
    history.append({"stage": nxt, "entered_at": now, "exited_at": None})

    role_state = dict(status.get("role_state") or {})
    role_state.setdefault(nxt, {})

    return {
        "current_stage": nxt,
        "stage_history": history,
        "role_state": role_state,
    }


def set_role_state(
    status: dict,
    role: str,
    state: str,
    *,
    iterations: int | None = None,
    completed_at: str | None = None,
) -> dict:
    """Return an updated role_state reflecting this role's new state.

    Pure: does not mutate the input status.
    """
    stage_name = status.get("current_stage", DEFAULT_STAGE_NAME)
    role_state = dict(status.get("role_state") or {})
    stage_state = dict(role_state.get(stage_name) or {})
    existing = dict(stage_state.get(role) or {})
    existing["state"] = state
    if iterations is not None:
        existing["iterations"] = iterations
    elif "iterations" not in existing:
        existing["iterations"] = 1
    else:
        existing["iterations"] = existing["iterations"] + 1
    if completed_at:
        existing["completed_at"] = completed_at
    stage_state[role] = existing
    role_state[stage_name] = stage_state
    return role_state


# ─── Watcher scope + re-activation ────────────────────────────────────────

WILDCARD_SCOPE = "*"


def watcher_is_in_scope(watcher_config: dict, status: dict) -> bool:
    """Is this watcher eligible to fire right now?

    Per the finalized spec (positive lists only, no exclusion):
    - ``scope: ["*"]`` → always fires.
    - ``scope: ["done"]`` → fires when idea is in the ``done`` stage.
    - ``scope: ["ideation"]`` → fires while the ``ideation`` role is
      eligible (i.e. in the currently-active role_group).
    - ``scope: ["ideation", "validation"]`` → fires if either is active.
    - no ``scope`` field → conservatively treated as wildcard (back-compat
      with today's post_ready watchers which had no scope at all).
    """
    scopes = watcher_config.get("scope")
    if not scopes:
        return True  # back-compat with un-scoped watchers
    if WILDCARD_SCOPE in scopes:
        return True

    current_stage = status.get("current_stage", DEFAULT_STAGE_NAME)
    if DONE_STAGE in scopes:
        if current_stage == DONE_STAGE:
            return True

    # Role-name scoping: match against the currently-eligible roles.
    active = set(eligible_roles(status))
    return bool(active.intersection(scopes))


def apply_reactivation(status: dict, reactivate: dict, *, watcher_name: str = "watcher") -> dict:
    """Return the status fields to write when a watcher re-activates a done idea.

    The ``reactivate`` payload shape (from the spec)::

        {
          "to_stage": "hotfix",
          "add_stage": {
            "name": "hotfix",
            "role_groups": [[{"name": "...", "handler": "..."}]],
            "watchers": []   # optional
          }
        }

    Returned dict updates status with:
    - ``pipeline`` — the inline pipeline, with add_stage inserted at the
      current position (per-idea shadow; doesn't touch the template).
    - ``current_stage`` — set to ``to_stage``.
    - ``stage_history`` — appended with a reactivation entry.
    - ``role_state`` — fresh sub-dict for the new stage if absent.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    to_stage = reactivate.get("to_stage")
    if not to_stage:
        raise ValueError("reactivate payload missing required field 'to_stage'")

    pipeline = migrate_pipeline(status.get("pipeline") or {})
    stages = list(pipeline.get("stages") or [])
    add_stage = reactivate.get("add_stage")

    if add_stage:
        # Insert at the end (reactivation always appends; we're coming from
        # `done` which means every stage is already exited).
        new_stage = dict(add_stage)
        new_stage.setdefault("watchers", [])
        new_stage["added_by"] = watcher_name
        new_stage["added_at"] = now
        # Replace any existing stage with the same name (idempotent add).
        stages = [s for s in stages if s.get("name") != new_stage.get("name")]
        stages.append(new_stage)

    if not any(s.get("name") == to_stage for s in stages):
        raise ValueError(
            f"reactivate target stage {to_stage!r} not in pipeline — "
            "include it in add_stage or pre-define it"
        )

    new_pipeline = dict(pipeline)
    new_pipeline["stages"] = stages
    # Rebuild legacy keys after inserting a stage.
    new_pipeline.pop("agents", None)
    new_pipeline = _synthesize_legacy_keys(new_pipeline)
    new_pipeline["parallel_groups"] = [
        [r.get("name") if isinstance(r, dict) else r for r in g]
        for stage in stages
        for g in (stage.get("role_groups") or [])
    ]

    history = list(status.get("stage_history") or [])
    history.append(
        {
            "stage": to_stage,
            "entered_at": now,
            "exited_at": None,
            "reactivated_by": watcher_name,
        }
    )

    role_state = dict(status.get("role_state") or {})
    role_state.setdefault(to_stage, {})

    return {
        "pipeline": new_pipeline,
        "current_stage": to_stage,
        "stage_history": history,
        "role_state": role_state,
    }


__all__ = [
    "DEFAULT_STAGE_NAME",
    "DONE_STAGE",
    "WILDCARD_SCOPE",
    "LEGACY_AGENTS",
    "LEGACY_WATCHERS",
    "default_pipeline",
    "migrate_pipeline",
    "initial_stage_fields",
    "backfill_stage_fields",
    "eligible_roles",
    "stage_is_complete",
    "next_stage_name",
    "compute_stage_advancement",
    "set_role_state",
    "watcher_is_in_scope",
    "apply_reactivation",
]
