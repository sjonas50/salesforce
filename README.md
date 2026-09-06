# Salesforce Off-Ramp

Reverse-engineer a Salesforce org into a dependency graph that shows its work: every automation, every field it reads or writes, every edge tagged with the evidence that produced it and a confidence score.

**X-Ray** (the product being built now) answers five questions about an org:

1. **What is here?** Inventory of all 21 automation categories plus the data model.
2. **What depends on what?** A typed graph across Apex, Flows, formulas, rules, schema, and UI, built from our own parsers and cross-checked against Salesforce's Dependency API.
3. **Where is this used?** Every inbound reference to a field, object, class, or Flow, with evidence.
4. **What happens when this changes?** Change closure, plus the automations that fire on a save in Order-of-Execution sequence.
5. **What can go?** Unused fields, legacy Workflow Rules and Process Builders with blast radius, orphaned Apex with the channel that explains it.

**Agent Factory** (translation + runtime) and **Shadow Mode** (validation) remain in the codebase for year two.

## Status

**Build plan v0.2 (2026-09-05): X-Ray first.** See [docs/strategy.md](docs/strategy.md) for the market study and [docs/build-plan.md](docs/build-plan.md) for the phase plan. Translation, shadow execution, and cutover code stays in-tree but is not on the twelve-month roadmap (AD-27).

| Area | What works today | Tests |
|---|---|---|
| Extraction | Fixture / SFDX directories (C19), REST + Tooling API through the MCP gateway with a Metadata API retrieve (no CLI) for what Tooling cannot read, sf CLI retrieve with re-split on size limits. 21 automation categories + 5 surface categories (layouts, Lightning pages, permission sets, profiles, reports); no passthrough. | `test_source_tree_and_schema.py`, `test_tooling_pull_client.py`, `test_sf_cli_pull_client.py` |
| Apex analysis | Tokenizer-based reference extraction: class graph, SOQL/DML targets, field reads and writes, callouts, named credentials, async targets, entry points, test-class detection, dynamic-access flag (C20). | `test_apex_analyzer.py`, `test_surfaces_data_mdapi.py` |
| Flows | Every element and resource type; derived object / field / Apex / subflow / email references; Tooling JSON and XML accepted. | `test_flow_extractor.py` |
| Formulas | Deterministic parser with `$` globals, `&` concat, 80+ functions; tolerant reference fallback. | `test_formula_parser.py`, `test_formula_references.py` |
| Schema | Objects, fields, lookups, record types, picklists from source tree or describe (C21). | `test_source_tree_and_schema.py` |
| Graph | Typed dependency graph, evidence channel + confidence per edge, Dependency-API cross-check (C22). | `test_tooling_pull_client.py`, `test_extract_e2e.py` |
| Data profile | Record counts (`limits/recordCount`) and custom-field fill rates (one aggregate query per object) attached to graph nodes. | `test_surfaces_data_mdapi.py` |
| Impact | Where-used with live / test / inactive split, change closure, save impact in Order-of-Execution order, unused fields with fill rate and "still referenced by", legacy automation (C23). | `test_impact_and_report.py`, `test_xray_e2e.py` |
| Report | X-Ray HTML with a where-used explorer, save-impact tables, unused / legacy sections, D3 graph; JSON schema 2.0. | `scripts/verify_xray.py` |
| Year two (kept) | OoE runtime, Tier 1/2/3 translators, Shadow Mode, Compare Mode, cutover orchestrator. | existing suites |

Run the whole thing on the fixture org:

```bash
make xray-fixture      # → out/xray/xray.html, out/xray/xray.json, out/xray/extract/*.json
make gate              # lint + typecheck + tests + fixture X-Ray
```

## Quickstart

```bash
make dev          # uv sync + pre-commit hooks
make lint         # ruff check + format check
make typecheck    # mypy strict
make test         # unit tests
make smoke        # smoke (in-memory SF backend)
```

`make help` shows the full target list. See [`docs/build-plan.md`](docs/build-plan.md) for the phase gates.

### CLI

```bash
# Extract from a fixture / SFDX project directory
uv run offramp extract --fixture tests/integration/fixtures/sample_org --out out/fx
uv run offramp extract --source-dir ~/projects/acme-sfdx --out out/acme

# Extract from a live org (SF_* env for JWT bearer auth). Default: REST/Tooling plus a
# Metadata API retrieve for the types Tooling cannot read; no CLI needed on either side.
uv run offramp extract --org acme_prod --out out/acme
uv run offramp extract --org acme_prod --via mdapi --out out/acme      # Metadata API for everything
uv run offramp extract --org acme_prod --via sf-cli --out out/acme     # sf CLI retrieve
uv run offramp extract --org acme_prod --no-data-profile --out out/acme  # skip record counts / fill rates

# Ask the graph (answers from graph.json, no org round-trip)
uv run offramp impact --from out/fx --where-used Lead.Country__c
uv run offramp impact --from out/fx --save Opportunity                  # OoE-ordered save impact
uv run offramp impact --from out/fx --change LeadScoringService --depth 3
uv run offramp impact --from out/fx --unused
uv run offramp impact --from out/fx --legacy

# Full X-Ray report (FalkorDB and LLM annotation are optional)
uv run offramp xray --fixture tests/integration/fixtures/sample_org --out out/xray --no-graph-db --skip-annotations
uv run offramp xray --org acme_prod --out out/acme/xray --save-impact Opportunity --save-impact Lead

# Year-two commands (kept, not extended): generate, shadow, cutover
```

## Documentation

- **[docs/strategy.md](docs/strategy.md)** — market study summary (Sweep, Dependency API status, pricing) and decisions AD-27..AD-31
- **[docs/build-plan.md](docs/build-plan.md)** — v0.2 X-Ray-first phase plan with runnable gates ([v0.1 archived](docs/build-plan-v0.1-archived.md))
- **[docs/architecture.md](docs/architecture.md)** — engineering architecture (components C1–C23, ADs)
- **[docs/research.md](docs/research.md)** — independent technology evaluation
- **[CLAUDE.md](CLAUDE.md)** — project conventions + stack-specific pitfalls

### Runbooks

- [Connect a scratch org](docs/runbooks/connect_scratch_org.md) — JWT bearer setup for `--org`
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
- **Anthropic Claude Sonnet 4.6** for optional LLM annotation (provider-routable)
- **simple-salesforce** for REST, Tooling API, and describe; **sf CLI** for Metadata API retrieves; **gRPC + fastavro** for Pub/Sub CDC
- **Own parsers** (AD-31): tokenizer-based Apex analyzer in `src/offramp/extract/apex`, Flow XML/JSON normalizer, recursive-descent formula parser, regex LWC classifier. No summit-ast, no Salto.
- **FalkorDB** (Cypher) for interactive graph exploration (optional; `networkx` in memory otherwise)
- **Postgres 16** (asyncpg) for app + shadow stores (year two)
- **Engram** (internal) for provenance; **F44** for Base L2 Merkle anchoring of sensitive decisions

## Repo layout

```
src/offramp/
├── core/            shared models (Component, Dependency, SchemaNode, EvidenceChannel), secrets, logging, config
├── extract/         C1–C4, C19–C21
│   ├── pull/        source_tree (C19), tooling_api (REST path), sf_cli, fixture, reconciler
│   ├── apex/        C20 tokenizer + reference extractor → ApexAnalysis
│   ├── categories/  one extractor per category (flow, apex_class, approval_process, rules, …)
│   ├── schema.py    C21 SchemaSnapshot from source tree or describe
│   ├── dispatch/    C2 CMT-driven trigger dispatch, framework detectors
│   ├── lwc/         C3 bundle classifier + Apex/schema imports
│   └── ooe_audit/   C4 Order-of-Execution surface audit
├── understand/      C5–C6, C22–C23
│   ├── dependencies.py  C22 typed graph, evidence + confidence, Dependency-API cross-check
│   ├── impact.py        C23 where-used, change closure, save impact, unused, legacy
│   ├── clustering.py    Louvain / Leiden business-process clusters
│   ├── graph_loader.py  optional FalkorDB materialization
│   ├── orphan/          6-channel orphan resolver (graph-aware)
│   ├── annotate.py      optional LLM annotation
│   └── xray/            report renderer (HTML + JSON schema 2.0)
├── mcp/             C12: gateway (query, tooling_query, describe, restful), quota allocator, JWT auth, real SF backend
├── cli/             extract, xray, impact (+ year-two: generate, shadow, cutover)
├── generate/        C7–C9 (year two): translators, formula parser + emitter, adapters
├── runtime/         C10–C11 (year two): OoE state machine, rules engine
├── validate/        C13–C15 (year two): shadow executor, Compare Mode, reconciliation
├── cutover/         C16 (year two)
├── engram/          C17: provenance client
└── event_bus/       C18: pluggable bus

templates/           xray.html.j2 (report), shadow_dashboard, parity_report

tests/
├── unit/            251 unit tests (no external services)
├── integration/     22 tests; fixture-driven ones run anywhere, FalkorDB / Postgres ones self-skip
├── integration/fixtures/sample_org/   realistic fixture org: 10 Apex classes, 2 triggers, 7 Flows, schema, rules, tooling dumps
├── ooe_runtime/     18 OoE state-machine cases (refire, cascade, mixed-DML, validation)
└── load/            4 throughput + latency benchmarks

infra/
├── helm/offramp/    Helm chart (MCP, Shadow, Cutover CronJob, NetworkPolicies)
└── operator/        on-prem operator (CRDs, RBAC, controller-loop contract)

docs/
├── strategy.md      market study + decisions AD-27..AD-31
├── build-plan.md    v0.2 X-Ray-first plan (v0.1 archived alongside)
├── architecture.md  engineering architecture (C1–C23, ADs)
├── research.md      tech evaluation
└── runbooks/        scratch-org connect, JWT rotation, cutover, rollback, quota, reconciliation
```

## Local development

Everything on the X-Ray path runs with no services: `make gate` needs only `uv`. FalkorDB and Postgres are optional and only used by the tests that name them (they self-skip when unreachable). Bring them up via Docker if you want them:

```bash
# Postgres for app state + shadow store
docker run -d --name offramp-postgres -p 5432:5432 \
  -e POSTGRES_USER=offramp -e POSTGRES_PASSWORD=offramp -e POSTGRES_DB=offramp \
  postgres:16-alpine
docker exec offramp-postgres psql -U offramp -d offramp -c "CREATE DATABASE offramp_shadow;"

# FalkorDB for the Component knowledge graph
docker run -d --name offramp-falkordb -p 6379:6379 falkordb/falkordb
```

For a live org, copy `.env.example` → `.env` and set the `SF_*` variables (Connected App consumer key, integration username, JWT private key path); see the [scratch-org runbook](docs/runbooks/connect_scratch_org.md). For the optional LLM annotation pass set `LLM_API_KEY`. `.env` is gitignored.

The `sf` CLI is only needed for `--via sf-cli`; the default REST/Tooling path needs nothing installed on the customer side.

```bash
make test                              # unit + OoE runtime tests
make gate                              # lint + typecheck + tests + fixture X-Ray + coverage gate
uv run pytest -m integration           # integration suite (service-backed tests self-skip)
uv run pytest -m load                  # benchmarks
uv run pytest                          # everything
```

### What each path can see

The REST/Tooling path reads full bodies for Apex, Flows, validation rules, workflow rules and actions, custom fields, LWC, platform events, CDC channels, layouts, Lightning pages, permission sets and profiles, CMT rows, CronTriggers, the data model, and the data profile. Approval processes, assignment / escalation / auto-response / sharing rules, and reports beyond the newest 300 are not exposed in full through Tooling, so the default `--org` run also issues a Metadata API retrieve for exactly those types through simple-salesforce. `--via mdapi` retrieves everything that way; `--via sf-cli` uses the CLI. Any type that still comes back partial is flagged in the report.

## Deployment

The Helm chart in `infra/helm/offramp/` predates the v0.2 pivot and deploys the year-two services (MCP gateway, shadow subscriber, cutover CronJob). The hosted X-Ray service (OAuth connect, scheduled scans) is Phase D of the build plan and is not in the chart yet. Install what exists with:

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
