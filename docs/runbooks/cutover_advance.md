# Runbook: Cutover Stage Advance

**Owner:** Engagement lead + on-call SRE
**Schedule:** the cutover orchestrator CronJob runs automatically every 15 min in prod (per the helm chart). This runbook covers the manual operator workflow for one-shot evaluations.

## Pre-flight

```bash
# 1. Confirm current routing config + readiness
uv run offramp cutover status --process-id <process>

# Expected output JSON includes:
#   stage_percent, entered_stage_at, dwell_remaining_s,
#   readiness_score, cutover_eligible, reason
```

If `readiness_score < 98` → DO NOT advance. Investigate divergence categorization in the shadow dashboard:

```bash
uv run offramp shadow report --process-id <process> --out /tmp/<process>-report
open /tmp/<process>-report/shadow_dashboard.html
```

## Advance one stage

```bash
# Dry-run first — shows the decision without applying it.
uv run offramp cutover advance --process-id <process> --dry-run

# If the decision shape is "advance" with a sensible reason, apply.
uv run offramp cutover advance --process-id <process>
```

The orchestrator only advances when **all** of:
- readiness ≥ 98
- current dwell time complete (48h at 1%, 24h at 5%, 12h at 25%, 6h at 50%)
- `cutover_eligible: true` from the readiness scorer

## Stages

| Stage | Dwell | Notes |
|---|---|---|
| 0%   | n/a  | Pre-cutover; routing returns "salesforce" for every record. |
| 1%   | 48h  | First runtime traffic. Watch the divergence dashboard hourly for the first 4h. |
| 5%   | 24h  | First multi-record validation. |
| 25%  | 12h  | Most representative slice. |
| 50%  | 6h   | Final pre-100 sanity check. |
| 100% | 0    | Cutover complete; post-cutover monitoring takes over. |

## What auto-rollback looks like

If readiness drops between two `advance` evaluations:

- score < 95 → orchestrator rolls back ONE stage automatically; alert fires.
- score < 90 → orchestrator marks decision as `immediate_rollback` requiring `--confirm`; alert pages on-call.

## See also

- [docs/runbooks/cutover_rollback.md](cutover_rollback.md)
- [docs/runbooks/quota_incident.md](quota_incident.md)
- [docs/architecture.md §11](../architecture.md)
