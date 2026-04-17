# Runbook: AD-21 — Pub/Sub Replay-Id Reconciliation

**Owner:** On-call SRE
**Trigger:** `shadow.reconcile.lag_breach` alert fires (lag > 60h, headroom before the SF 72h cliff).

## Background

The Salesforce Pub/Sub API retains replay-ids for **72 hours**. If our subscriber falls more than 3 days behind, the replay state is unrecoverable and we need to reconcile via REST.

## Diagnose

```bash
# What's the lag for affected processes?
uv run offramp shadow status --process-id <process>
# Look for: lag_status=reconciliation_required, lag_hours > 60
```

## Mitigate

```bash
# Run the resyncer for one process — pulls the affected sObjects via REST,
# overwrites the shadow store, resets the replay-id checkpoint.
uv run python -m offramp.validate.reconcile.resync \
  --process <process> \
  --sobject Account
```

For full-org reconciliation:

```bash
for sobject in Account Lead Opportunity Case Contact; do
  uv run python -m offramp.validate.reconcile.resync \
    --process <process> --sobject "$sobject"
done
```

## Prevent

- Subscriber pods should auto-restart; if they're crash-looping, look at the gateway-side OAuth token expiry first (JWT cert rotation runbook).
- Run the lag monitor as an alarm: alert at 24h, page at 60h.
- For peak-load periods, scale up the shadow subscriber replicas (one per object channel) to stay caught up.
