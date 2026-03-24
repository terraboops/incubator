# Troubleshooting

## Agent run issues

### Agent fails immediately

Check the pool log for the error:

```bash
grep -A 20 "failed on" pool/trellis.log | tail -25
```

Common causes:
- **Missing API key** — set `ANTHROPIC_API_KEY` in `.env` or configure Claude Code auth
- **Sandbox permission denied** — if `sandbox_enabled: true`, the agent may lack access to a required path. Check the error for `EPERM` or `Operation not permitted`. Add the path to `sandbox_extra_read_paths` or `sandbox_extra_write_paths` in `registry.yaml`
- **nono not installed** — sandbox requires the nono CLI: `brew install always-further/nono/nono`
- **Model not available** — verify the model name in `registry.yaml` matches your API access

### Agent runs but produces no output

Check the agent transcript:

```bash
ls -t blackboard/ideas/<slug>/agent-logs/
cat blackboard/ideas/<slug>/agent-logs/<newest-file>.json | python3 -m json.tool | head -50
```

If the transcript is empty, the agent crashed during initialization. Check
`pool/trellis.log` for the full traceback.

### Agent keeps iterating without progressing

The iteration cap is 3 by default. After 3 `iterate` recommendations, the idea
is gated for human review. Check the dashboard for ideas showing "Awaiting
human review" and either dismiss the review or kill the idea.

If you want the agent to iterate more freely, change the gating mode to `auto`
in the idea's pipeline config.

### Agent can't access files

If sandbox is enabled, agents can only read/write paths explicitly granted in
their profile. Default permissions:

| Path | Access |
|------|--------|
| Project root | Read |
| `blackboard/ideas/<idea>/` | Read + Write |
| `workspace/<idea>/` | Read + Write |
| `/tmp` | Read + Write (via claude-code profile) |
| `~/.claude` | Read + Write (via claude-code profile) |

Add additional paths via `sandbox_extra_read_paths` or `sandbox_extra_write_paths`
in `registry.yaml`.

### Worker pool not scheduling work

```bash
cat pool/state.json | python3 -m json.tool
```

Check:
- **Queue depth** — if 0, no work is available (all ideas killed/released/review-gated)
- **All workers active** — pool may be at capacity; increase `POOL_SIZE` in `.env`
- **Idea in review** — check dashboard for "Awaiting human review" badges

### Idea stuck in a phase

```bash
trellis status <idea-slug>
```

Common causes:
- **Phase is `*_review`** — waiting for human approval. Dismiss from dashboard
- **`needs_human_review: true`** — iteration cap hit. Dismiss review or kill idea
- **Agent erroring repeatedly** — check `pool/trellis.log` for recurring errors
- **No matching agent** — the pipeline references an agent not in `registry.yaml`

## Diagnostic commands

```bash
# Pool status
trellis list                              # all ideas and phases
cat pool/state.json | python3 -m json.tool  # pool workers and queue

# Agent logs
ls -t blackboard/ideas/<slug>/agent-logs/  # list runs newest first

# Server logs
tail -50 pool/trellis.log                 # recent pool activity
grep ERROR pool/trellis.log               # all errors
grep "failed on" pool/trellis.log         # agent failures

# Sandbox debugging
nono run --dry-run --profile claude-code --read /your/path -- echo test
```

## Debug with Claude Code

Copy one of the prompts below and paste it into Claude Code from your Trellis
project directory. Claude will inspect the relevant files and diagnose the issue.

### General agent diagnostics

```
I'm running Trellis and having issues with agent runs. Help me diagnose:

1. Read pool/state.json — how many workers are active? What's the queue depth?
2. Search pool/trellis.log for recent ERROR lines and "failed on" messages.
   Show the last 5 errors with tracebacks.
3. Run `trellis list` — flag any ideas that look stuck (high iteration count,
   needs_human_review, or same phase for a long time).
4. For failing agents, read the newest transcript in
   blackboard/ideas/<slug>/agent-logs/ — is it empty? Does it show errors?
5. Read registry.yaml — are all agents active? Are models valid?
6. Summarize what's wrong and suggest fixes.
```

### Stuck idea diagnosis

```
The idea "<IDEA_SLUG>" is stuck. Help me figure out why:

1. Read blackboard/ideas/<IDEA_SLUG>/status.json — what phase is it in?
   What do stage_results show? Is needs_human_review true?
2. Check the pipeline config in status.json — what agents are expected?
   Does every agent exist in registry.yaml?
3. Read the most recent agent transcript in
   blackboard/ideas/<IDEA_SLUG>/agent-logs/ — what did the agent do?
   Did it set a phase_recommendation?
4. Search pool/trellis.log for "<IDEA_SLUG>" — any errors or warnings?
5. Tell me what's blocking this idea and how to unblock it.
```

### Sandbox permission issues

```
Agent "<AGENT_NAME>" is failing with permission errors under nono sandbox.
Help me fix it:

1. Search pool/trellis.log for "EPERM" or "Operation not permitted" near
   "<AGENT_NAME>" entries.
2. Read the agent's config in registry.yaml — what sandbox settings does it have?
3. Check what paths the agent is trying to access from the error messages.
4. Suggest which paths to add to sandbox_extra_read_paths or
   sandbox_extra_write_paths to fix the issue.
```

### Cost investigation

```
I want to understand where my Trellis budget is going:

1. Run `trellis list` and show me cost per idea.
2. For the most expensive idea, list all agent transcripts in
   blackboard/ideas/<slug>/agent-logs/ sorted by date.
3. Check which agents cost the most by reading the cost_usd field
   in each transcript.
4. Look at iteration_count in status.json — is an agent iterating
   excessively?
5. Suggest how to reduce costs (lower max_turns, switch to haiku
   for specific agents, tighten iteration caps).
```

## Filing an issue

When opening a [GitHub issue](https://github.com/terraboops/trellis/issues), include:

1. **Trellis version**: `trellis --version`
2. **What you expected** vs what happened
3. **Relevant logs**: `pool/trellis.log` excerpt (redact any API keys or personal data)
4. **Agent transcript**: the relevant file from `blackboard/ideas/<slug>/agent-logs/`
5. **Registry config**: the agent's entry from `registry.yaml`
6. **Pipeline config**: the idea's pipeline (from dashboard or `status.json`)
