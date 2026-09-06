# Build Plan v0.2 — X-Ray First

**Version:** 0.2 (supersedes v0.1, archived at [build-plan-v0.1-archived.md](build-plan-v0.1-archived.md))
**Date:** 2026-09-05
**Strategy source:** [strategy.md](strategy.md) (market study, 2026-09-05)
**Architecture:** [architecture.md](architecture.md); component numbers **Cn** unchanged, new components **C19–C23** added below.

## Why the plan changed

The v0.1 plan spread 34 weeks across three products. The result was 12K lines of Python covering six phases thinly: no working connection to a real org, no Apex source in the pipeline, 7 of ~20 Flow element types parsed, 9 of 21 categories left as XML passthrough, and a dependency graph with two edge types.

The market study found that the one capability every paid vendor is weak on, and no free tool offers, is a **complete, trustworthy, cross-type dependency graph with execution semantics**. Salesforce's own Dependency API has been beta for eight years and cannot produce one. Sweep, the category leader, was acquired by ServiceNow on 2026-09-03, which puts its Salesforce roadmap in doubt.

So v0.2 narrows to **one product for twelve months: X-Ray**, sold per org with a free scan, connected by OAuth as an external SaaS. Translation (Agent Factory) and validation (Shadow Mode) are retained in the codebase but move to year two.

## Product definition: X-Ray

X-Ray answers five questions about a Salesforce org, with a source and a confidence on every answer:

1. **What is here?** Complete inventory of the 21 automation categories plus the data model (objects, fields, relationships, record types, picklists).
2. **What depends on what?** A typed dependency graph across code, declarative automation, formulas, schema, and UI, built from our own parsers and cross-checked against the Dependency API.
3. **Where is this used?** For any field, object, class, or Flow: every inbound reference, grouped by category, with the evidence that produced the edge.
4. **What happens when this changes?** Impact analysis: the downstream closure of a change, and the automations that fire on a save to a given object in Order-of-Execution sequence.
5. **What can go?** Unused and unreferenced metadata, legacy Workflow Rules and Process Builders with their Flow-migration blast radius, and orphaned Apex with the channel that proves it is or is not dead.

Non-goals for the twelve-month window: generating replacement code, shadow execution, cutover routing, multi-platform (HubSpot etc.).

## New components

| # | Component | Purpose | Module |
|---|---|---|---|
| C19 | Source tree reader | One reader for sf-retrieve-shaped directories (fixtures, sf CLI output, customer-supplied SFDX projects). Reads `.cls`/`.trigger` bodies, all field types, objects, record types. | `src/extract/pull/source_tree.py` |
| C20 | Apex reference extractor | Tokenizer-based static analysis of Apex: class graph, SOQL/DML targets, callouts, annotations, framework interfaces, trigger context. Designed so a full parser can replace the tokenizer without changing the output contract. | `src/extract/apex/` |
| C21 | Schema extractor | Objects, fields, relationships, record types, picklists from source tree and/or REST describe. Output is `SchemaSnapshot`. | `src/extract/schema.py` |
| C22 | Dependency graph builder | Typed edges with evidence channel and confidence from every extractor, merged with Dependency-API rows as a cross-check. | `src/understand/dependencies.py` |
| C23 | Impact analysis | "Where is this used", downstream closure, OoE-ordered save impact, unused-metadata detection. | `src/understand/impact.py` |

## Architecture decision deltas (AD-27 .. AD-31)

| ID | Decision | Rationale |
|---|---|---|
| AD-27 | **X-Ray first.** Generate, validate, and cutover packages stay in-tree but are not on the twelve-month roadmap. | Buyer evidence exists for org intelligence; none yet for off-platform translation. |
| AD-28 | **Own parsers, API as cross-check.** Every dependency edge is produced by our extractors. `MetadataComponentDependency` rows are ingested as a second opinion that raises confidence where they agree and are surfaced as "API-only" edges where they do not. | The API is beta, capped, unfilterable by name, and omits reports. Vendors that lean on it (Elements, Panaya) inherit its gaps. |
| AD-29 | **Two extraction paths, REST first.** Tooling/REST via the MCP gateway is the primary path (no CLI dependency on the customer side). sf CLI retrieve is the secondary path for customers who supply an SFDX project or want a full metadata ZIP. Both feed C19. | Sweep-style SaaS onboarding is an OAuth connect, not a CLI install. |
| AD-30 | **Per-edge provenance is a product feature.** Engram anchoring already records it; the X-Ray report exposes evidence channel and confidence to the customer. | "Shows its work" is the positioning against Sweep's black box. |
| AD-31 | **No summit-ast.** The tokenizer in C20 is the year-one Apex analyzer; a grammar-backed parser (apex-parser via JVM, or tree-sitter-sfapex) is a year-two upgrade behind the same `ApexAnalysis` contract. | summit-ast is archived; a JVM/Bazel toolchain in a Python product is a cost we do not need to pay to ship year one. |

## Phases

Complexity legend: **S** ≤ 1 day, **M** 2–5 days, **L** ≥ 1 week. Sequence follows dependency; nothing downstream is credible until the graph is real.

### Phase A — Real extraction (months 1–2)

| # | Task | Files | Cx |
|---|---|---|---|
| A.1 | C19 source tree reader; `FixturePullClient` becomes a thin wrapper. Reads `.cls`/`.trigger` bodies, every `*.field-meta.xml`, `*.object-meta.xml`, record types. | `extract/pull/source_tree.py`, `extract/pull/fixture.py` | M |
| A.2 | Fix categories/LWC circular import; registry resolves lazily. | `extract/categories/base.py`, `_passthrough.py` | S |
| A.3 | Backend protocol gains `tooling_query`, `describe_global`, `describe`, `restful`; in-memory + simple-salesforce implementations. | `mcp/server.py`, `mcp/sf_backend.py` | M |
| A.4 | C1 Tooling/REST pull client: ApexClass/ApexTrigger bodies, Flow metadata (active versions via FlowDefinition), ValidationRule, WorkflowRule, CustomField, CustomObject, ApprovalProcess where exposed; MetadataComponentDependency chunked by type; CronTrigger; CMT rows. Emits the same payload contract as C19. | `extract/pull/tooling_api.py` | L |
| A.5 | C1 sf CLI pull client: generated `package.xml`, `sf project retrieve start`, then C19 over the output. Runner injected for tests. | `extract/pull/sf_cli.py` | M |
| A.6 | C21 schema extractor from source tree and from describe. | `extract/schema.py`, `core/models.py` (`SchemaSnapshot`) | M |
| A.7 | CLI `--org` wired for `extract` and `xray`; `--source-dir` for SFDX projects; `--via sf-cli`. | `cli/extract.py`, `cli/xray.py` | S |
| A.8 | Richer fixture org: realistic Apex classes with SOQL/DML/callouts, a trigger with a handler, Flows with decisions/assignments/loops/lookups/action calls, an approval process with steps. | `tests/integration/fixtures/sample_org/` | M |

| A.9 | Metadata API retrieve through simple-salesforce (`mdapi`), no CLI on either side; default REST path retrieves only the types Tooling cannot read in full (approval, assignment/escalation/auto-response/sharing rules, reports). `CompositePullClient` + reconciler body precedence. | `extract/pull/mdapi.py`, `pull/reconciler.py` | M |
| A.10 | Tooling client hardening from review: `Metadata` fetched per record, CronTrigger / AsyncApexJob / ProcessDefinition via REST, custom-object Ids mapped to names, CDC channel query fixed. | `extract/pull/tooling_api.py` | M |

**Gate A:** `make test` green; `offramp extract --fixture … --out out/fx` produces components for 21 categories with Apex bodies present and `schema.json` written; `offramp extract --org <alias>` against a scratch org produces ≥ 1 component per exercised category with zero `NotImplementedError` paths remaining in `extract/pull`.

### Phase B — Understanding the code and the declarative surface (months 2–4)

| # | Task | Files | Cx |
|---|---|---|---|
| B.1 | C20 Apex tokenizer: strip comments/strings, tokens, class/interface/trigger headers, `extends`/`implements`, annotations. | `extract/apex/tokenizer.py` | M |
| B.2 | C20 reference extractor: class references (static calls, `new`, type usages), SOQL `FROM`/fields/relationship paths, DML statements and `Database.*`, callouts, `System.schedule`/`enqueueJob`/`executeBatch`, `Schema.SObjectType` refs, entry-point annotations, framework interfaces. Output `ApexAnalysis`. | `extract/apex/references.py`, `extract/apex/model.py` | L |
| B.3 | Apex Class and Apex Trigger extractors use C20; trigger handler resolution via body + CMT dispatch. | `extract/categories/apex_class.py`, `apex_trigger.py` | M |
| B.4 | Full Flow element coverage: decisions (rules, conditions, operators), assignments, loops, lookups/creates/updates/deletes with object + filters + fields, waits, subflows, action calls by type, formulas, variables, screens, start entry criteria. Derived `references`. Existing keys preserved for Tier 1 translators. | `extract/categories/flow.py` | L |
| B.5 | Formula reference extraction (fields, relationship paths, `$` globals) using the formula parser with a tolerant fallback. Parser breadth: `ISNEW`, `ISCHANGED`, `PRIORVALUE`, `INCLUDES`, `REGEX`, date parts, `$User`/`$Profile`/`$Setup`/`$Label`. | `generate/formula/parser.py`, `generate/formula/references.py` | M |
| B.6 | Normalize the passthrough categories: Approval Process (entry criteria, steps, approvers, actions), Sharing Rules (criteria), Escalation, Auto-Response, Roll-Up Summary, Platform Event, CDC. Each emits `references`. | `extract/categories/*.py` | L |
| B.7 | LWC: keep the regex classifier; add `lightning/ui*Api` object/field references and `@salesforce/schema` imports. | `extract/lwc/bundle.py` | S |
| B.8 | Surface categories: page layouts, Lightning pages, permission sets, profiles, reports (`CategoryName` 22–26, never on the save path). UI / security / reporting references feed where-used and the unused-field verdict. | `extract/categories/surfaces.py` | M |
| B.9 | Dynamic-access flag per Apex class (`Database.query` with built strings, `Type.forName(var)`, `sObject.get(var)`, `getGlobalDescribe`); lowers parser-edge confidence and shows in the report. Literal `so.get('Field__c')` resolves. | `extract/apex/references.py` | S |

**Gate B:** every fixture Apex class yields ≥ 1 reference; `LeadDispatcher` resolves to its handlers; every fixture Flow yields object + field references; no category is `passthrough=True`; formula parser handles the fixture corpus plus a 40-case reference test.

### Phase C — The graph (months 3–5)

| # | Task | Files | Cx |
|---|---|---|---|
| C.1 | `Dependency` model gains `evidence` (channel) and `notes`; new `SchemaNode` for objects/fields. | `core/models.py` | S |
| C.2 | C22 builder: edges from Apex, Flow, formula, workflow, validation, assignment/escalation/auto-response, approval, sharing, rollup, LWC, CMT dispatch, schema relationships. | `understand/dependencies.py` | L |
| C.3 | Dependency-API cross-check: match rows to our edges by (type, name); agreement raises confidence; API-only edges are added at 0.6 with evidence `dependency_api`. Coverage report lists both directions of disagreement. | `understand/dependencies.py`, `extract/audit.py` | M |
| C.4 | FalkorDB loader writes Object/Field nodes and every edge kind; clustering uses the full graph; Leiden via `leidenalg`/`igraph` behind a feature flag, Louvain default. | `understand/graph_loader.py`, `understand/clustering.py` | M |
| C.5 | Orphan resolver uses Apex class references and CronTrigger rows; channel list unchanged. | `understand/orphan/resolver.py` | S |
| C.6 | Per-org coverage report: edges by evidence channel, unresolved references, API disagreement. | `extract/audit.py`, `understand/xray/render.py` | M |

**Gate C:** fixture graph has ≥ 8 edge kinds; every edge carries evidence and confidence; `LeadValidationHandler` is not an orphan; disagreement report renders.

### Phase D — X-Ray as a product (months 4–7)

| # | Task | Files | Cx |
|---|---|---|---|
| D.1 | C23 "Where is this used": inbound references for any node, grouped by category with evidence. | `understand/impact.py` | M |
| D.2 | C23 impact closure and OoE-ordered save impact for an object, driven by `ooe_audit` step mapping. | `understand/impact.py` | M |
| D.3 | Unused-metadata detection: fields with no automation, UI, or code references; inactive automation; legacy WFR/PB with migration blast radius. | `understand/impact.py` | M |
| D.4 | X-Ray HTML: dependency table with evidence, "Where is this used" explorer, save-impact view, unused list, schema summary. JSON export versioned `2.0`. | `templates/xray.html.j2`, `understand/xray/render.py` | L |
| D.5 | CLI: `offramp impact --node <Object.Field>`; `offramp xray` runs without FalkorDB when `--no-graph-db` (networkx only). | `cli/impact.py`, `cli/xray.py` | M |
| D.8 | Data profile: `limits/recordCount` + one aggregate `COUNT(field)` query per object (chunked) → record counts and fill rates on graph nodes; `--no-data-profile` to skip. Unused fields carry fill rate and "never populated". | `extract/data_profile.py` | M |
| D.10 | Knowledge library (C24, AD-32): `ProcessDefinition` model + per-category builders (Flows full, rules full, approvals full, Apex references-only with source attached); file-backed content-addressed store with scan history, diff, families, search; `offramp kg ingest/list/search/show/families/scans/diff/export`; `--library` on extract/xray; Mermaid + Markdown rendering; persistent FalkorDB mirror. | `core/process.py`, `understand/process_ir.py`, `knowledge/` | L |
| D.9 | Usage semantics: only active, non-test automation counts as usage; where-used reports `automation_total` / `active_total` / test-only / inactive-only; unused-field reasons `no_references`, `test_only`, `inactive_only`, `security_only`, `ui_only`, `reporting_only`. | `understand/impact.py` | S |
| D.6 | Continuous sync: scheduled re-extract, content-hash diff, change log per component. | `extract/changes.py` | L |
| D.7 | Hosted service: FastAPI app exposing connect (OAuth), scan, report, impact; multi-tenant by org (AD-10 single-tenant data plane per customer). | `service/` | L |

**Gate D:** free-scan flow works end-to-end against a scratch org from OAuth connect to rendered report; impact query answers in < 2 s on a 5K-component org.

### Phase E — Execution-order impact and safe cleanup (months 6–12)

E.1 finish OoE runtime steps that real orgs exercise (`runtime/ooe`); E.2 surface "what fires, in what order" in the impact view; E.3 cleanup execution with deploy-backed rollback (Tooling API deactivate, Metadata API deploy); E.4 AppExchange connector listing after revenue.

## Known limitations (tracked, not yet scheduled)

- Apex analysis is tokenizer-based: class properties are not typed, `is_sobject_name` uses a fixed standard-object list plus suffix rules, and inner-class references (`Outer.Inner`) resolve only when the outer class is in the corpus. A grammar-backed parser (AD-31) is the fix.
- `MetadataComponentDependency` rows with no parser evidence become `dependency_api` edges at 0.6 confidence so the report can show them; they are excluded from "live automation" counts but do appear in totals.
- The Tooling path reads reports through the Analytics describe endpoint, capped at the 300 most recently run; the Metadata API path has no cap.
- Aggregate `COUNT(field)` is not allowed on long text, rich text, encrypted, multi-select, and compound fields, so those fields have no fill rate.

## Deferred (year two)

Phases 3–5 of v0.1 (translators, shadow, cutover) remain in `src/generate`, `src/validate`, `src/cutover` with their tests. They are not deleted and not extended until X-Ray has paying customers.
