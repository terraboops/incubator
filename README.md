# incubator

A team of Claude agents that takes an idea from research to release.

You describe an idea. Agents research the market, write a feasibility study,
build an MVP, test it, and prepare launch materials — running autonomously with
human checkpoints between phases.

![Ideas pipeline](docs/screenshots/dashboard-ideas.png)

## Install

```bash
brew tap terraboops/tap
brew install incubator
```

Or from source:

```bash
pip install .
```

## Quick start

```bash
incubator init myproject && cd myproject
incubator serve                      # dashboard + agents at localhost:8000
```

Submit your first idea:

```bash
incubator incubate "Cat cafe in Vancouver" -d "A cat cafe targeting remote workers"
```

Or use the web dashboard at `localhost:8000/ideas/new`.

## How it works

An idea flows through four phases, each handled by a specialized agent:

| Phase | Agent does |
|-------|-----------|
| **Ideation** | Competitive analysis, feasibility study, feedback synthesis |
| **Implementation** | Builds an MVP in a sandboxed workspace |
| **Validation** | Tests the implementation against the spec |
| **Release** | Deployment artifacts and launch materials |

Agents share state through a **blackboard** — a plain filesystem directory per
idea. Each agent reads what previous agents wrote and adds its own work, so
context accumulates naturally without a database.

A **worker pool** schedules agents in time-boxed cycles, rotating across ideas
by priority. Between phases, the system pauses for **human approval** via
Telegram or the dashboard before proceeding.

![Agent team](docs/screenshots/dashboard-agents.png)

## Architecture

```
 You ──► idea ──► [ ideation ] ──► [ implementation ] ──► [ validation ] ──► [ release ]
                       │                  │                     │                 │
                       ▼                  ▼                     ▼                 ▼
                  blackboard/ideas/<slug>/  ← shared filesystem state
```

- **No framework** — agents are Claude sessions with plain-text prompts and MCP tools
- **Blackboard pattern** — agents coordinate through files, not message passing
- **Worker pool** — configurable concurrency, time-boxing, and priority rotation
- **Human-in-the-loop** — Telegram notifications + approval gates between phases
- **Self-improving** — agents accumulate learnings in `knowledge/learnings.md` across runs

## Agent customization

Each agent lives in `agents/<name>/` with:

- `prompt.py` — the system prompt (a Python string constant)
- `.claude/CLAUDE.md` — project-level instructions
- `knowledge/learnings.md` — accumulated learnings (preserved across upgrades)

The prompts are plain text. No abstractions, no DSLs. Edit them directly.

## Configuration

Copy `.env.example` to `.env`:

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot for notifications + approval |
| `POOL_SIZE` | 3 | Concurrent agent slots |
| `CYCLE_TIME_MINUTES` | 30 | Worker pool cycle window |
| `MODEL_TIER_HIGH` | claude-sonnet-4-6 | Model for pipeline agents |
| `MODEL_TIER_LOW` | claude-haiku-4-5 | Model for watchers |

Agent definitions live in `registry.yaml` — models, tool access, turn limits,
and token budgets per agent.

## CLI

```
incubator init [DIR]          Scaffold a new project
incubator incubate TITLE      Submit an idea
incubator status IDEA         Show idea status
incubator list                List all ideas
incubator serve               Dashboard + worker pool
incubator serve --background  Run as daemon
incubator serve --stop        Stop daemon
incubator agent upgrade       Update agents from package defaults
```

## Project layout

```
myproject/
  .incubator            # project marker
  .env                  # config
  registry.yaml         # agent definitions
  agents/               # prompts and knowledge
    ideation/
    implementation/
    validation/
    release/
    artifact-check/     # quality checks across all ideas
    competitive-watcher/ # monitors competitive landscape
    research-watcher/   # tracks relevant research
  blackboard/ideas/     # per-idea shared state
  workspace/            # agent working dirs
```

## Development

```bash
git clone https://github.com/terrateamio/incubator.git
pip install -e ".[dev]"
pytest -v                     # 114 tests
```

## Docs

- [Agent system](docs/agents.md) — customization, creating new agents
- [Architecture](docs/architecture.md) — blackboard pattern, pool scheduler, phase transitions
- [Self-hosting](docs/self-hosting.md) — daemon mode, launchd, systemd, reverse proxy
