# incubator

An autonomous idea incubation pipeline powered by Claude.

Submit an idea, and a team of AI agents researches it, builds an MVP, validates
it, and prepares it for release — with human oversight at every stage.

## Quick start

```bash
pip install .                        # or: pip install -e ".[dev]"
incubator init myproject
cd myproject
incubator serve                      # web dashboard + worker pool at localhost:8000
```

## How it works

An idea flows through four pipeline phases:

1. **Ideation** — competitive analysis, feasibility study, feedback synthesis
2. **Implementation** — builds an MVP in a sandboxed workspace
3. **Validation** — tests the implementation against the spec
4. **Release** — prepares deployment artifacts and launch materials

Each phase is handled by a specialized Claude agent. Agents read and write to a
shared **blackboard** (a filesystem directory per idea) so their work accumulates
across phases.

A **worker pool** schedules agents in time-boxed cycles, rotating across ideas
by priority. Between phases, the orchestrator can pause for **human approval**
(via Telegram or the web dashboard) before proceeding.

After release, ideas can loop back for **refinement** — agents re-examine their
previous work with fresh eyes and improve it.

## Submitting ideas

```bash
incubator incubate "Cat cafe in Vancouver" -d "A cat cafe targeting remote workers"
```

Or use the web dashboard at `http://localhost:8000/ideas/new`.

## Agent customization

Each agent lives in `agents/<name>/` with:

- `prompt.py` — the system prompt (a Python string constant `SYSTEM_PROMPT`)
- `.claude/CLAUDE.md` — project-level instructions for the Claude session
- `knowledge/learnings.md` — accumulated learnings (preserved across upgrades)

Edit these files to change how agents behave. The prompts are plain text with
no framework abstractions.

### Adding new agents

Create a new directory under `agents/` with `prompt.py` and register it in
`registry.yaml`. See `docs/agents.md` for details.

### Upgrading agent configs

When you update the incubator package, run:

```bash
incubator agent upgrade --dry-run    # preview changes
incubator agent upgrade --all        # apply all updates
```

This updates `prompt.py` and `CLAUDE.md` from the package defaults while
preserving your `knowledge/learnings.md` and `.claude/` session data.

## Configuration

### Environment variables (.env)

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token for notifications |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for notifications |
| `POOL_SIZE` | 3 | Concurrent agent slots |
| `CYCLE_TIME_MINUTES` | 30 | Worker pool cycle window |
| `WEB_HOST` | 0.0.0.0 | Dashboard bind address |
| `WEB_PORT` | 8000 | Dashboard port |
| `MODEL_TIER_HIGH` | claude-sonnet-4-6 | Model for main agents |
| `MODEL_TIER_LOW` | claude-haiku-4-5 | Model for watchers |

Copy `agents/.env.example` to `.env` and fill in your values.

### registry.yaml

Defines agents, their models, tool access, turn limits, and budgets. Each agent
entry maps to a directory under `agents/`. See the default `registry.yaml` for
the full schema.

## CLI reference

| Command | Description |
|---|---|
| `incubator init [DIR]` | Scaffold a new project |
| `incubator incubate TITLE` | Submit an idea to the pipeline |
| `incubator status IDEA` | Show idea status |
| `incubator list` | List all ideas |
| `incubator resume IDEA` | Resume a paused idea |
| `incubator kill IDEA` | Kill an idea |
| `incubator run` | Start the worker pool (no web UI) |
| `incubator serve` | Start web dashboard + worker pool |
| `incubator serve --background` | Run as a background daemon |
| `incubator serve --stop` | Stop the background daemon |
| `incubator watch` | Start background watchers |
| `incubator evolve` | Run evolution retrospective |
| `incubator agent upgrade` | Update agents from package defaults |

## Project structure

```
myproject/
  .incubator            # project marker
  .env                  # configuration
  registry.yaml         # agent definitions
  global-system-prompt.md
  agents/               # agent prompts and knowledge
    ideation/
    implementation/
    validation/
    release/
    artifact-check/     # cross-pipeline maintenance agent
  blackboard/ideas/     # per-idea shared state
    _template/          # template for new ideas
  workspace/            # agent working directories
  pool/                 # pool state and logs
```

## Development

```bash
git clone <repo>
pip install -e ".[dev]"
pytest -v
```

## Further reading

- [Agent system](docs/agents.md) — how agents work, customization, creating new agents
- [Architecture](docs/architecture.md) — blackboard pattern, pool scheduler, phase transitions
- [Self-hosting](docs/self-hosting.md) — daemon mode, launchd, systemd, reverse proxy
