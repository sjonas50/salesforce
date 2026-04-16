# Architecture: Salesforce Off-Ramp

**Version:** 0.1 (engineering spec derived from build-plan v2.1 + research)
**Date:** 2026-04-16
**Scope:** code-level architecture for the Off-Ramp platform (X-Ray, Agent Factory, Shadow Mode). The strategic build plan is [Salesforce-OffRamp-Build-Plan-v2.1.docx](../Salesforce-OffRamp-Build-Plan-v2.1.docx); the technology evaluation is [research.md](research.md). This document is the engineering contract between phases.

## 1. System Overview

```
                    ┌──────────────────────────────────────────────────────┐
                    │                Customer Salesforce Org                │
                    │  (v66.0 Spring '26, Enterprise+ edition)              │
                    └──┬────────────────┬──────────────────┬────────────────┘
                       │ Metadata API   │ REST/Bulk 2.0    │ Pub/Sub gRPC
                       │ + sf CLI       │ + Tooling API    │ (CDC + PE)
                       ▼                ▼                  ▼
        ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
        │  Extract Engine  │  │   MCP Gateway    │  │ Shadow Subscriber│
        │  (Phase 1)       │  │   (always-on)    │  │  (Phase 4)       │
        │  src/extract     │  │   src/mcp        │  │  src/validate    │
        └──┬───────────────┘  └─────┬────────────┘  └────────┬─────────┘
           │ canonical Component    │ all reads/writes        │ CDC stream
           │ + content hash         │ go through here          │ + reconcile
           ▼                        ▼                          ▼
        ┌─────────────────────────────────────────────────────────┐
        │              Kafka / Redis Streams (event bus)           │
        └────┬─────────────────┬─────────────────┬─────────────────┘
             ▼                 ▼                 ▼
        ┌─────────┐      ┌──────────┐      ┌──────────┐
        │ FalkorDB│      │ Postgres │      │  Engram  │──► F44 (Base L2)
        │ (graph) │      │(state +  │      │(prov.    │     Merkle anchor
        │         │      │ shadow)  │      │ records) │     for sensitive
        └────┬────┘      └────┬─────┘      └────┬─────┘     decisions only
             │                │                  │
             ▼                ▼                  │
        ┌──────────────────────────────┐         │
        │   Understand Engine          │         │
        │   (Phase 2: annotate, cluster)│         │
        │   src/understand             │         │
        └──────────┬───────────────────┘         │
                   ▼                              │
        ┌──────────────────────────────┐         │
        │   Generate Engine            │         │
        │   (Phase 3: emit code)       │◄────────┘
        │   src/generate               │
        └──────────┬───────────────────┘
                   ▼
        ┌─────────────────────────────────────────────────────────┐
        │  Generated Runtime Artifact (per migrated process)       │
        │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
        │  │ Tier 1: Rules│  │ Tier 2: Temp │  │ Tier 3: Lang │   │
        │  │ Python       │  │ workflow     │  │ Graph state  │   │
        │  │ (OoE-aware)  │  │ + activities │  │ machine      │   │
        │  └──────────────┘  └──────────────┘  └──────────────┘   │
        └────────────────────────┬────────────────────────────────┘
                                 │ MCP-only
                                 ▼
                    ┌─────────────────────────┐
                    │  Cutover Orchestrator   │  Phase 5
                    │  src/cutover            │  (1/5/25/50/100% routing)
                    └─────────────────────────┘
```

## 2. Component Breakdown

| # | Component | Purpose | Tech | Inputs | Outputs |
|---|-----------|---------|------|--------|---------|
| C1 | `src/extract` | Pull canonical metadata for all 21 SF automation categories + reconcile across Salto / sf CLI / Tooling API | Python 3.12, simple-salesforce 1.12.9, Salto CLI shell-out, sfdx-hardis | SF org credentials, OAuth Connected App | Component records (Pydantic) + content hash + Engram anchor |
| C2 | `src/extract/dispatch` | Resolve dynamic Apex dispatch via Custom Metadata Type records | Python, Tooling API | ApexClass corpus + CMT records | Synthetic dependency edges with confidence scores |
| C3 | `src/extract/lwc` | Parse LWC bundles, classify business-logic density, link JS→Apex | tree-sitter-javascript via py-tree-sitter | LightningComponentBundle metadata | LWC Component nodes + Apex linkage edges |
| C4 | `src/extract/ooe_audit` | Count which of the 21 OoE steps the customer org actually exercises | Python | extracted corpus + optional EventLogFile | OoE Surface Audit report (JSON + HTML) |
| C5 | `src/understand` | Load FalkorDB graph, run Leiden clustering, LLM-annotate components | FalkorDB (Cypher), networkx Leiden, Llama 3.1 70B via OpenAI-compatible endpoint | Component records | Annotated graph + BusinessProcess clusters + complexity scores |
| C6 | `src/understand/orphan` | 6-channel orphan resolution | Python | Apex corpus + LWC + Connected Apps + Named Credentials + CronTriggers + EventLogFile | Resolved entry-point classifications |
| C7 | `src/generate` | Translation-matrix-driven code emission per tier | Python (rules), Temporal SDK, LangGraph | Annotated graph nodes | Deployable Python package per process |
| C8 | `src/generate/formula` | Deterministic SF formula → Python translator (NO LLM fallback) | Custom recursive-descent parser | Formula AST | Python function + 50–200 test cases |
| C9 | `src/generate/adapters` | Managed Package Adapter Generator | Python templating + curated library | Detected MP dependencies | MCP tool definitions |
| C10 | `src/runtime/ooe` | Python state machine implementing 21-step OoE with re-fire + cascade tracking | Python, asyncio | Save transactions from MCP gateway | Transaction trace + commit/abort |
| C11 | `src/runtime/rules` | Embedded rules engine library | Python (no service) | Rule definitions + record context | RuleResult |
| C12 | `src/mcp` | Single SF interface — auth, API budget, Engram anchoring, request logging | FastAPI, MCP server SDK, Pub/Sub gRPC client | MCP tool calls from runtimes | SF API responses + provenance records |
| C13 | `src/validate/shadow` | CDC-fed shadow execution + divergence detection | Python, Pub/Sub gRPC, Postgres shadow store | Live CDC events + production read-through | Divergence records + readiness scores |
| C14 | `src/validate/compare_mode` | Replay SF debug logs through OoE runtime (week 16+) | Python | SF debug log export + reconstructed record state | Step-by-step trace comparison + categorized findings |
| C15 | `src/validate/reconcile` | **[Research gap #1]** Re-sync via REST when CDC subscriber lags >60h or gap event arrives | Python, simple-salesforce | Subscriber lag metric + gap event notifications | Full record re-fetch + replay-id reset |
| C16 | `src/cutover` | Hash-deterministic traffic shifter, saga compensation, instant rollback | Python | per-process routing config | Routing decisions (Engram + F44 anchored) |
| C17 | `src/engram` | Provenance client (every read/write/decision anchored) | Python wrapping Rust core | Decision payloads | Engram record IDs |
| C18 | `src/event_bus` | Pluggable abstraction (Redis Streams dev, Azure Event Hubs prod, NATS on-prem) | Python | Cross-component messages | Delivered events |

## 3. Data Flow Sequence

**Extract (one-time per org):**
1. Operator runs `offramp extract --org <alias>` against a customer SF org.
2. C1 invokes Salto + sf CLI + Tooling API in parallel; reconciler merges with documented precedence rules.
3. C2 reads CMTs → resolves dispatch edges. C3 parses LWC bundles. C4 audits OoE step exercise per component.
4. Each Component is hashed, written to Postgres, and anchored in Engram (C17).
5. Coverage report emitted to `out/<org>/extract/coverage.html`.

**Understand (per X-Ray engagement):**
1. Component records loaded into FalkorDB as typed nodes.
2. C5 runs Leiden clustering → BusinessProcess nodes; LLM annotation pass produces summaries + complexity scores. Every annotation Engram-anchored with prompt+model+output.
3. C6 resolves orphans across 6 channels.
4. X-Ray report rendered (interactive HTML + PDF + JSON export).

**Generate (per migrated process):**
1. Operator selects a process from the X-Ray report.
2. C7 walks the cluster's components; per-component the Translation Matrix dispatches to Tier 1 (rules), Tier 2 (Temporal), or Tier 3 (LangGraph) emitter.
3. C8 deterministically translates formulas; C9 generates MP adapters; dual-target generation produces both a rule and a Temporal wrapper for boundary components.
4. Output: a versioned Python package with manifest + deployment helm chart.

**Validate (continuous, per process):**
1. C13 subscribes to `/data/<Object>ChangeEvents` via Pub/Sub gRPC.
2. For each CDC event, the shadow executor reconstructs the triggering transaction and runs the generated artifact via the OoE runtime (C10) against shadow Postgres.
3. Field-level diff vs. next CDC event from production → divergence record (C15 catches gap events and triggers reconciliation).
4. Readiness score updated; ≥98 for 14 consecutive days = cutover-eligible.
5. **Compare Mode** (C14) operates from week 16 against customer-supplied debug log exports — same divergence pipeline, lower fidelity, earlier signal.

**Cutover (per process):**
1. Operator runs `offramp cutover advance --process <id>`.
2. C16 updates MCP gateway routing config to next stage (1/5/25/50/100%).
3. Per-record hash determines routing. Routing decision Engram-anchored; stage transitions F44-anchored.
4. If readiness drops below 95 during dwell → automatic rollback to previous stage.
5. Saga compensation activities execute on rollback.

## 4. External Dependencies

| Dependency | Auth | Notes / Risk |
|---|---|---|
| Salesforce REST/Bulk/Tooling API v66.0 | OAuth2 JWT Bearer (Connected App) | Pin API version. Org-wide quota shared. |
| Salesforce Pub/Sub API (gRPC) | OAuth2 JWT Bearer | 72h replay-id retention. Gap events possible. |
| Salesforce Metadata API v66.0 | Same OAuth | 400MB ZIP cap; Flow wildcard unreliable. |
| FalkorDB | Service-to-service token | Cypher compatibility; default port 6379. |
| Temporal | mTLS to Temporal Cloud / self-hosted cluster | Python SDK 1.16.0+. |
| Postgres 16 | Connection string in Key Vault | One DB for app state, one for shadow writes. |
| Kafka (MSK) or Redis Streams (dev) | mTLS / token | CDC retention ≥ 96h to outlast SF 72h cliff. |
| Llama 3.1 70B (or vendor LLM) | Customer-provided endpoint | Self-hosted preferred; OpenAI-compatible API. |
| Engram | Internal Rust + Python SDK | Postgres-backed; F44 anchors on Base L2. |
| AWS Secrets Manager / Azure Key Vault | IAM / managed identity | JWT private keys, OAuth tokens, DB creds. |

## 5. Environment Variables

```bash
# Salesforce per-org
SF_ORG_ALIAS=fisher_sandbox
SF_LOGIN_URL=https://login.salesforce.com
SF_CLIENT_ID=...                  # Connected App consumer key
SF_USERNAME=integration@fisher.com
SF_JWT_KEY_PATH=/secrets/sf_jwt.pem
SF_API_VERSION=66.0               # pinned

# Pub/Sub API
SF_PUBSUB_HOST=api.pubsub.salesforce.com:7443
SF_CDC_REPLAY_RECONCILE_THRESHOLD_HOURS=60

# Infra
POSTGRES_DSN=postgresql://...
POSTGRES_SHADOW_DSN=postgresql://...
FALKORDB_URL=redis://falkordb:6379
KAFKA_BOOTSTRAP=...               # or REDIS_STREAMS_URL for dev
TEMPORAL_HOST=temporal.svc:7233
TEMPORAL_NAMESPACE=offramp-fisher

# LLM
LLM_BASE_URL=https://llama.fisher.internal/v1
LLM_API_KEY=...
LLM_MODEL=llama-3.1-70b-instruct

# Provenance
ENGRAM_API_URL=https://engram.svc
F44_NETWORK=base-mainnet           # or base-sepolia in dev

# Observability
DATADOG_API_KEY=...
LOG_LEVEL=INFO
```

All secrets sourced from Key Vault / Secrets Manager via init-container; no plaintext in env files. Pre-deployment scan rejects any commit containing a value matching the secret patterns.

## 6. Scaling Considerations

- **Single-tenant per customer** (AD-10): one AKS namespace dev/staging, dedicated cluster prod. No shared data plane.
- **MCP gateway** is the throughput bottleneck — horizontally scale behind a Service; instrument p50/p99 latency. Target: p50 <50ms, p99 <200ms.
- **Pub/Sub subscriber** is single-process per object channel; partition by object for parallelism. Subscriber lag is the primary SLO — alert at >60h lag (triggers C15 reconciliation).
- **Temporal workers** scale per workflow type with their own task queues. One worker pool per migrated process at first; consolidate after maturity.
- **Shadow Postgres** sized for 30-day rolling shadow data per process. Partition by process_id.
- **FalkorDB** holds the org's Component graph (typically 1–10K nodes); single instance sufficient. Scale read replicas if X-Ray report rendering becomes hot.
- **API quota guard** in MCP gateway polls SF `/limits` every 60s; per-process budget allocation prevents one runaway shadow run from starving cutover-mode writes.
- **Cost ceilings:** alert when monthly Datadog ingest, LLM tokens, or Temporal action count exceeds 110% of baseline.

## 7. Architecture Decision Deltas vs. v2.1 Plan

These supplements address the [research.md Appendix A](research.md#appendix-a-reconciliation-with-salesforce-offramp-build-plan-v21) gaps:

| ID | Delta | Affects |
|---|---|---|
| AD-21 | Add `src/validate/reconcile` (C15) for Pub/Sub 72h-cliff recovery via REST re-sync. Triggered by lag metric or gap event. | §10 of v2.1 plan |
| AD-22 | Add 7th divergence category: **gap_event_full_refetch_required** | §10.4 of v2.1 plan |
| AD-23 | OoE runtime test suite (C10) explicitly covers mixed-DML setup/non-setup boundaries | §18.3 of v2.1 plan |
| AD-24 | MCP gateway implements per-process API quota allocation + `/limits` polling. Surface utilization in observability stack. | §9.7 of v2.1 plan |
| AD-25 | JWT cert rotation runbook + automated quarterly rotation test in sandbox. Phase 0 deliverable. | §17.2 of v2.1 plan |
| AD-26 | Pin SF API version to 66.0 (Spring '26); upgrade cadence one release behind GA. | R6 of v2.1 plan |

## 8. File Structure (target)

```
salesforce/                            (current root)
├── docs/
│   ├── research.md                    ✓ exists
│   ├── architecture.md                ✓ this file
│   └── build-plan.md                  ← Phase-gated execution plan
├── Salesforce-OffRamp-Build-Plan-v2.1.docx   ✓ strategic plan (immutable)
├── CLAUDE.md                          ← project conventions
├── pyproject.toml                     ← uv-managed
├── Makefile                           ← make dev / test / lint
├── .env.example
├── infra/
│   ├── terraform/
│   └── helm/
├── src/
│   ├── core/                          shared models + utils
│   ├── extract/
│   │   ├── pull/                      C1
│   │   ├── dispatch/                  C2
│   │   ├── lwc/                       C3
│   │   └── ooe_audit/                 C4
│   ├── understand/                    C5
│   │   └── orphan/                    C6
│   ├── generate/                      C7
│   │   ├── formula/                   C8
│   │   └── adapters/                  C9
│   ├── runtime/
│   │   ├── ooe/                       C10
│   │   └── rules/                     C11
│   ├── mcp/                           C12
│   ├── validate/
│   │   ├── shadow/                    C13
│   │   ├── compare_mode/              C14
│   │   └── reconcile/                 C15  [AD-21]
│   ├── cutover/                       C16
│   ├── engram/                        C17
│   └── event_bus/                     C18
└── tests/
    ├── conftest.py
    ├── unit/
    ├── integration/                   scratch-org-backed
    └── ooe_runtime/                   200+ cases (§18.3)
```
