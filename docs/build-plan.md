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
| AD-31 | **No summit-ast.** Apex analysis sits behind the `ApexAnalysis` contract with two engines: Salesforce's ANTLR grammar through the Node package `@apexdevtools/apex-parser` (default since 2026-09-11, no JVM) and the own tokenizer as the fallback when Node is absent or a file does not parse. | summit-ast is archived; the Node grammar package costs one `npm ci` and parses NPSP in nine seconds, so the year-two swap was pulled forward once the tokenizer's false edges were measured on real corpora. |

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
| D.6 | **Done 2026-09-10.** Continuous sync: `offramp sync` (once or `--interval`), per-component content-hash diff with reasons (activation, references gained/lost, code size), change log + last snapshot per org inside the library; every scan with `--library` records it; `offramp changes` reads it. | `understand/changes.py`, `cli/changes.py` | L |
| D.7 | Hosted service: FastAPI app exposing connect (OAuth), scan, report, impact; multi-tenant by org (AD-10 single-tenant data plane per customer). | `service/` | L |

**Gate D:** free-scan flow works end-to-end against a scratch org from OAuth connect to rendered report; impact query answers in < 2 s on a 5K-component org.

### Phase E — Execution-order impact and safe cleanup (months 6–12)

E.1 finish OoE runtime steps that real orgs exercise (`runtime/ooe`); E.2 surface "what fires, in what order" in the impact view; E.3 cleanup execution with deploy-backed rollback (Tooling API deactivate, Metadata API deploy); E.4 AppExchange connector listing after revenue.

## Gate A result (2026-09-06)

Run against a fresh Developer Edition (`offramp-scratch`, sf CLI session auth) with the fixture deployed via `scripts/scratch_org.sh deploy`: 651 components across 24 of 26 categories, 5,621 nodes, 13,272 edges over 13 evidence channels, both extraction paths (Tooling/REST and Metadata API) reconciled, zero failed categories, `scripts/verify_xray.py` OK. The two categories without data are expected: `process_builder` (new orgs cannot create processes) and `change_data_capture` (no channel members in a stock org).

The whole X-Ray flow was then driven on that scan: `offramp impact` (where-used, save impact in OoE order, change impact, unused, legacy) answers correctly for fields that describe hides; the HTML report renders all twelve sections (7.6 MB, of which 6.7 MB is the embedded graph — a scaling item for 5K-component orgs); `offramp kg ingest` of the org scan and the fixture scan collapses all 26 shared automations to one entry each, the only separate entries being the two that genuinely differ (auto-response sender email, Process Builder present only in the fixture); `kg search / families / scans / diff / export` work. Both FalkorDB mirrors were then loaded and queried with Cypher (`make falkordb`, no Docker): the X-Ray graph as `offramp-scratch` (5,960 nodes, 16,216 relationships) and the library as `offramp_knowledge`, where `MATCH (p:Process)-[:FOUND_IN]->(o:Org)` returns the 26 processes shared by both orgs. The run surfaced fifteen API behaviours, now CLAUDE.md pitfalls 14 and 17–23.

## Third-party code: Easy Spaces (2026-09-06)

Salesforce's Easy Spaces LWC sample app (108 components: 4 package dirs of Apex, LWC, Aura, flows, custom metadata, record pages, permission sets) was deployed into the same org with its sample data. First scan: 694 components, 37 unresolved references, Dependency API cross-check 27 matched / 60 API-only. Fixes driven by that code — case-insensitive Apex qualifiers, `Outer.Inner` types, platform types, metadata-relationship chains, custom-metadata layout naming, LWC composition via templates and `c/` imports, API-row suffix/direction/template handling, coverage double-counting, name lookup preferring data-model nodes — brought it to 692 components, 1 unresolved reference (an inner class used unqualified), 65 matched / 8 API-only. The 8 are Aura bundle rows (Aura is not an extraction category) plus one lookup-derived object reference. The library ingest adds Easy Spaces' 6 automations and places `MarketServices`/`CustomerServices`/`LeadController` in one shape family with our fixture code.

### Easy Spaces gap analysis (source scan vs org scan, both in FalkorDB)

Scanning the Easy Spaces source tree directly (`offramp xray --source-dir`) and diffing it against the org scan, per component, showed the two extraction paths agree on 272 of 282 edges; the 10 source-only edges are the custom-metadata relationship chain the org path cannot follow (pitfall 27). Gaps found and fixed: the source reader saw only one of four package directories and matched `.git/objects` as metadata (pitfall 26); flows that embed LWCs as screen components or launch component actions had no edge to them (now `lwc_bundles` references, with fidelity notes saying why a flow is partial); Lightning message channels were invisible (pitfall 28); custom metadata records in source format were not read.

Gaps from that analysis, all closed the same day:

1. **Aura bundles** are now a category (`aura_bundle`, `src/extract/aura`): controller class, Apex actions, child components and events, `startFlow`, message channels, record-form objects/fields, labels. Easy Spaces source scans with zero unresolved references; every earlier API-only cross-check row is now a parser edge.
2. **Custom metadata records** are `cmt_record` graph nodes: a change to `Contact.MailingCity` reaches `CustomerServices` through `Customer_Fields__mdt.Contact_Customer_Fields` at depth two; rows are read from the org (REST) and from source (`customMetadata/*.md-meta.xml`).
3. **Custom tabs, custom applications, path assistants** are surface categories with their own extractors and edges (tab → object/page/component, app → tabs/pages/objects, path → picklist field + key fields). Prompts, branding, content assets and themes remain out of scope (nothing depends on them).
4. **Inner classes used unqualified** resolve to the declaring class.
5. **Quota**: the scan reads `/limits` first and refuses when fewer requests remain than a scan needs (`--ignore-api-budget` overrides), and the Tooling client no longer fetches per-record Metadata for surfaces the Metadata API path already retrieves, which removes ~420 of ~600 calls per scan.

Still open: re-verifying all of the above against the org once its 24 h request window resets (the fixture now carries an Aura bundle, an event, two tabs, an app and a path assistant that have not been deployed yet).

## Validation (2026-09-06)

Three layers, added in the order a customer would ask for them:

1. **Differential** — `offramp compare` (C25, `understand/compare.py`): two extract outputs diffed component by component; parser edges in one only, process definitions whose canonical form differs, components in one only (`--a-is-subset` for a repo against the whole org). Easy Spaces source vs org: 272 agreeing edges, 10 source-only (the metadata-relationship chain), 0 org-only.
2. **Behavioural** — `offramp verify` (C26, `verify/`): drive the flow in the org under a debug trace and compare the visited elements, element kinds, transitions and DML with the definition. Offline mode takes a saved debug log. The comparer rejected the first hand-written trace because the path did not match the fixture flow's connectors, which is the point.
3. **Round-trip** — `offramp verify --roundtrip`: `knowledge/flow_xml.py` renders a definition back to Flow XML (`kg show --format flowxml`), the copy is deployed as `<Name>_rt` and must trace identically. Every full-fidelity fixture flow re-extracts to the same definition from its rendered XML.

**Live results (2026-09-10, Developer Edition, recipes in `config/verify_recipes.example.json`): 2 pass, 0 mismatch.** `SendWelcomeEmail` (autolaunched) and `LeadRouting` (record-triggered): every visited element known to the model, element kinds, transitions, the decision's evaluated rule and taken branch, the Apex action target, the DML, and the subflow's nested elements all match; both round-trip copies deployed from rendered XML and traced identically (2 and 5 elements). Getting there took four org/fixture findings, none a model defect: the Default Workflow User must be a user with a verified email (Setup → Process Automation Settings); an email alert addressed to the record owner has zero recipients when an assignment rule has given the record to a queue without an email address; an after-save flow calling Apex that makes a synchronous callout is rejected on every save (fixed with a Queueable); the fixture flow's entry value was not a real picklist value so the flow had never fired. And one real model defect that only execution could catch: a lookup's output variable (`outputReference`) was not in the `ProcessDefinition`, so the rendered copy stored the lookup automatically and passed a null record id to the email alert — the model, normaliser and renderer now carry it (and `assignRecordIdToReference`, and the record-variable form of updates), and the identity test asserts it. The parser's log format needed four corrections from real logs (CLAUDE.md pitfall 29). Scan cost after the surface skip: ~360 requests (was ~600); cross-check 76 matched / 1 API-only.

**Toward a pilot (2026-09-10):** D.6 is done; annotations are the last untested stage (never run: no `LLM_API_KEY`). Second sample org chosen: the free **Financial Services Cloud Developer Edition** ("FSC playground", developer.salesforce.com/promotions/orgs/fscplayground) — a real financial-institution org with the managed package, sample data and its flows; managed Apex bodies are hidden, so it also exercises the references-only path at scale.

## Big real corpora (2026-09-10)

Instead of a financial-services org (the FSC Developer Edition signup rejects an email that already owns a Developer Edition), the four largest open-source Salesforce codebases were scanned from source, no API calls: **NPSP** (Nonprofit Success Pack: 1,044 classes, 26 triggers, TDTM framework, 193 CMT records, 765 fields, 157 UI bundles), **EDA** (625 classes, 37 triggers), **PMM**, and **apex-recipes**. Results after the fixes they drove:

| Corpus | Components | Edges (tokenizer → grammar) | Unresolved refs: start → tokenizer fixes → grammar engine | Dispatch edges |
|---|---|---|---|---|
| NPSP | 1,334 (was 1,113) | 25,543 → 24,617 | 2,743 → 431 → **13** (5 packages recorded as dependencies) | 88 |
| EDA | 814 (was 695) | 8,399 → 7,881 | 569 → 135 → **0** | 90 |
| PMM | 230 | 3,160 → 3,097 | 233 → 13 → **7** (all report columns) | — |
| apex-recipes | 174 (was 35) | 1,018 → 1,031 | 93 → 22 → **0** | — |

The grammar engine (AD-31 delivered early: Salesforce's ANTLR grammar through `tools/apex-parser`, see CLAUDE.md pitfall 32) removed about 2,300 NPSP edges the tokenizer had invented — field names read as objects (`Amount__c`, `Primary_Contact__c`), member accesses on lowercase variables read as standard objects (`address.…`), case-insensitive class candidates that matched the wrong class (`contactService` → `ContactService` when the variable is a `BDI_ContactService`) — and added about 1,200 real ones (`System.Label.X`, `Trigger.new` typing, `insert new X(...)`, `map.get(k).Field`). NPSP's remaining 13 are `Task.Engagement_Plan__c` (a field the corpus really does not define) and four one-off platform types.

What they fixed: SFDX package-directory semantics in the source reader (pitfall 26), installed-package fields as dependencies, platform types and constants, standard objects as types, and Apex-defined trigger tables (pitfall 31). NPSP scans in about six seconds. Next: the FSC org once signed up with a second email; deploying PMM to the Developer Edition to run `offramp verify` on its flows; the remaining unresolved classes at scale (inner types through inheritance, lowercase variables read as objects).

Done 2026-09-11: `offramp health` (six static rules incl. the after-save-flow → callout-Apex hazard and picklist values that do not exist — it immediately caught the fixture's NightlyHousekeeping filtering on `Status = 'Dead'`) and `offramp recipes` / `verify --auto-recipes` (recipes synthesised from required fields, entry conditions, validation-rule `ISBLANK` terms and sibling-flow silencing; both Developer Edition flows verified on generated recipes alone). Still open: verifying the Easy Spaces flows (screen flows need a user).

## Annotations (2026-09-11)

The LLM pass runs on every X-Ray scan (`LLM_API_KEY`; workspace-scoped key or `LLM_WORKSPACE_ID`). Two versions were run on the Developer Edition the same day:

| Pass | Input to the model | Mean confidence | Below 0.6 | Notes |
|---|---|---|---|---|
| v1 | 4,000-char slice of raw metadata | 0.75 | 43 at ≤ 0.6 | low scores = sharing-rule config dumps, bundles without source, managed code, truncated prompts |
| v2 | dossier: facts + process model + neighbourhood + source; evidence + unknowns required; deterministic tier hint; earned confidence | 0.92 (model-annotated rows 0.74 under the caps) | 11 | 216 empty sharing rules answered by rule at 1.0; managed code/bundles described from outside and capped at 0.5; 7 business-process narratives with risks |

The remaining low scores are honest: hidden managed-package code and bundles (unknowns listed), and components whose only visible caller is a page. Next for confidence: a review sample (accept/reject per annotation) to calibrate the caps against people, and verify results cited as facts on every flow (`offramp annotate --verify-results`).

## Known limitations (tracked, not yet scheduled)

- `offramp verify` drives record-triggered and autolaunched flows only; screen flows, platform-event flows and approval processes need a user or an event and report `not_verifiable`.


- The Flow component on Lightning pages (`flowruntime:flowRuntimeForFlexipage`) could not be deployed through the Metadata API in any region or template we tried, so the real-org fixture page carries fields and an LWC only; the page→flow edge is covered by an inline unit test. Build one page in App Builder, retrieve it, and diff to close this.
- The fixture org is single-user and small: fill rates and record counts are exercised but not representative, and reports shipped by Salesforce are not describable by the API.
- Apex analysis without Node falls back to the tokenizer (class properties untyped, fields mistaken for objects, case-insensitive class candidates). With the grammar engine the remaining gaps are relationship hops on typed variables (`con.Account.Name` records `Contact.Account` only), `is_sobject_name` still a fixed standard-object list plus suffix rules for names the schema snapshot lacks, and dotted `Outer.Inner` references that resolve to the outer class only.
- `MetadataComponentDependency` rows with no parser evidence become `dependency_api` edges at 0.6 confidence so the report can show them; they are excluded from "live automation" counts but do appear in totals.
- The Tooling path reads reports through the Analytics describe endpoint, capped at the 300 most recently run; the Metadata API path has no cap.
- Aggregate `COUNT(field)` is not allowed on long text, rich text, encrypted, multi-select, and compound fields, so those fields have no fill rate.

## Deferred (year two)

Phases 3–5 of v0.1 (translators, shadow, cutover) remain in `src/generate`, `src/validate`, `src/cutover` with their tests. They are not deleted and not extended until X-Ray has paying customers.
