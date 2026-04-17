# Runbook: Salesforce API Quota Incident (AD-24)

**Owner:** On-call SRE
**Trigger:** `mcp.gateway.quota_exhausted` alert fires, or `/limits` shows < 5% daily allotment remaining.

## Background

Salesforce daily API quota is **org-wide** — shared across every integration, user, and Off-Ramp process. The MCP gateway's quota allocator (AD-24) splits remaining capacity per-process by configured weight; when a process tries to make a call that would exceed its share, `QuotaExhausted` is raised before the SF round-trip.

## Diagnose

```bash
# What does the gateway think the per-process utilization is?
curl -s http://mcp:8080/quota/utilization | jq

# What does Salesforce itself report?
uv run python -c "
import asyncio
from offramp.mcp.quota import QuotaAllocator
# ... inspect the running allocator's snapshot
"
```

Look for one of three patterns:

1. **One process is greedy.** Highest-weighted process is consuming everything.
   → reduce its weight in the gateway config; restart MCP pods.
2. **A non-Off-Ramp integration is spiking.** Salesforce-side load (a customer-side ETL, a Mulesoft job).
   → coordinate with the SF admin; pause the spike; raise quota for the day if possible.
3. **Genuinely over capacity.** The customer's Off-Ramp footprint exceeds their org's tier.
   → request an enterprise quota increase from Salesforce; in the interim, pause non-critical processes.

## Mitigation

```bash
# Pause a non-critical shadow process to free up its allocation.
kubectl scale deployment/<release>-shadow --replicas=0 -n offramp-<customer>

# Resume after quota recovers.
kubectl scale deployment/<release>-shadow --replicas=1 -n offramp-<customer>
```

For acute events, the rollback runbook also frees up writes to Salesforce (since rolled-back processes route to SF directly).

## Long-term

- Right-size the per-process weight tables (gateway ConfigMap).
- Schedule heavy bulk operations off-peak.
- Negotiate quota increase if the platform footprint is steady-state pressure.
