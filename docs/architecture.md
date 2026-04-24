# Architecture

## Blackboard pattern

Ideas share state through a filesystem-based blackboard at
`blackboard/ideas/<slug>/`. Each idea gets a directory with:

- `status.json` — phase, iteration count, cost tracking, pipeline config, phase history
- `idea.md` — the original idea description
- `feedback.json` — structured feedback entries with identity tracking
- Phase artifacts — self-contained `.html` files (feasibility, market landscape, etc.)
- `agent-logs/` — full transcripts of every agent run
- `agent-knowledge/<agent>/` — per-idea notes from each agent

Agents read and write to the blackboard through MCP tools (`read_blackboard`,
`write_blackboard`, `register_feedback`, `set_phase_recommendation`). This
decouples agents from each other — they communicate only through shared files
and structured feedback entries.

The `_template/` directory defines the initial file structure for new ideas.

## Pipelines

A pipeline defines which agents work on an idea and in what order. Each idea
stores its pipeline config in `status.json`. The default pipeline ships with
four stages, but pipelines are fully customizable.

### Pipeline config

```yaml
agents: [ideation, implementation, validation, release]
post_ready: [competitive-watcher, research-watcher]
parallel_groups:
  - [ideation, implementation, validation, release]
  - [competitive-watcher, research-watcher]
gating:
  default: auto
  overrides:
    validation: human-review
```

| Field | Description |
|-------|-------------|
| `agents` | Ordered list of pipeline stages. Agents run sequentially — each must complete before the next starts |
| `post_ready` | Agents that run after all main stages complete (watchers, quality checks) |
| `parallel_groups` | Concurrency constraints. Agents in the same group serialize on an idea; agents in different groups can overlap |
| `gating` | Approval gates. `default` sets the mode for all stages; `overrides` sets per-stage modes |

### Pipeline templates

Reusable pipeline configs live in `pipeline-templates/` as `.yaml` or `.prose`
files. Both formats produce the same internal dict. Create them via the
dashboard at `/pipelines/` or by adding files directly:

```yaml
# pipeline-templates/research-only.yaml
name: research-only
description: Deep research without building anything
agents: [ideation]
post_ready: [competitive-watcher, research-watcher]
gating:
  default: human-review
```

Or in Prose format:

```prose
# pipeline-templates/research-only.prose
pipeline research-only:
  description: "Deep research without building anything"

  parallel:
    session: competitive-watcher
    session: research-watcher

  session: ideation
  gate: human-review
```

Templates can be applied to new ideas at creation time or to existing ideas
via the dashboard. Each idea gets its own copy of the pipeline config, so
modifying a template doesn't affect ideas already using it. The file format
is resolved at load time — the pool and blackboard only see the canonical
pipeline dict, regardless of whether it originated from YAML or Prose.

### Per-idea customization

Every idea stores its own pipeline in `status.json`. You can:

- Assign a template when creating an idea
- Modify an idea's pipeline mid-flight from the dashboard
- Add or remove stages, watchers, and gating overrides per idea
- Change parallel groups to control agent concurrency

### Custom pipelines

To create a pipeline with custom agents:

1. Define your agents in `registry.yaml` (or via the agent wizard)
2. Create a pipeline template referencing those agents
3. Apply the template to new ideas

Example: a content pipeline with research, writing, and editing stages:

```yaml
# pipeline-templates/content.yaml
name: content
description: Research, write, and edit content
agents: [researcher, writer, editor]
post_ready: [fact-checker]
gating:
  default: auto
  overrides:
    editor: human-review
```

## Worker pool

The `PoolManager` schedules agent runs with priority-queue dispatch. The pool
continuously:

1. Scores all active ideas by priority
2. Fills `pool_size` concurrent worker slots with the highest-priority work
3. Each agent gets a timeout (configurable via `JOB_TIMEOUT_MINUTES`)
4. When a slot finishes, the next highest-priority item fills it

### Priority scoring

Ideas are scored based on:

- **Phase weight** — earlier phases get slight priority to maintain pipeline flow
- **Starvation** — ideas that haven't been serviced recently get boosted
- **Deadline pressure** — ideas approaching their configured deadline get priority
- **Iteration count** — diminishing returns after repeated runs in the same phase

The pool writes state snapshots to `pool/state.json` every 10 seconds for
dashboard visibility.

### Scheduling constraints

The pool respects pipeline constraints when dispatching work:

- **Parallel groups** — agents in the same group never run simultaneously on the same idea
- **Max concurrent** — each agent has a configurable limit on how many instances can run across all ideas
- **Serial within pipeline** — the next stage only starts after the current one completes

## Phase transitions

An idea moves through its pipeline stages sequentially. After each agent run,
the agent sets a **phase recommendation**:

- `proceed` — move to the next stage
- `iterate` — run the same stage again (up to 3 times before human review)
- `needs_review` — pause for human approval
- `kill` — abandon the idea

### Gating modes

Each stage transition can be gated independently:

- **auto** — proceed immediately on `proceed` recommendation
- **human-review** — always wait for human approval (via Telegram or web dashboard)
- **llm-decides** — the agent self-assesses whether human review is needed

Gating is configured per-stage in the pipeline's `gating.overrides` map, with
a `gating.default` fallback.

## Refinement cycles

After an idea completes all pipeline stages, it can loop back to the first
stage for refinement. Agents receive refinement context telling them to critique
and improve existing work rather than starting over. Each cycle through the
pipeline is tracked in `phase_history`. The number of refinement cycles is
configurable per idea (`max_refinement_cycles` in `status.json`).

## Watchers

Watcher agents run on a cron cadence alongside the pipeline. They monitor
all non-killed ideas continuously — even after release:

- **competitive-watcher** — monitors competitor activity (default: every 6 hours)
- **research-watcher** — tracks relevant academic/industry research (default: daily)

Watchers discover new information and submit it as structured feedback.
Pipeline agents pick up this feedback on their next run and incorporate it
into artifacts. This creates a continuous intelligence loop — ideas stay
current even after they're released.

Watchers can also be placed in `post_ready` to run once after the pipeline
completes, or given a `cadence` to run on a cron schedule indefinitely.

## Evolution

The `trellis evolve` command runs a retrospective across agent transcripts.
It identifies patterns (what worked, what failed) and updates
`agents/<name>/knowledge/learnings.md`. These learnings are injected into
future agent runs, creating a feedback loop.

## Web dashboard

The dashboard is a FastAPI application with:

- **Backend**: FastAPI + WebSocket for real-time updates
- **Frontend**: Jinja2 templates + HTMX for dynamic interactions + Tailwind CSS
- **Views**: idea list, idea detail, agent list, pool status, activity feed,
  cost tracking, evolution history, pipeline editor, settings

The dashboard can start with or without the worker pool (`--no-pool`).
