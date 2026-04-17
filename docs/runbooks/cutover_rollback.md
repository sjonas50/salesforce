# Runbook: Cutover Rollback

**Owner:** On-call SRE
**When:** divergence-rate spike, customer-reported regression, or instant_rollback alert fires.

Two flavors:

## A. One-stage rollback (orchestrator-driven)

Triggered automatically when readiness < 95. Manual equivalent:

```bash
# 1. Inspect the decision the orchestrator would make.
uv run offramp cutover advance --process-id <process> --dry-run

# 2. Apply (no confirm needed for one-stage rollback).
uv run offramp cutover advance --process-id <process>
```

Output includes the engram + F44 anchor IDs for the transition. Save these for the incident ticket.

## B. Instant rollback to 0% (human-confirmed)

Triggered manually when something is bad enough that the staged regression isn't fast enough.

```bash
uv run offramp cutover rollback --process-id <process> --confirm
```

Effect:
- routing config → 0%, applied on next gateway request
- engram + F44 anchored stage transition
- saga compensation runs in reverse (deletes records the runtime created, sends correction emails for offsetable actions; PAUSES on REQUIRES_HUMAN actions)

## Verifying rollback took effect

```bash
uv run offramp cutover status --process-id <process>
# Expect: stage_percent: 0
```

Then in the gateway logs, every subsequent routing decision for the process should show `routed_to=salesforce`.

## Saga pauses

If saga compensation pauses for a `REQUIRES_HUMAN` activity (e.g., an LLM call already cost real money), the orchestrator returns `paused_for_human: true`. The on-call must:

1. Read the saga record's last activity in the engram trace.
2. Decide whether to manually compensate or accept the cost.
3. Call `offramp cutover advance --process-id <process> --confirm` to resume.

## Post-rollback

After a confirmed-clean rollback:

1. Open an incident ticket linking the engram anchors.
2. Identify root cause via shadow divergence dashboard + Compare Mode replay.
3. Fix; re-extract; re-generate; new shadow run; only then re-`begin`.
