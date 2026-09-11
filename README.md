# Salesforce Off-Ramp

Reverse-engineer a Salesforce org into a dependency graph that shows its work: every automation, every field it reads or writes, every edge tagged with the evidence that produced it and a confidence score.

**X-Ray** (the product being built now) answers five questions about an org:

1. **What is here?** Inventory of all 22 automation categories and 8 UI/access surfaces, plus the data model and installed packages.
2. **What depends on what?** A typed graph across Apex, Flows, formulas, rules, schema, and UI, built from our own parsers and cross-checked against Salesforce's Dependency API.
3. **Where is this used?** Every inbound reference to a field, object, class, or Flow, with evidence.
4. **What happens when this changes?** Change closure, plus the automations that fire on a save in Order-of-Execution sequence.
5. **What can go?** Unused fields, legacy Workflow Rules and Process Builders with blast radius, orphaned Apex with the channel that explains it.
6. **Is the model right?** Structural cross-checks, a repo-vs-org diff, and behavioural verification that runs each flow in the org under a debug trace and compares the path with the reverse-engineered definition.
7. **What does it mean?** Static health checks, a platform-neutral process library, and LLM annotations built from the full dossier of each component, with evidence, unknowns and an earned confidence score.

**Agent Factory** (translation + runtime) and **Shadow Mode** (validation) remain in the codebase for year two.

## Status

**Build plan v0.2 (2026-09-05): X-Ray first.** See [docs/strategy.md](docs/strategy.md) for the market study and [docs/build-plan.md](docs/build-plan.md) for the phase plan. Translation, shadow execution, and cutover code stays in-tree but is not on the twelve-month roadmap (AD-27).

| Area | What works today | Tests |
|---|---|---|
| Extraction | Fixture / SFDX directories with multi-root package semantics (C19), REST + Tooling API through the MCP gateway with a Metadata API retrieve (no CLI) for what Tooling cannot read, sf CLI retrieve with re-split on size limits, `/limits` budget guard. 22 automation categories (incl. Aura) + 8 surface categories (layouts, Lightning pages, permission sets, profiles, reports, tabs, apps, paths), custom-metadata records, installed packages, bundle sources; no passthrough. | `test_source_tree_and_schema.py`, `test_tooling_pull_client.py`, `test_sf_cli_pull_client.py` |
| Apex analysis | Grammar-backed engine (Salesforce's ANTLR grammar via Node, `make apex-parser`) with scoped variable typing; the own tokenizer is the fallback. Class graph, SOQL/DML targets, field reads and writes, callouts, named credentials, async targets, entry points, Apex-defined trigger dispatch tables, dynamic-access flag (C20). NPSP: 13 unresolved references out of 24,617 edges; EDA and apex-recipes: 0. | `test_apex_analyzer.py` (both engines), `test_apex_ast.py` |
| Flows | Every element and resource type; derived object / field / Apex / subflow / email references; Tooling JSON and XML accepted. | `test_flow_extractor.py` |
| Formulas | Deterministic parser with `$` globals, `&` concat, 80+ functions; tolerant reference fallback. | `test_formula_parser.py`, `test_formula_references.py` |
| Schema | Objects, fields, lookups, record types, picklists from source tree or describe (C21). | `test_source_tree_and_schema.py` |
| Graph | Typed dependency graph, evidence channel + confidence per edge, Dependency-API cross-check (C22). | `test_tooling_pull_client.py`, `test_extract_e2e.py` |
| Data profile | Record counts (`limits/recordCount`) and custom-field fill rates (one aggregate query per object) attached to graph nodes. | `test_surfaces_data_mdapi.py` |
| Impact | Where-used with live / test / inactive split, change closure, save impact in Order-of-Execution order, unused fields with fill rate and "still referenced by", legacy automation (C23). | `test_impact_and_report.py`, `test_xray_e2e.py` |
| Validation | `offramp compare` (repo vs org diff, C25); `offramp verify` runs each flow in the org under a debug trace and checks visited elements, decisions, action targets and DML against the definition, `--roundtrip` deploys the definition rendered back to Flow XML and requires an identical trace (C26); `offramp recipes` / `verify --auto-recipes` synthesise the records and inputs from the schema and entry conditions. Live on a Developer Edition: 2 of 2 flows pass on generated recipes alone. | `test_verify.py`, `test_compare.py`, `test_health_and_recipes.py` |
| Health checks | `offramp health` and `health.json` on every scan: picklist values that do not exist, callouts inside a save path, owner alerts after queue assignment, same-record after-save updates, multiple triggers per object, missing fault paths. | `test_health_and_recipes.py` |
| Change log | `offramp sync` scans into the library on a schedule; `offramp changes` lists what moved since the last scan, per component (D.6). | `test_changes.py` |
| Annotations | Every automation component is annotated by the LLM from a dossier (static facts, process model, dependency neighbourhood, health and verification results, source); the model returns evidence and unknowns and confidence is earned through caps; empty sharing rules are answered by rule, managed code is described from the outside; clusters get business-process narratives with risks. Reusable across scans by content hash. | `test_annotate.py` |
| Process library | Every automation normalized to a platform-neutral `ProcessDefinition` (trigger, conditions, steps, data effects), content-addressed so identical logic across scans and orgs is one reusable entry; scan history, diffs, shape families, search; Markdown + Mermaid rendering; optional persistent FalkorDB mirror (C24, AD-32). | `test_knowledge.py` |
| Report | X-Ray HTML with a where-used explorer, save-impact tables, health findings, unused / legacy sections, annotated components and process narratives, D3 graph; JSON schema 2.0. | `scripts/verify_xray.py` |
| Year two (kept) | OoE runtime, Tier 1/2/3 translators, Shadow Mode, Compare Mode, cutover orchestrator. | existing suites |

Run the whole thing on the fixture org:

```bash
make apex-parser       # once: the grammar-backed Apex parser (Node); without it the tokenizer is used
make xray-fixture      # → out/xray/xray.html, out/xray/xray.json, out/xray/extract/*.json
make gate              # lint + typecheck + tests + fixture X-Ray
```

Against a real org (a Developer Edition passed Gate A of the build plan; see `docs/build-plan.md` and CLAUDE.md pitfalls 14–34 for everything the live runs taught):

```bash
sf org login web --alias my-org
scripts/scratch_org.sh deploy && scripts/scratch_org.sh xray   # deploy the fixture, then scan (SF_AUTH_MODE=sf-cli)
uv run offramp xray --org my-org --auth sf-cli --out out/my-org --library out/library
```

## Quickstart

```bash
make dev          # uv sync + pre-commit hooks + Apex parser + local FalkorDB (no Docker)
make falkordb     # FalkorDB as brew Redis + module, persisted in ~/.local/share/offramp
make falkordb-browser   # graph UI on http://localhost:3000
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

# Build the reusable process library while you scan, then use it
uv run offramp extract --fixture tests/integration/fixtures/sample_org --out out/fx --library ~/.offramp/library
uv run offramp kg search "lead routing"
uv run offramp kg show LeadRouting --format mermaid          # or md | json | code
uv run offramp kg families                                    # processes that share a shape
uv run offramp kg scans && uv run offramp kg diff <scan_a> <scan_b>
uv run offramp kg export --out library.json

# Full X-Ray report (FalkorDB and LLM annotation are optional)
uv run offramp xray --fixture tests/integration/fixtures/sample_org --out out/xray --no-graph-db --skip-annotations
uv run offramp xray --org acme_prod --out out/acme/xray --save-impact Opportunity --save-impact Lead
uv run offramp xray --org acme_prod --out out/acme/xray --annotations out/acme/xray/annotations.json   # reuse annotations

# Validate the model against the org
uv run offramp compare --a out/acme_repo/extract --b out/acme/extract --a-is-subset   # repo vs org
uv run offramp recipes --from out/acme/extract --out recipes.json                     # review, then:
uv run offramp verify --org acme_prod --auth sf-cli --from out/acme/extract --recipes recipes.json --roundtrip
uv run offramp verify --org acme_prod --auth sf-cli --from out/acme/extract --auto-recipes   # no file needed

# Static health checks (also written as health.json by every scan)
uv run offramp health --from out/acme/extract --fail-on error

# Annotate an existing scan without org calls (LLM_API_KEY); --processes adds cluster narratives
uv run offramp annotate --from out/acme/extract --processes --dump-context out/dossiers

# Watch an org over time
uv run offramp sync --org acme_prod --library out/library --interval 3600
uv run offramp changes --library out/library --org acme_prod --last 5

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
- **Anthropic Claude Sonnet 5** for the annotation pass (`LLM_API_KEY`; a workspace-scoped key, or `LLM_WORKSPACE_ID` for an organization-level key; provider-routable)
- **simple-salesforce** for REST, Tooling API, and describe; **sf CLI** for Metadata API retrieves; **gRPC + fastavro** for Pub/Sub CDC
- **Own parsers** (AD-31): Apex analysis in `src/offramp/extract/apex` behind one contract with two engines — Salesforce's ANTLR grammar through the Node package `@apexdevtools/apex-parser` (`tools/apex-parser`, default) and a tokenizer fallback — plus a Flow XML/JSON normalizer, a recursive-descent formula parser, and regex LWC/Aura classifiers. No summit-ast, no Salto.
- **FalkorDB** (Cypher) for interactive graph exploration (optional; `make falkordb` runs it without Docker; `networkx` in memory otherwise)
- **Postgres 16** (asyncpg) for app + shadow stores (year two)
- **Engram** (internal) for provenance; **F44** for Base L2 Merkle anchoring of sensitive decisions

## Repo layout

```
src/offramp/
├── core/            shared models (Component, Dependency, SchemaNode, EvidenceChannel), secrets, logging, config
├── extract/         C1–C4, C19–C21
│   ├── pull/        source_tree (C19), tooling_api (REST path), sf_cli, fixture, reconciler
│   ├── apex/        C20 → ApexAnalysis: ast_bridge + ast_analyzer (grammar engine), references (tokenizer fallback)
│   ├── categories/  one extractor per category (flow, apex_class, approval_process, rules, …)
│   ├── schema.py    C21 SchemaSnapshot from source tree or describe
│   ├── dispatch/    C2 CMT-driven trigger dispatch, framework detectors
│   ├── lwc/, aura/  C3 bundle classifiers + Apex/schema imports, message channels
│   ├── data_profile.py  record counts + fill rates
│   └── ooe_audit/   C4 Order-of-Execution surface audit
├── understand/      C5–C6, C22–C23
│   ├── dependencies.py  C22 typed graph, evidence + confidence, Dependency-API cross-check
│   ├── impact.py        C23 where-used, change closure, save impact, unused, legacy
│   ├── clustering.py    Louvain / Leiden business-process clusters
│   ├── graph_loader.py  optional FalkorDB materialization
│   ├── orphan/          6-channel orphan resolver (graph-aware)
│   ├── process_ir.py    every automation → platform-neutral ProcessDefinition
│   ├── health.py        static health checks
│   ├── compare.py, changes.py   repo-vs-org diff (C25), change log across scans (D.6)
│   ├── annotate.py, annotate_context.py, tier_rules.py   LLM annotations from dossiers, earned confidence
│   └── xray/            report renderer (HTML + JSON schema 2.0)
├── knowledge/       C24: content-addressed process library, Markdown/Mermaid/Flow-XML rendering, FalkorDB mirror
├── verify/          C26: debug-log trace parser, trace-vs-definition comparison, org runner, generated recipes
├── mcp/             C12: gateway (query, tooling_query, describe, restful, mdapi), quota allocator, JWT / sf CLI auth
├── cli/             extract, xray, impact, kg, compare, verify, recipes, health, annotate, sync, changes (+ year two: generate, shadow, cutover)
├── generate/        C7–C9 (year two): translators, formula parser + emitter, adapters
├── runtime/         C10–C11 (year two): OoE state machine, rules engine
├── validate/        C13–C15 (year two): shadow executor, Compare Mode, reconciliation
├── cutover/         C16 (year two)
├── engram/          C17: provenance client
└── event_bus/       C18: pluggable bus

templates/           xray.html.j2 (report), shadow_dashboard, parity_report
tools/apex-parser/   Node wrapper around Salesforce's ANTLR Apex grammar (apex_ast.js); `make apex-parser` installs it
scripts/             scratch_org.sh (deploy the fixture + scan a real org), verify_xray.py, verify_extract_coverage.py

tests/
├── unit/            371 unit tests (no external services; the Apex analyzer suite runs on both engines)
├── integration/     22 tests; fixture-driven ones run anywhere, FalkorDB / Postgres ones self-skip
├── integration/fixtures/sample_org/   deployable fixture org: 12 Apex classes, 2 triggers, 7 Flows, LWC + Aura, rules, approval, schema, tooling dumps
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

Everything on the X-Ray path runs with no services: `make gate` needs only `uv` (and Node for the grammar-backed Apex engine; without it the tokenizer runs). FalkorDB and Postgres are optional and only used by the tests that name them (they self-skip when unreachable).

```bash
make falkordb                 # FalkorDB without Docker (brew Redis + module), or:
docker run -d --name offramp-falkordb -p 6379:6379 falkordb/falkordb

# Postgres for app state + shadow store (year two)
docker run -d --name offramp-postgres -p 5432:5432 \
  -e POSTGRES_USER=offramp -e POSTGRES_PASSWORD=offramp -e POSTGRES_DB=offramp \
  postgres:16-alpine
docker exec offramp-postgres psql -U offramp -d offramp -c "CREATE DATABASE offramp_shadow;"
```

For a live org, copy `.env.example` → `.env` (every key is documented there; keep comments on their own lines). Two auth modes: `SF_AUTH_MODE=jwt` with a Connected App consumer key, integration username and JWT private key path (see the [scratch-org runbook](docs/runbooks/connect_scratch_org.md)), or `SF_AUTH_MODE=sf-cli` to reuse the session of `sf org login web --alias <alias>` for local runs. For annotations set `LLM_API_KEY`. `.env` is gitignored and is loaded into the environment by the CLI.

The `sf` CLI is needed for `--via sf-cli` retrieves and for `--auth sf-cli`; the default REST/Tooling path with JWT needs nothing installed on the customer side. A Developer Edition allows 15,000 API requests per day and one scan costs about 360; the scan reads `/limits` first and refuses when the budget is short.

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
