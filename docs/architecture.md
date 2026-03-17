# Architecture

## Blackboard pattern

Ideas share state through a filesystem-based blackboard at
`blackboard/ideas/<slug>/`. Each idea gets a directory with:

- `status.json` — phase, iteration count, cost tracking, phase history
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

## Worker pool

The `PoolManager` schedules agent runs in fixed-length **cycle windows**
(default: 30 minutes). Within each window:

1. All active ideas are scored by priority
2. The pool fills `pool_size` concurrent slots with the highest-priority work
3. Each agent gets a deadline (the end of the cycle window)
4. When a slot finishes, the next highest-priority item fills it
5. At window end, incomplete work is interrupted and state is saved

### Priority scoring

Ideas are scored based on:

- **Phase weight** — earlier phases get slight priority to maintain pipeline flow
- **Starvation** — ideas that haven't been serviced recently get boosted
- **Deadline pressure** — ideas approaching their configured deadline get priority
- **Iteration count** — diminishing returns after repeated runs in the same phase

The pool writes state snapshots to `pool/state.json` every 10 seconds for
dashboard visibility.

## Phase transitions

An idea moves through phases: `ideation -> implementation -> validation -> release`.

After each agent run, the agent sets a **phase recommendation**:

- `proceed` — move to the next phase
- `iterate` — run the same phase again (up to 3 times)
- `needs_review` — pause for human approval
- `kill` — abandon the idea

### Gating modes

Each phase transition can be gated:

- **auto** — proceed immediately on `proceed` recommendation
- **human** — always wait for human approval (via Telegram or web dashboard)
- **llm-decides** — the agent self-assesses whether human review is needed

Gating is configured per-idea in `status.json` under `pipeline_config`.

## Refinement cycles

After an idea reaches `release`, it can loop back to `ideation` for refinement.
Agents receive refinement context telling them to critique and improve existing
work rather than starting over. Each cycle through the pipeline is tracked in
`phase_history`.

## Evolution

The `incubator evolve` command runs a retrospective across agent transcripts.
It identifies patterns (what worked, what failed) and updates
`agents/<name>/knowledge/learnings.md`. These learnings are injected into
future agent runs, creating a feedback loop.

## Watchers

Watcher agents run on a cron cadence alongside the pipeline:

- **competitive-watcher** — monitors competitor activity (default: every 6 hours)
- **research-watcher** — tracks relevant academic/industry research (default: every 8 hours)

Watchers don't create their own artifact files. Instead, they either update
existing artifacts directly with cited research, or use `register_feedback`
when they find something important but don't know where to apply it. Each
idea's pipeline config controls which watchers are active for that idea.

## Web dashboard

The dashboard is a FastAPI application with:

- **Backend**: FastAPI + WebSocket for real-time updates
- **Frontend**: Jinja2 templates + HTMX for dynamic interactions + Tailwind CSS
- **Views**: idea list, idea detail, agent list, pool status, activity feed,
  cost tracking, evolution history, settings

The dashboard can start with or without the worker pool (`--no-pool`).
