# Salesforce Off-Ramp

Reverse-engineer a Salesforce org's automation surface, translate each component to the right execution tier (deterministic rules, durable workflows, or AI agents), validate via shadow execution against live production traffic, and migrate incrementally with cryptographically-provenanced rollback.

**Three products in one platform:**
- **X-Ray** — diagnostic. Inventory + dependency graph + complexity heatmap of an org.
- **Agent Factory** — translation + runtime. Generates the rules / workflows / agents that replicate the extracted business logic.
- **Shadow Mode** — validation + regression detection. Forks live CDC traffic and runs translated artifacts in parallel, catching divergence before cutover and continuously after.

## Status

All six build-plan phases shipped. **163 unit + integration tests** passing, plus **4 load benchmarks**. Real services (no mocks): Salesforce Pub/Sub gRPC, Postgres, FalkorDB, Anthropic Claude Sonnet 4.6.

| Phase | What | Tests |
|---|---|---|
| 0 — Scaffold | Project skeleton, shared models, JWT cert rotation runbook | 25 |
| 1 — Extract Engine | 21-category metadata extractors + dynamic dispatch + LWC analyzer + OoE Surface Audit | +18 |
| 2 — Understanding + X-Ray | FalkorDB graph + Louvain clustering + LLM annotation (Claude Sonnet 4.6) + complexity scoring + 6-channel orphan resolver + interactive HTML report | +12 |
| 3 — OoE Runtime + Translators + MCP | 21-step OoE state machine + SF formula parser + Tier 1/2/3 translators + dual-target generation + managed-package adapters + MCP gateway + AD-24 quota allocator | +52 |
| 4 — Shadow + Compare Mode | Real Pub/Sub gRPC subscriber + Postgres shadow store + 7-category divergence (incl. AD-22 gap-event) + AD-21 lag/gap reconciliation + readiness scoring + dashboard + compliance export + Compare Mode debug-log replay | +26 |
| 5 — Cutover Orchestrator | Hash-deterministic per-record router + saga compensation + auto-advance/rollback driven by readiness scores + Engram + F44 anchoring + Behavioral Parity Report + post-cutover monitor + Helm chart + on-prem operator skeleton + 4 production runbooks | +30 + 4 load |

## Quickstart

```bash
make dev          # uv sync + pre-commit hooks
make lint         # ruff check + format check
make typecheck    # mypy strict
make test         # unit tests
make smoke        # smoke (in-memory SF backend)
```

`make help` shows the full target list. See [`docs/build-plan.md`](docs/build-plan.md) for the per-phase test gates.

### CLI

```bash
# Phase 1: extract a Salesforce org's automation surface
uv run offramp extract --fixture tests/integration/fixtures/sample_org \
                       --out out/sample_org

# Phase 2: render the X-Ray report (FalkorDB + Claude Sonnet 4.6 required)
uv run offramp xray --fixture tests/integration/fixtures/sample_org \
                    --out out/sample_org/xray

# Phase 3: translate components to runtime artifacts
uv run offramp generate --fixture tests/integration/fixtures/sample_org \
                        --out out/artifact

# Phase 4: shadow execution + reports
uv run offramp shadow start --process-id demo --artifact out/artifact/tier1 \
                            --events tests/integration/fixtures/synthetic_events.json
uv run offramp shadow status --process-id demo
uv run offramp shadow report --process-id demo --out out/shadow_report
uv run offramp shadow replay-log --process-id demo --artifact out/artifact/tier1 \
                                 --log-file path/to/sf_debug.log

# Phase 5: cutover orchestration
uv run offramp cutover begin    --process-id demo
uv run offramp cutover advance  --process-id demo --dry-run
uv run offramp cutover advance  --process-id demo
uv run offramp cutover status   --process-id demo
uv run offramp cutover rollback --process-id demo --confirm
uv run offramp cutover monitor  --process-id demo --auto-rollback
uv run offramp cutover parity-report --process-id demo --org-alias fisher \
                                     --out out/parity
```

## Documentation

- **[docs/research.md](docs/research.md)** — independent technology evaluation
- **[docs/architecture.md](docs/architecture.md)** — engineering architecture (components C1–C18, ADs)
- **[docs/build-plan.md](docs/build-plan.md)** — phase-gated execution plan
- **[CLAUDE.md](CLAUDE.md)** — project conventions + stack-specific pitfalls

### Runbooks

- [JWT cert rotation](docs/runbooks/jwt_cert_rotation.md) — AD-25
- [Cutover advance](docs/runbooks/cutover_advance.md) — staged-percentage advance flow
- [Cutover rollback](docs/runbooks/cutover_rollback.md) — auto + instant
- [Quota incident](docs/runbooks/quota_incident.md) — AD-24 quota exhaustion
- [Replay-id reconciliation](docs/runbooks/reconcile_replay_lag.md) — AD-21 72h-cliff recovery

## Stack

- **Python 3.12**, [`uv`](https://docs.astral.sh/uv/) for deps, ruff (lint+format), mypy strict, pytest + pytest-asyncio
- **Pydantic v2** for all data boundaries; **structlog** for structured logging
- **FastAPI** + **MCP server SDK** for the gateway (the single Salesforce interface)
- **Temporal** (Python SDK 1.16+) for Tier 2 durable workflows
- **LangGraph** for Tier 3 judgment-required agents (run inside Temporal activities)
- **Anthropic Claude Sonnet 4.6** for Phase 2 LLM annotation (provider-routable)
- **simple-salesforce** for REST + Bulk API 2.0; **gRPC + fastavro** for Pub/Sub CDC
- **FalkorDB** (Cypher) for the Component knowledge graph
- **Postgres 16** (asyncpg) for app + shadow stores
- **tree-sitter-javascript** for LWC analysis; **summit-ast** for Apex; **lightning-flow-scanner-core** for Flows
- **Salto** + **sf CLI** for metadata extraction
- **Engram** (internal) for provenance; **F44** for Base L2 Merkle anchoring of sensitive decisions

## Repo layout

```
src/offramp/
├── core/            shared models, secrets, logging, config
├── extract/         C1–C4: pull, dispatch, lwc, ooe_audit, per-category extractors
├── understand/      C5–C6: graph, annotate, cluster, orphan, X-Ray report
├── generate/        C7–C9: tier1/tier2/tier3 translators, formula parser, managed-package adapters
├── runtime/         C10–C11: OoE state machine, rules engine
├── mcp/             C12: gateway, tools, quota allocator, real SF backend
├── validate/        C13–C15: shadow executor, Compare Mode, AD-21 reconciliation
├── cutover/         C16: router, saga, orchestrator, parity report, post-cutover monitor
├── engram/          C17: provenance client
├── event_bus/       C18: pluggable bus
└── cli/             offramp CLI entry points

tests/
├── unit/            136 unit tests (fast, no external services)
├── integration/     27 integration tests (require Postgres + FalkorDB)
├── ooe_runtime/     OoE state-machine cases (refire, cascade, mixed-DML, validation)
└── load/            4 throughput + latency benchmarks

infra/
├── helm/offramp/    Helm chart (MCP, Shadow, Cutover CronJob, NetworkPolicies)
└── operator/        on-prem operator (CRDs, RBAC, controller-loop contract)

docs/
├── research.md      tech evaluation
├── architecture.md  engineering architecture (C1–C18, ADs)
├── build-plan.md    phase-gated execution plan
└── runbooks/        production runbooks (cutover, rollback, quota, reconciliation, JWT rotation)
```

## Local development

The integration tests need real services. Bring them up via Docker:

```bash
# Postgres for app state + shadow store
docker run -d --name offramp-postgres -p 5432:5432 \
  -e POSTGRES_USER=offramp -e POSTGRES_PASSWORD=offramp -e POSTGRES_DB=offramp \
  postgres:16-alpine
docker exec offramp-postgres psql -U offramp -d offramp -c "CREATE DATABASE offramp_shadow;"

# FalkorDB for the Component knowledge graph
docker run -d --name offramp-falkordb -p 6379:6379 falkordb/falkordb
```

For the LLM annotation pass (X-Ray), copy `.env.example` → `.env` and fill in `LLM_API_KEY` + `ANTHROPIC_API_KEY` with your Anthropic API key. `.env` is gitignored.

```bash
make test                              # unit tests only
uv run pytest -m integration           # integration suite (needs Postgres + FalkorDB)
uv run pytest -m load                  # benchmarks
uv run pytest                          # everything
```

## Deployment

The Helm chart in `infra/helm/offramp/` is single-tenant per customer. Install:

```bash
helm install offramp infra/helm/offramp/ \
  --namespace offramp-<customer> --create-namespace \
  --set customer.alias=<customer> \
  --set customer.salesforceOrgAlias=<org_alias> \
  --set image.tag=<version>
```

See [`infra/helm/offramp/values.yaml`](infra/helm/offramp/values.yaml) for the full configuration surface. On-prem customers see [`infra/operator/README.md`](infra/operator/README.md).

## License

Proprietary — © The Attic AI, Inc. All rights reserved.
