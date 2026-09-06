# Build Plan: Salesforce Off-Ramp

**Version:** 0.1 (engineering execution plan)
**Date:** 2026-04-16
**Maps to:** [Salesforce-OffRamp-Build-Plan-v2.1.docx](../Salesforce-OffRamp-Build-Plan-v2.1.docx) milestones M0–M13.
**Architecture:** [architecture.md](architecture.md). Components referenced as **Cn**.

This is the code-level companion to v2.1: every component (C1–C18) is sequenced into a gated phase with concrete tasks, files, and a runnable test gate. Phases are scoped to what can be merged into `main` and verified independently. **Do not advance a phase until its gate passes.**

Complexity legend: **S** = ≤ 1 day, **M** = 2–5 days, **L** = ≥ 1 week.

---

## Phase 0 — Scaffold & Foundation

**Maps to v2.1 M0 (week 4).** Goal: an engineer can clone the repo, run `make dev`, and execute an end-to-end smoke test against a SF scratch org.

### Tasks

| # | Task | Files | Complexity |
|---|---|---|---|
| 0.1 | Initialize uv-managed Python 3.12 project; add ruff (format+lint), mypy strict, pytest, pytest-asyncio | `pyproject.toml`, `.python-version`, `ruff.toml` | S |
| 0.2 | Repo skeleton matching architecture §8; `__init__.py` per package | all `src/*/` directories | S |
| 0.3 | Makefile: `dev`, `test`, `lint`, `typecheck`, `smoke`, `clean` | `Makefile` | S |
| 0.4 | Pre-commit hooks: ruff format, ruff check, mypy, gitleaks; custom hook rejecting translation-matrix changes without fixture updates | `.pre-commit-config.yaml`, `scripts/check_matrix_fixtures.py` | M |
| 0.5 | GitHub Actions CI: lint + typecheck + unit + integration (scratch-org-recorded), Trivy + gitleaks scan, container build | `.github/workflows/ci.yml` | M |
| 0.6 | `.env.example` with every variable from architecture §5; secrets-loader utility reading from Key Vault / Secrets Manager | `.env.example`, `src/core/secrets.py` | S |
| 0.7 | Pydantic shared models stub: `Component`, `Dependency`, `AST`, `TranslationArtifact`, `ShadowComparison` | `src/core/models.py`, `tests/unit/test_models.py` | M |
| 0.8 | Engram client stub (HTTP wrapper, no real backend yet); records anchor calls to a local Postgres for tests | `src/engram/client.py`, `tests/unit/test_engram_stub.py` | M |
| 0.9 | Event bus abstraction with Redis Streams impl for dev | `src/event_bus/__init__.py`, `src/event_bus/redis_streams.py` | M |
| 0.10 | Smoke test: connect to a Developer Edition scratch org via JWT, fetch one Account record, round-trip through stub MCP gateway | `tests/integration/test_smoke.py`, `src/mcp/server.py` (skeleton) | M |
| 0.11 | **[AD-25]** JWT cert rotation runbook + automated rotation test against sandbox | `docs/runbooks/jwt_cert_rotation.md`, `tests/integration/test_cert_rotation.py` | M |
| 0.12 | Terraform modules for AKS namespace per customer; helm chart skeleton for one service | `infra/terraform/`, `infra/helm/offramp/` | L |

### Phase 0 Gate

```bash
make dev && make lint && make typecheck && make test && make smoke
```

Must pass green. The `smoke` target retrieves one Flow + one Account from a Developer Edition scratch org and confirms the Engram stub recorded both anchors.

---

## Phase 1 — Extract Engine

**Maps to v2.1 M1 (week 8) + M2 (week 10).** Goal: every one of the 21 SF automation categories has an extractor with documented coverage. OoE Surface Audit operational.

### Tasks

| # | Task | Files | Complexity |
|---|---|---|---|
| 1.1 | C1: Salto wrapper — shell-out to `salto fetch`, parse NaCl, map to canonical Component | `src/extract/pull/salto.py`, `tests/unit/test_salto_parser.py` | M |
| 1.2 | C1: sf CLI wrapper — dynamic `package.xml`, bulk retrieve all 21 types | `src/extract/pull/sf_cli.py`, `tests/unit/test_sf_cli_wrapper.py` | M |
| 1.3 | C1: Tooling API client — `MetadataComponentDependency`, `FlowVersionView`, `CronTrigger`, CMT records | `src/extract/pull/tooling_api.py` | M |
| 1.4 | C1: Reconciler with documented precedence + disagreement logging | `src/extract/pull/reconciler.py`, `tests/unit/test_reconciler.py` | M |
| 1.5 | Per-category extractors (21 of them) — see architecture §C1; each emits Component + content hash + Engram anchor | `src/extract/categories/*.py`, `tests/unit/test_category_*.py` | L |
| 1.6 | C2: Dynamic dispatch resolver — CMT reader, class resolver with confidence scores, framework detectors (Kevin O'Hara, fflib, Trigger Actions Framework) | `src/extract/dispatch/{cmt_reader,class_resolver,framework_detectors}.py` | L |
| 1.7 | C3: LWC analyzer — bundle retriever, tree-sitter JS parser, classifier (ui_only/mixed/business_logic_heavy), Apex linker | `src/extract/lwc/*.py` | L |
| 1.8 | C4: OoE Surface Audit — per-component step classifier, frequency counter (EventLogFile-driven), report generator | `src/extract/ooe_audit/*.py`, `tests/unit/test_ooe_step_classifier.py` | M |
| 1.9 | Coverage audit — per-category counts, unresolved references, suspected gaps | `src/extract/audit.py` | M |
| 1.10 | CLI: `offramp extract --org <alias> --out <dir>` orchestrating C1–C4 + audit | `src/cli/extract.py`, `tests/integration/test_extract_e2e.py` | M |

### Phase 1 Gate

```bash
make test && \
  uv run offramp extract --org dev_scratch --out out/dev_scratch && \
  uv run python scripts/verify_extract_coverage.py out/dev_scratch --min-categories 21
```

Against a seeded scratch org with at least one component per category, all 21 must extract, the OoE Surface Audit must produce a 21-row report, and coverage must be ≥95% per category.

---

## Phase 2 — Understanding Engine + X-Ray Report

**Maps to v2.1 M3 (week 14) + M4 (week 16) + M5 (week 18).** Goal: ship the X-Ray product — the first revenue-generating deliverable.

### Tasks

| # | Task | Files | Complexity |
|---|---|---|---|
| 2.1 | FalkorDB loader — Component records → typed nodes + dependency edges | `src/understand/graph_loader.py`, `tests/integration/test_falkordb_load.py` | M |
| 2.2 | Leiden clustering with tunable resolution → BusinessProcess nodes | `src/understand/clustering.py`, `tests/unit/test_clustering.py` | M |
| 2.3 | LLM annotation pass with provenance-aware prompt harness; Engram-anchored | `src/understand/annotate.py`, `tests/unit/test_prompt_harness.py` | L |
| 2.4 | Complexity scoring (translation difficulty + migration risk per component) | `src/understand/complexity.py`, `tests/unit/test_complexity.py` | M |
| 2.5 | C6: Orphan resolver — six channels (LWC imports, Connected Apps, Named Credentials, CronTrigger, EventLogFile, integration-partner docs) | `src/understand/orphan/*.py` | L |
| 2.6 | X-Ray HTML report generator (D3 force-directed graph + heatmap + inventory tables) | `src/understand/xray/render_html.py`, `templates/xray.html.j2` | L |
| 2.7 | X-Ray PDF executive summary | `src/understand/xray/render_pdf.py` | M |
| 2.8 | X-Ray JSON export schema | `src/understand/xray/schema.py`, `tests/unit/test_xray_schema.py` | S |
| 2.9 | CLI: `offramp xray --org <alias> --out <dir>` | `src/cli/xray.py`, `tests/integration/test_xray_e2e.py` | M |

### Phase 2 Gate

```bash
make test && \
  uv run offramp xray --org dev_scratch --out out/dev_scratch/xray && \
  uv run python scripts/verify_xray.py out/dev_scratch/xray
```

Verifier asserts: HTML+PDF+JSON all present, graph has ≥1 BusinessProcess cluster, every component has annotation + complexity score, every annotation Engram-anchored.

---

## Phase 3 — OoE Runtime + Tier Translators + MCP Gateway

**Maps to v2.1 M6 (week 20) + M7 (week 24) + M8 (week 26).** Goal: end-to-end translation of one real process (Fisher lead routing) deployed and runnable.

### Tasks

| # | Task | Files | Complexity |
|---|---|---|---|
| 3.1 | C10: OoE runtime state machine — 21 steps, transaction context, commit/rollback | `src/runtime/ooe/{state_machine,transaction}.py` | L |
| 3.2 | C10: Re-fire cycle (step 12 → re-execute steps 5+9 once, flag clearing) | `src/runtime/ooe/refire.py` | L |
| 3.3 | C10: Cascade tracking — depth-bounded recursion, once-per-entity-per-transaction Flow rule | `src/runtime/ooe/cascade.py` | M |
| 3.4 | **[AD-23]** OoE test suite — 200+ cases including mixed-DML setup/non-setup boundaries | `tests/ooe_runtime/test_*.py` | L |
| 3.5 | C11: Tier 1 rules engine — `evaluate(record, context) -> RuleResult` uniform signature | `src/runtime/rules/engine.py`, `tests/unit/test_rules_engine.py` | M |
| 3.6 | C7: Tier 1 translator — validation rules, before-save flows, simple after-save flows | `src/generate/tier1.py`, `tests/unit/test_tier1_translation.py` | L |
| 3.7 | C8: Deterministic formula parser + audit harness (50–200 test cases per formula, NO LLM fallback) | `src/generate/formula/{parser,emitter,audit}.py`, `tests/unit/test_formula_parser.py` | L |
| 3.8 | C7: Tier 2 translator — approval processes, batch jobs, scheduled flows, escalation rules → Temporal workflows | `src/generate/tier2.py`, `tests/unit/test_tier2_translation.py` | L |
| 3.9 | C7: Tier 3 translator — LangGraph StateGraph emission for judgment-required components | `src/generate/tier3.py`, `tests/unit/test_tier3_translation.py` | L |
| 3.10 | Dual-target generation for Tier 1/Tier 2 boundary | `src/generate/dual_target.py` | M |
| 3.11 | C9: Managed Package Adapter generator — auto-adapter for any global Apex; hand-tuned library entries for CPQ, Conga, DocuSign, Marketing Cloud Connect, Pardot | `src/generate/adapters/{detector,mcp_emitter}.py`, `src/generate/adapters/hand_tuned/*.py` | L |
| 3.12 | C12: MCP gateway — FastAPI + MCP server SDK exposing `sf_query`, `sf_bulk_query`, `sf_create`, `sf_update`, `sf_delete`, `sf_describe`, `sf_cdc_subscribe`, `sf_publish_event` | `src/mcp/{server,tools}.py`, `tests/integration/test_mcp_tools.py` | L |
| 3.13 | **[AD-24]** MCP gateway: `/limits` poller + per-process API quota allocation + utilization metrics | `src/mcp/quota.py`, `tests/unit/test_quota_allocator.py` | M |
| 3.14 | MCP gateway: every call Engram-anchored with calling component identity | `src/mcp/anchoring.py` | S |
| 3.15 | End-to-end translation of Fisher lead routing process — generated artifact deploys to a sandbox Temporal cluster | `examples/fisher_lead_routing/`, `tests/integration/test_e2e_lead_routing.py` | M |

### Phase 3 Gate

```bash
make test && \
  uv run pytest tests/ooe_runtime -v && \
  uv run offramp generate --process lead_routing --out artifacts/lead_routing && \
  uv run offramp deploy --artifact artifacts/lead_routing --target sandbox && \
  uv run pytest tests/integration/test_e2e_lead_routing.py
```

OoE runtime test suite must hit 100% pass; lead routing artifact must execute one synthetic Lead through the OoE runtime end-to-end with all decisions Engram-anchored.

---

## Phase 4 — Validation Engine (Shadow Mode + Compare Mode)

**Maps to v2.1 M9 (week 28) + M10 (week 30) + M11 (week 32).** Goal: ship Shadow Mode as a standalone product; readiness scoring gates cutover.

### Tasks

| # | Task | Files | Complexity |
|---|---|---|---|
| 4.1 | C13: Pub/Sub gRPC subscriber for `/data/<Object>ChangeEvents` with Avro decoding | `src/validate/shadow/subscriber.py`, `tests/integration/test_pubsub_subscriber.py` | L |
| 4.2 | Forked data environment: read-through to prod, write-intercept to shadow Postgres, intra-transaction read-back | `src/validate/shadow/data_env.py`, `tests/unit/test_data_env.py` | L |
| 4.3 | Shadow executor: reconstructs triggering transaction, runs translated artifact via OoE runtime, records trace | `src/validate/shadow/executor.py` | L |
| 4.4 | Field-level diff vs. next CDC event from production | `src/validate/shadow/diff.py`, `tests/unit/test_field_diff.py` | M |
| 4.5 | Divergence categorization: 7 categories (translation_error, ooe_ordering, governor_limit, non_deterministic_ordering, formula_edge_case, env_artifact, **[AD-22] gap_event_full_refetch_required**) | `src/validate/shadow/categorize.py`, `tests/unit/test_categorize.py` | M |
| 4.6 | **[AD-21]** C15 reconciliation: subscriber lag monitor (>60h alert), gap event handler, full-record re-fetch via REST, replay-id reset | `src/validate/reconcile/{lag_monitor,gap_handler,resync}.py`, `tests/integration/test_reconcile_72h_cliff.py` | L |
| 4.7 | Readiness scoring: 30-day rolling window, severity-weighted, edge-case coverage, confidence intervals; ≥98 for 14 days = cutover-eligible | `src/validate/shadow/readiness.py`, `tests/unit/test_readiness.py` | M |
| 4.8 | Divergence dashboard — interactive HTML rendered from shadow Postgres | `src/validate/shadow/dashboard.py` | M |
| 4.9 | Compliance report export (Engram + F44 anchored proof of every comparison) | `src/validate/shadow/compliance_export.py` | M |
| 4.10 | C14: Compare Mode — SF debug log parser, state reconstructor, replay harness sharing categorization with C13 | `src/validate/compare_mode/*.py`, `tests/integration/test_compare_mode.py` | L |
| 4.11 | CLI: `offramp shadow start --process <id>`, `offramp shadow status`, `offramp shadow report` | `src/cli/shadow.py` | M |

### Phase 4 Gate

```bash
make test && \
  uv run pytest tests/integration/test_reconcile_72h_cliff.py -v && \
  uv run pytest tests/integration/test_compare_mode.py -v && \
  uv run offramp shadow start --process lead_routing --duration 5m --synthetic && \
  uv run offramp shadow report --process lead_routing --assert-readiness-emitted
```

Synthetic-CDC harness drives 1000 events through shadow execution; verifier asserts: divergence categorization populates all 7 categories under stress; gap-event simulation triggers C15 reconciliation; Compare Mode replays 30-day debug log and reports findings.

---

## Phase 5 — Cutover Orchestrator + Hardening

**Maps to v2.1 M12 (week 32) + M13 (week 34).** Goal: first production cutover ready; platform hardened for design-partner deployment.

### Tasks

| # | Task | Files | Complexity |
|---|---|---|---|
| 5.1 | C16: Hash-deterministic per-record traffic router; staged percentages (1/5/25/50/100) with dwell timers | `src/cutover/router.py`, `tests/unit/test_router_determinism.py` | M |
| 5.2 | Saga compensation framework: per-activity declared compensations; rollback executor | `src/cutover/saga.py`, `tests/unit/test_saga_compensation.py` | M |
| 5.3 | MCP gateway routing-config integration — single config change rolls back to 0% | `src/mcp/routing.py`, `tests/integration/test_instant_rollback.py` | M |
| 5.4 | Auto-advance / auto-rollback driven by readiness-score thresholds (≥98 advance, <95 rollback, <90 immediate-rollback-with-signoff) | `src/cutover/orchestrator.py`, `tests/integration/test_auto_rollback.py` | M |
| 5.5 | Engram + F44 anchoring of every routing decision and stage transition | `src/cutover/provenance.py` | S |
| 5.6 | Behavioral Parity Report generator (4 categories: deliberate_simplifications, discovered_undocumented, platform_imposed_deviations, customer_requested_improvements) | `src/cutover/parity_report.py`, `templates/parity_report.html.j2` | M |
| 5.7 | Post-cutover continuous Shadow Mode (regression-detection role) | `src/cutover/post_cutover_monitor.py` | M |
| 5.8 | Helm chart for full deployment (MCP, runtimes, shadow subscriber, reconciler, observability sidecars) | `infra/helm/offramp/` | L |
| 5.9 | On-prem operator (secrets, CDC ingestion) for non-Azure customers | `infra/operator/` | L |
| 5.10 | Production runbooks: cutover advance, rollback, reconciliation, cert rotation, quota incident | `docs/runbooks/*.md` | M |
| 5.11 | Load test: 10K txn/hr MVP target; benchmark MCP p50 <50ms p99 <200ms, rules p50 <10ms p99 <50ms | `tests/load/`, `scripts/benchmark.py` | M |
| 5.12 | Final dry-run cutover against Fisher sandbox — full 1→100% advance with simulated rollback at each stage | `tests/integration/test_cutover_dry_run.py` | M |

### Phase 5 Gate

```bash
make test && \
  uv run pytest tests/load -v && \
  uv run pytest tests/integration/test_cutover_dry_run.py -v && \
  uv run python scripts/verify_load_targets.py --p50-mcp-ms 50 --p99-mcp-ms 200 --txn-per-hour 10000
```

Load benchmarks must hit targets; dry-run cutover must complete 1→5→25→50→100 with one simulated rollback per stage; Behavioral Parity Report renders against Fisher sandbox data.

---

## Cross-cutting Tracks (parallel to phases)

These tracks run alongside the phased work, owned by separate engineers per v2.1 §16:

| Track | Owner | Deliverable | Phase alignment |
|---|---|---|---|
| Engram + F44 | Rust/systems engineer | E1 (week 6) Rust core; E2 (week 12) prov API; F1 (week 14) Sepolia; F2 (week 18) mainnet | Integrates each phase |
| Salesforce Reference doc publication | Tech lead | Public artifact at week 6 | GTM enabler, not code |
| SF Runtime Specialist (contract) | External | OoE internals review weeks 14–24 | Phase 3 risk reduction |
| Charter X-Ray sales | Sales/PM | 4 contracts closed by week 12 | Revenue track |

---

## Aggregate

- **5 phases** + Phase 0 scaffold = **6 gated stages**
- **65 enumerated tasks**
- **12 explicit research-gap addresses** (AD-21 through AD-26)
- Maps cleanly onto v2.1 milestones M0–M13

## Recommended Next Step

Run `/build` to start Phase 0 (Scaffold & Foundation).
