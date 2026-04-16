# Research: Comprehensively Offloading Salesforce Workflows

**Date:** 2026-04-16
**Scope:** Migrating Salesforce-native automation (Flow, Process Builder, Workflow Rules, Apex triggers, Approval Processes, Validation Rules, Email Alerts, scheduled jobs) onto external systems to reduce platform lock-in.

## Executive Summary

A comprehensive Salesforce off-ramp requires a **3-layer external stack**: (1) an event-ingestion path via the Pub/Sub API (gRPC), (2) a durable orchestrator replacing Flow/Apex logic, and (3) an external rules engine for Validation Rules. No single product covers all automation types — plan for composition. The recommended opinionated stack is **Pub/Sub API → Kafka → Temporal**, with **GoRules ZEN + Pydantic v2** for validation logic and **simple-salesforce 1.12.9** for write-back. Process Builder and Workflow Rules reached **end-of-support 2025-12-31**, so migration pressure is real and time-bounded. The only safe execution model is **strangler-fig at the object level** — never org-wide cutover. Public end-to-end "Salesforce de-automation" case studies are scarce; most material covers intra-Salesforce migration. Plan to manually rewrite Flow logic against extracted XML — no production-ready Flow-to-code converter exists.

## Problem Statement

Salesforce automation lives in Flow, Apex triggers, Approval Processes, and Validation Rules — all governed by Salesforce's Order of Execution and per-edition governor limits. Externalizing this logic requires: a real-time change feed out of Salesforce, a durable workflow engine that can model long-running approvals, a rules engine that replicates declarative validation, and a write-back path that doesn't recursively trip the same governor limits. The principal risks are (a) the 72-hour CDC retention cliff, (b) shared org-wide API quota exhaustion, (c) gap events silently dropping field-level deltas, and (d) Approval Processes emitting no CDC events.

## Technology Evaluation

### Orchestration Engines (replacing Flow / Process Builder / Apex triggers)

| Tool | License | Hosting | Last Release | Verdict |
|---|---|---|---|---|
| **Temporal** | MIT (server) | Self-host / Cloud | v1.26.x, Apr 2026 | **Recommended** — durable execution, Python SDK 1.16, fits long-running approvals |
| **n8n** (self-host) | Sustainable Use | Both | Active 2026, 90k★ | **Recommended (no-code lane)** — Salesforce node, fast for Process Builder analogs |
| **Kestra** | Apache 2.0 | Both | Active 2026, 26.6k★ | Consider — YAML DAGs, Python tasks, growing |
| **Prefect** | Apache 2.0 | Both | v3.6.26, Dec 2025 | ETL-shaped, weak fit for event-driven approvals |
| **Camunda 8 / Zeebe** | Apache + EE | Both | v8.6, Oct 2024 | **Avoid** — Oct 2024 license change forces Enterprise (~$330k/yr at AWS scale) for prod self-host |
| **Windmill** | AGPLv3 / EE | Both | Active 2026, 16.1k★ | Consider for small teams |
| **AWS Step Functions** | Proprietary | SaaS | Ongoing | Consider only if AWS-locked and team accepts vendor lock-in |

### iPaaS (low-code lane for ops users)

**Recommended:** **n8n self-hosted** (free, Salesforce node, 600+ integrations) for dev-led teams. **Make.com** for cheapest SaaS path. **Avoid MuleSoft** — Salesforce-owned, embeds further platform dependency. **Workato/Boomi** are enterprise-grade but $10k+/yr minimums.

### Salesforce Data Extraction

| Tool | Version | Use Case |
|---|---|---|
| **simple-salesforce** | 1.12.9 (Aug 2025) | **Recommended** — REST + Bulk API 2.0 from Python 3.9–3.13 |
| **Pub/Sub API gRPC client** | Salesforce-managed | **Recommended** — only official real-time CDC path |
| **Airbyte** (OSS) | v1.x | Bulk historical sync to warehouse; **no real-time CDC** for SF |
| **Fivetran** | Managed | More reliable SF CDC than Airbyte; expensive at scale |
| **Heroku Connect** | Managed | **Avoid** for new arch — bidi sync conflicts, $4k+/mo |

### Rules Engines (replacing Validation Rules / declarative Apex)

**Recommended:** **GoRules ZEN** (MIT, Rust + Python bindings, JSON-storable decision tables, 2026-active) for table-driven validation. **Pydantic v2 validators** for inline field-level rules in Python services. **Avoid Drools** unless Java-native team.

## Architecture Patterns Found

Five dominant patterns from the prior-art survey:

1. **Apex Trigger → Platform Event → External Queue → Service** — most common. Apex triggers publish semantic Platform Events; Salesforce Event Relay (GA 2024) or Confluent connector egresses to Kafka/SQS/EventBridge; Lambda/Temporal executes the logic that was in Flow. Best for async automation (notifications, downstream sync, audit).

2. **CDC + Kafka + Reverse-ETL Mirror** — replicate SF objects to Snowflake/BigQuery via Pub/Sub CDC; run dbt/Airflow transformations; write back via Hightouch/Census or REST upserts. Best for derived-field computation. Pain: write-back latency (minutes) cannot replace synchronous validation.

3. **Outbound Message / Platform Event → Webhook → Temporal Workflow** — best pattern for multi-party approvals. State and waits live in Temporal (durable, replayable) rather than Salesforce Approval Process.

4. **Strangler Fig at Object Level** — new external automation runs in shadow mode, compared against legacy Flow/Apex. Cut over object by object after N days of clean parity. **Never org-wide cutover.** This is the only universally-recommended migration model.

5. **Specification-Driven Rebuild from Metadata Export** — Salto NaCl or `sf project retrieve start` produces inventory. Lightning Flow Scanner scores complexity. Each automation rewritten manually against the extracted XML spec. Slowest but most maintainable.

## Key APIs and Services

**Salesforce Spring '26 / API v66.0:**

- **REST API v66.0** — OAuth2 JWT Bearer preferred. Enterprise: 100K + 1K/user/24h. Hard 403 once breached. Rolling window, not midnight reset.
- **Bulk API 2.0** — 150 MB/job, 100M records/24h, 25 concurrent jobs, 15K batches/24h shared with Bulk 1.0. Query results cap 15 GB/job.
- **Pub/Sub API (gRPC)** — modern CDC channel. Avro-encoded, max 1000 streams/HTTP2 channel, 4 MB/PublishRequest, **72h retention**. JWT Bearer auth only.
- **Change Data Capture** — `/data/ChangeEvents` topics. Emits **gap events** (header-only, no field data) under load — naive consumers silently lose deltas.
- **Platform Events** — Standard ~250K deliveries/24h shared with CDC. 72h retention for high-volume.
- **Metadata API v66.0** — 400 MB ZIP cap. Flow wildcard `*` unreliable; enumerate explicitly. **Deployed Flows arrive inactive by default** outside prod.
- **Tooling API** — only path to Apex source via `SELECT Body FROM ApexClass`.

**External infra:** Kafka (MSK), Temporal (orchestration), Snowflake/BigQuery (warehouse), Datadog/Grafana (lag + quota observability), AWS Secrets Manager (JWT keys), Hightouch/Census (reverse-ETL).

## Known Pitfalls and Risks

1. **CDC 72h cliff** — subscriber down >3 days = unrecoverable replay loss. Build a reconciliation job triggered by gap events or lag >60h.
2. **Gap events** — silent field-data holes during high load. Detect and trigger full record re-fetch.
3. **Flow deploys as inactive** — every Metadata API deploy lands inactive outside prod. Automate post-deploy activation via Tooling API.
4. **Mixed-DML exceptions** — Apex can't modify setup + non-setup objects in one transaction. External Platform Event handlers can silently hit this.
5. **API quota is org-wide** — 100-user Enterprise org shares 200K calls/24h across ALL integrations + users. Monitor `/limits`.
6. **Approval Process has no CDC** — must fire Platform Event from Apex trigger on `ProcessInstance` or poll. Most fragile part of approval offload.
7. **Order of Execution traps** — Validation → Assignment → Auto-Response → Workflow → Process/Flow → Trigger. Externalize an entire object's automation or none of it; partial externalization causes race conditions.
8. **Validation Rule externalization is hardest** — they fire synchronously before save. External validation = before-save Flow calling external service (latency + new failure mode). Most teams leave Validation Rules in Salesforce longest.
9. **JWT cert rotation = production incident** — Salesforce sends no expiry warnings. Automate rotation; test under simulated expiry in sandbox.
10. **No Flow-to-code converter** — every Flow must be manually rewritten. Budget accordingly.
11. **Process Builder / Workflow Rules end-of-support 2025-12-31** — no bug fixes. Audit and migrate before any offload work begins.
12. **Camunda 8.6 license change (Oct 2024)** — self-managed prod now requires Enterprise. Don't pick Camunda for greenfield.
13. **n8n Sustainable Use License** — fine for internal automation, blocks SaaS-product use.
14. **Confluent SF CDC connector fails at >1M records** — fallback to custom polling microservice (~30s cycle = degraded SLA).

## Recommended Stack (Opinionated)

```
Salesforce Org (v66.0, Enterprise+)
├── Pub/Sub API (gRPC, CDC + Platform Events) ──► Kafka (MSK, retention ≥ 96h)
├── REST API (CRUD, approval actions)        ◄── Temporal Workers
├── Bulk API 2.0 (historical backfill)
└── Metadata API + sf CLI (config extraction, CI)

Kafka ──► Temporal (orchestration, sagas, durable approvals)
                ├── GoRules ZEN (decision tables / validation)
                ├── Pydantic v2 (inline field validation)
                └── simple-salesforce 1.12.9 (write-back)

Snowflake (audit, analytics) ◄── Temporal Workers
Hightouch (throttled reverse-ETL) ──► Salesforce REST

Observability: Datadog (subscriber lag, /limits quota, workflow failures)
Secrets:       AWS Secrets Manager (JWT keys, OAuth tokens, cert rotation automation)
Inventory:     Salto NaCl + Lightning Flow Scanner (pre-migration)
No-code lane:  n8n self-hosted (replacing Email Alerts, simple Process Builders)
```

**Avoid:** MuleSoft (lock-in + cost), Heroku Connect ($4k+/mo + bidi conflicts), Camunda 8 (license), CometD Streaming (legacy), Drools (Java-only).

## Migration Playbook

1. **Inventory** — `sf project retrieve start` + Salto NaCl + SOQL on `FlowDefinition` / `ApexTrigger` / `ValidationRule`. Pipe through Lightning Flow Scanner.
2. **Dependency map** — per automation: objects touched, co-firing automations, outbound callouts.
3. **Classify** — synchronous validation (keep longest); async notifications (first to externalize); long-running approvals (Temporal target).
4. **Shadow mode** — both paths run in parallel; emit results to comparison topic; alert on divergence.
5. **Object-level cutover** — deactivate legacy automation per object after clean shadow parity.
6. **Governance** — block new Flows/Apex post-cutover; route new automation through external system.

## Open Questions

- **Salesforce edition?** Enterprise vs. Unlimited changes API quota ~5x and Platform Event allocations.
- **Bidirectional sync or read-only extraction?** Bidi materially complicates conflict resolution.
- **Volume baseline** (workflows/day, records/day)? Temporal self-host overkill below ~1k/day.
- **Multi-step parallel approvals?** Temporal handles natively; n8n does not.
- **Timeline for full SF exit vs. parallel-run?** Pub/Sub API requires SF org alive — unusable post-exit.
- **Existing MuleSoft license?** Changes cost calculus.
- **Mixed-DML in current Apex?** Must audit before external trigger replacement.
- **Custom Apex process plugins on Approvals?** No direct external equivalent.

## Sources

**Orchestration & libraries:**
- [Temporal GitHub](https://github.com/temporalio/temporal) · [Python SDK](https://pypi.org/project/temporalio/)
- [State of Workflow Orchestration 2025](https://www.pracdata.io/p/state-of-workflow-orchestration-ecosystem-2025)
- [n8n alternatives 2026](https://dev.to/lightningdev123/top-5-n8n-alternatives-in-2026-choosing-the-right-workflow-automation-tool-54oi)
- [GoRules ZEN](https://github.com/gorules/zen)
- [simple-salesforce](https://pypi.org/project/simple-salesforce/)
- [Camunda 8 self-managed pricing forum](https://forum.camunda.io/t/camunda-8-self-managed-pricing-only-zeebe-engine/58604)

**Salesforce APIs:**
- [Spring '26 Developer Guide v66.0](https://developer.salesforce.com/blogs/2026/01/developers-guide-to-the-spring-26-release)
- [API limits cheatsheet](https://developer.salesforce.com/docs/atlas.en-us.salesforce_app_limits_cheatsheet.meta/salesforce_app_limits_cheatsheet/salesforce_app_limits_platform_api.htm)
- [Pub/Sub API allocations](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/allocations.html) · [gRPC overview](https://developer.salesforce.com/docs/platform/pub-sub-api/guide/grpc-api.html)
- [CDC gap events](https://developer.salesforce.com/docs/atlas.en-us.change_data_capture.meta/change_data_capture/cdc_other_events_gap.htm)
- [Bulk API 2.0 limits](https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/bulk_common_limits.htm)
- [Apex governor limits](https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_gov_limits.htm)
- [Workflow Rules / Process Builder EOL](https://help.salesforce.com/s/articleView?id=001096524&language=en_US&type=1)
- [OAuth JWT Bearer Flow](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_jwt_flow.htm&language=en_US&type=5)

**Architecture & prior art:**
- [Event-Driven SF CDC — Mishima Ltd](https://mishimaltd.medium.com/event-driven-salesforce-change-data-capture-8308051841fc)
- [SF in Event-Driven Architecture — Van Vlaenderen](https://medium.com/@kris_22373/integrating-salesforce-in-an-event-driven-architecture-56865ff50c91)
- [Streaming SF to GCP Pub/Sub — League](https://medium.com/inside-league/real-time-streaming-salesforce-updates-to-pubsub-d9aedd5973ca)
- [SF Integration Patterns](https://architect.salesforce.com/docs/architect/fundamentals/guide/integration-patterns.html)
- [Confluent SF CDC Source Connector](https://docs.confluent.io/kafka-connectors/salesforce/current/change-data-capture/overview.html)
- [Salto metadata guide](https://www.salto.io/blog-posts/the-complete-guide-to-retrieving-salesforce-metadata)
- [Lightning Flow Scanner](https://github.com/Flow-Scanner/lightning-flow-scanner) · [apex-parser](https://github.com/nawforce/apex-parser)
- [Strangler Fig — Thoughtworks](https://www.thoughtworks.com/en-us/insights/articles/embracing-strangler-fig-pattern-legacy-modernization-part-one)
- [Temporal + Kafka durable execution — Waehner](https://www.kai-waehner.de/blog/2025/06/05/the-rise-of-the-durable-execution-engine-temporal-restate-in-an-event-driven-architecture-apache-kafka/)

---

## Appendix A: Reconciliation with Salesforce-OffRamp-Build-Plan-v2.1

This research was conducted independently of the existing internal build plan ([Salesforce-OffRamp-Build-Plan-v2.1.docx](../Salesforce-OffRamp-Build-Plan-v2.1.docx), April 2026). Comparing the two:

### Where the plan and research converge (research validates the plan)

- **Three-tier execution model** (Rules / Temporal / LangGraph) matches the externally-observed best-practice distribution. Plan's 70/20/10 split is consistent with prior-art findings.
- **Custom Python rules engine over Drools/json-rules-engine** — plan's rationale (OoE re-fire semantics aren't generic) is the same conclusion my Tier 1 research reached. **AD-4 is sound.**
- **Temporal as the durable orchestrator** — independently the top recommendation. **AD-5 is sound; avoiding Camunda 8 was correct (Oct 2024 license change makes it untenable for self-hosted prod).**
- **MCP gateway as single SF boundary** — equivalent to the "controlled egress" pattern across all five reference implementations.
- **Strangler-fig at object/process level, never org-wide** — universally validated by every public case study; the staged 1/5/25/50/100% protocol is consistent with the Salesforce Event Relay → EventBridge pattern at AWS.
- **Lightning Flow Scanner + Salto + summit-ast + tree-sitter** — exactly the open-source toolchain my research independently surfaced.
- **Pub/Sub API gRPC for CDC** — the only official real-time channel; plan correctly uses it.
- **No Flow-to-code converter exists** — research confirms; plan correctly assumes manual/translator-driven rewrite.
- **Customer-hosted single-tenant** (AD-10, AD-14) — aligned with regulated-customer expectations from prior art.

### Where the plan goes deeper than research (Attic IP not surfaceable externally)

These are differentiators research could not have generated on its own:

- **Engram + F44 provenance layer** (Base L2 Merkle anchoring) — no public analog. Strong moat for regulated buyers; the cryptographic audit trail is the compliance claim no competitor can replicate without rebuilding the substrate.
- **OoE Surface Audit** (§7.7) bounding runtime scope per-customer — converts unbounded compatibility into bounded. Research had flagged "Order of Execution is a trap"; the plan's response is materially better than my mitigation suggestion.
- **Compare Mode harness** (§9.2.6) using SF debug logs from week 16 — ~80% of OoE bugs at ~20% of the shadow-infra cost. Clever; not in any public reference.
- **Translation Verification** with Finding Triage Queue + customer communication playbook — operationalizes a politically-sensitive class of finding (pre-existing customer formula bugs) that destroys vendor relationships if mishandled.
- **Managed Package Adapter Generator** — directly addresses the scope wall research called out (CPQ/Conga/DocuSign Apex is obfuscated and untranslatable). Hand-tuned top-5 library is the right move.
- **Dual-target generation** for Tier 1/Tier 2 boundary — collapses re-classification cost from 20% to ~8%. Not in public literature.
- **Behavioral Parity Report** as a deliverable — turns "we cannot achieve perfect parity" from a weakness into a structured audit artifact. Aligns with regulated-customer auditor expectations.

### Gaps in the plan that this research surfaces (worth incorporating)

These were not visible in the v2.1 plan and warrant explicit treatment:

1. **Pub/Sub API 72-hour replay-id cliff.** Plan assumes shadow execution against live CDC but doesn't specify the reconciliation job triggered when a subscriber lags >60h or the org emits gap events. **Recommend:** add explicit reconciliation activity in the MCP gateway that re-syncs affected records via REST/SOQL when replay state is lost. Risk register doesn't currently name this.
2. **CDC gap events** (header-only, field data dropped under load) are silent. Plan's shadow-execution architecture (§10.2) compares "the next CDC event from production" — if that event is a gap, the comparison is meaningless. **Recommend:** divergence-categorization (§10.4) needs a seventh bucket: "gap event — full re-fetch required."
3. **Mixed-DML exceptions** in Apex (setup vs. non-setup objects in one transaction) are a class of failure the OoE runtime test suite should explicitly cover. Not currently named in §18.3.
4. **Org-wide API quota** is shared across ALL integrations + users + reverse-ETL + nightly Bulk + Shadow Mode reads. Plan mentions "API budget management" in the MCP gateway (§9.7) but doesn't specify the quota-pressure escalation policy when multiple migrated processes contend. **Recommend:** add a `/limits` endpoint poll + per-process quota allocation to the MCP gateway, surface in observability stack.
5. **JWT cert rotation = silent production incident.** Salesforce sends no expiry warnings. Plan §17.2 mentions OAuth Connected App but not cert lifecycle automation. **Recommend:** add cert rotation runbook + automated rotation test in sandbox to Phase 0 deliverables.
6. **API version pinning to v66.0 (Spring '26).** R6 mentions "version pinning on SF CLI and API version" generally but no specific version. Plan should pin and schedule the upgrade cadence (Salesforce ships 3 releases/year).
7. **No-code lane for ops users (n8n self-hosted)** is absent. Plan handles delivery exclusively via consultancy. Worth considering whether n8n complements Agent Factory for low-complexity Process Builder analogs that don't justify a Temporal workflow — could reduce per-process cutover cost on the long tail. (May be intentionally out of scope; flagging for awareness.)

### Honest disagreements

None material. Research's initial recommendation of GoRules ZEN for validation rules is **superseded** by the plan's purpose-built engine — the plan's reasoning (OoE-aware re-fire semantics aren't expressible in a generic decision-table engine) is correct and stronger than the research-default recommendation.

### Net assessment

The build plan is internally consistent, more detailed than research could produce externally, and well-defended in its v2.1 remediations. The plan should **adopt** the six gap items above (especially #1, #2, #4, #5) into the relevant sections (§10.4 divergence categories, §15 risk register, §17.2 security architecture, §18.3 OoE test suite). Research does not recommend any architecture-level change to the plan.
