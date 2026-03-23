# Agent system

## How agents work

Every agent is a Claude session managed by `BaseAgent` (`trellis/core/agent.py`).
When an agent runs, it:

1. Receives a system prompt from `prompt.py` plus contextual injections (deadline
   awareness, refinement mode, knowledge history)
2. Gets access to MCP tool servers: blackboard tools, telegram tools, and
   evolution tools
3. Runs inside a `ClaudeSDKClient` session with configured model, turn limit,
   and budget cap
4. Writes its outputs to the blackboard and sets a phase recommendation

Agents are defined in `registry.yaml` and their files live under `agents/<name>/`.

## Agent directory layout

```
agents/ideation/
  prompt.py             # SYSTEM_PROMPT string constant
  .claude/
    CLAUDE.md           # project-level Claude instructions
  knowledge/
    learnings.md        # accumulated learnings (auto-populated by evolution)
```

## System prompts

The system prompt is the primary way to control agent behavior. Each agent's
`prompt.py` exports a `SYSTEM_PROMPT` string that defines the agent's role,
instructions, and output format.

On top of the agent-specific prompt, the framework injects:

- **Global system prompt** (`global-system-prompt.md`) — shared instructions
  for all agents
- **Deadline context** — remaining time when running in a pool window
- **Refinement context** — instructions for iterative improvement after first release
- **LLM-decides gating** — self-assessment instructions when the agent controls
  phase transitions
- **Knowledge context** — global learnings plus per-idea notes from previous runs

## CLAUDE.md

The `.claude/CLAUDE.md` file provides project-level instructions that the Claude
session loads automatically. Use this for:

- Tool usage patterns specific to this agent
- File path conventions
- Quality standards

## Knowledge and learnings

Each agent type has a `knowledge/learnings.md` file that accumulates insights
across runs. The evolution system (`trellis evolve`) analyzes agent transcripts
and updates these files.

Per-idea knowledge is stored on the blackboard at
`blackboard/ideas/<slug>/agent-knowledge/<agent>/`. Agents can write notes here
using the `write_knowledge` MCP tool, and these notes are included in the
system prompt on subsequent runs.

## Creating new agents

### Via the filesystem

1. Create `agents/<name>/prompt.py` with a `SYSTEM_PROMPT` string
2. Create `agents/<name>/.claude/CLAUDE.md` with session instructions
3. Create `agents/<name>/knowledge/` directory
4. Add an entry to `registry.yaml`:

```yaml
- name: my-agent
  description: What this agent does
  model: claude-sonnet-4-6
  max_turns: 30
  max_budget_usd: 0
  status: active
  tools: [Read, Write, Bash, Glob, Grep]
  phase: ideation          # which pipeline phase, or "*" for all
  permission_mode: bypassPermissions
```

### Via the web UI

The dashboard at `/agents` lets you create agents through a form. This creates
the directory structure and registry entry for you.

## The artifact-check agent

`artifact-check` is a maintenance agent with `phase: "*"`, meaning it runs
across all ideas rather than within a single pipeline phase. It:

- Reviews artifacts for mechanical quality issues (accessibility, clarity, structure)
- Uses `register_feedback` to report specific, actionable findings
- Checks existing feedback to avoid duplicates
- Never creates its own artifact files

This is a useful pattern for any cross-cutting concern: quality gates, cost
monitoring, compliance checks, etc.

## Registry fields

| Field | Type | Description |
|---|---|---|
| `name` | string | Agent identifier, matches directory name |
| `description` | string | Human-readable description |
| `model` | string | Claude model to use |
| `max_turns` | int | Maximum conversation turns |
| `max_budget_usd` | float | Budget cap (0 = unlimited) |
| `status` | string | `active` or `inactive` |
| `tools` | list | Allowed Claude tools |
| `phase` | string | Pipeline phase or `*` for global |
| `permission_mode` | string | `bypassPermissions` for autonomous operation |
| `thinking` | object | Thinking configuration (e.g., `type: adaptive`) |
| `setting_sources` | list | Settings sources (e.g., `[project]`) |
| `claude_home` | string | Path to `.claude/` directory |
| `cadence` | string | Cron expression for watcher agents |
| `env` | object | Extra environment variables |

### Sandbox fields

See [security.md](security.md) for the full security model.

| Field | Type | Default | Description |
|---|---|---|---|
| `sandbox_enabled` | bool | `false` | Enable nono kernel-level sandbox |
| `sandbox_ssh` | bool | `false` | Pass `SSH_AUTH_SOCK` through (for git operations) |
| `sandbox_rollback` | bool | `false` | Enable content-addressable snapshots (`--rollback`) |
| `sandbox_profile` | string | `claude-code` | Base nono profile to inherit from |
| `sandbox_proxy_credentials` | list | `["anthropic"]` | Credential names to proxy (agent never sees raw tokens) |
| `sandbox_allowed_hosts` | list | `[]` | Allowed outbound hosts via nono proxy |
| `sandbox_allowed_ports` | list | `[]` | Allowed local port bindings (for dev servers) |
| `sandbox_allowed_commands` | list | `[]` | Override destructive command blocks |
| `sandbox_extra_read_paths` | list | `[]` | Additional paths the agent may read |
| `sandbox_extra_write_paths` | list | `[]` | Additional paths the agent may write |
| `sandbox_credential_maps` | list | `[]` | 1Password/Apple Passwords URIs mapped to env vars |
| `sandbox_verify_attestations` | bool | `false` | Require Sigstore-signed instruction files |
