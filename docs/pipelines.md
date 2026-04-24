# Pipelines

Pipelines define how agents collaborate on an idea. Each pipeline specifies
which agents run, in what order, with what concurrency constraints, and what
approval gates.

## Default pipeline

Trellis ships with a default pipeline that takes ideas through four stages:

```
ideation → implementation → validation → release
```

After release, `competitive-watcher` and `research-watcher` run as post-ready
agents, then continue monitoring on a cron cadence.

This is just one configuration. You can create pipelines with any agents in
any order.

## Pipeline formats

Pipeline templates can be written in **YAML** or **Prose**. Both formats
produce the same internal pipeline config and are fully interchangeable.

### YAML format

```yaml
name: my-pipeline
description: What this pipeline does
agents: [stage-1, stage-2, stage-3]
post_ready: [watcher-1]
parallel_groups:
  - [stage-1, stage-2, stage-3]
  - [watcher-1]
gating:
  default: auto
  overrides:
    stage-2: human-review
```

### Prose format

Prose is a declarative orchestration language. The same pipeline as above:

```prose
pipeline my-pipeline:
  description: "What this pipeline does"

  parallel:
    session: watcher-1

  session: stage-1
  gate: auto

  session: stage-2
  gate: human-review

  session: stage-3
  gate: auto
```

Prose maps to the same fields: `pipeline NAME:` sets the name, `session:`
entries become agents (in order), `parallel:` blocks become `post_ready` and
`parallel_groups`, and `gate:` entries become `gating.overrides`.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `agents` | yes | Ordered list of pipeline stages. Run sequentially per idea |
| `post_ready` | no | Agents that run after all stages complete |
| `parallel_groups` | no | Concurrency groups. Agents in the same group serialize on an idea |
| `gating.default` | no | Default approval mode: `auto`, `human-review`, or `llm-decides` |
| `gating.overrides` | no | Per-stage approval mode overrides |

### Gating modes

| Mode | Behavior |
|------|----------|
| `auto` | Proceed immediately when the agent recommends `proceed`. Gate to human review after 3 `iterate` cycles |
| `human-review` | Always pause for human approval before advancing |
| `llm-decides` | The agent self-assesses whether human review is needed |

## Creating pipeline templates

### Via the dashboard

Go to `/pipelines/` and click "New Template". The visual composer lets you:

- Add agents as pipeline stages or post-ready watchers
- Reorder stages by dragging
- Set per-stage gating modes
- Configure parallel groups

### Via the filesystem

Add a `.yaml` or `.prose` file to `pipeline-templates/`:

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

Or in Prose:

```prose
# pipeline-templates/content.prose
pipeline content:
  description: "Research, write, and edit content"

  parallel:
    session: fact-checker

  session: researcher
  gate: auto

  session: writer
  gate: auto

  session: editor
  gate: human-review
```

Templates in either format are loaded automatically by the dashboard.

## Applying pipelines to ideas

- **At creation** — select a pipeline template when submitting a new idea
- **Mid-flight** — change an idea's pipeline from the dashboard at any time
- **Via API** — POST to `/ideas/{idea_id}/pipeline` with the config

Each idea stores its own copy of the pipeline config. Changing a template
doesn't affect ideas already using it.

## Parallel groups

Parallel groups control which agents can run simultaneously on the same idea.
Agents in the same group are serialized — only one runs at a time. Agents in
different groups can overlap.

```yaml
parallel_groups:
  - [ideation, implementation, validation, release]  # group 1: serial
  - [competitive-watcher, research-watcher]           # group 2: serial
  # groups 1 and 2 can overlap — a watcher can run while implementation runs
```

If `parallel_groups` is not specified, all agents default to a single group
(fully serial).

## Post-ready agents

Post-ready agents run after all main pipeline stages complete. They're
typically watchers that monitor external sources:

```yaml
post_ready: [competitive-watcher, research-watcher]
```

Once all post-ready agents have been serviced, the idea transitions to
`released` (or loops back for refinement if `max_refinement_cycles` allows).

Post-ready agents with a `cadence` in the registry continue running
indefinitely on all non-killed ideas — even after release.

## Converting YAML templates to Prose

To convert existing YAML pipeline templates to Prose:

```bash
trellis pipelines-to-prose           # convert all, back up originals as .yaml.bak
trellis pipelines-to-prose --dry-run # preview without writing
```

Both formats coexist — you don't need to convert everything at once.

## Examples

Each example is shown in both formats.

### Research-only pipeline

Deep research without building anything:

```yaml
name: research-only
agents: [ideation]
post_ready: [competitive-watcher, research-watcher]
gating:
  default: human-review
```

```prose
pipeline research-only:
  description: "Deep research without building anything"

  parallel:
    session: competitive-watcher
    session: research-watcher

  session: ideation
  gate: human-review
```

### Fast prototype

Skip validation, auto-approve everything:

```yaml
name: fast-prototype
agents: [ideation, implementation]
gating:
  default: auto
```

```prose
pipeline fast-prototype:
  description: "Skip validation, auto-approve everything"

  session: ideation
  gate: auto

  session: implementation
  gate: auto
```

### Reviewed pipeline

Human approval at every stage:

```yaml
name: reviewed
agents: [ideation, implementation, validation, release]
gating:
  default: human-review
```

```prose
pipeline reviewed:
  description: "Human approval at every stage"

  session: ideation
  gate: human-review

  session: implementation
  gate: human-review

  session: validation
  gate: human-review

  session: release
  gate: human-review
```

### Content pipeline

Custom agents for content creation:

```yaml
name: content
agents: [researcher, writer, editor, publisher]
post_ready: [seo-checker]
parallel_groups:
  - [researcher, writer, editor, publisher]
  - [seo-checker]
gating:
  default: auto
  overrides:
    publisher: human-review
```

```prose
pipeline content:
  description: "Custom agents for content creation"

  parallel:
    session: seo-checker

  session: researcher
  gate: auto

  session: writer
  gate: auto

  session: editor
  gate: auto

  session: publisher
  gate: human-review
```
