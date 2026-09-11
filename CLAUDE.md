# Salesforce Off-Ramp

Three-product platform that reverse-engineers a Salesforce org's automation surface, translates each component to the appropriate execution tier (deterministic rules, durable workflows, or AI agents), validates via shadow execution against live production traffic, and migrates incrementally with cryptographically-provenanced rollback.

**Products:** X-Ray (diagnostic) → Agent Factory (translation + runtime) → Shadow Mode (validation + regression detection).

**Current focus (build plan v0.2, 2026-09-05): X-Ray only.** See [docs/build-plan.md](docs/build-plan.md) and [docs/strategy.md](docs/strategy.md). `src/generate`, `src/validate`, `src/cutover` stay in-tree but are not extended until X-Ray has paying customers (AD-27).

## Source-of-truth documents

These are the **canonical** specs. Read them before changing anything significant.

- [Salesforce-OffRamp-Build-Plan-v2.1.docx](Salesforce-OffRamp-Build-Plan-v2.1.docx) — strategic build plan (34 weeks, M0–M13). Immutable except via formal v2.x revision.
- [docs/research.md](docs/research.md) — independent technology evaluation + 6 gap items (Appendix A) integrated into AD-21..AD-26.
- [docs/architecture.md](docs/architecture.md) — engineering architecture spec; components C1–C18.
- [docs/build-plan.md](docs/build-plan.md) — v0.2 X-Ray-first phase plan with runnable gates (v0.1 archived alongside).
- [docs/strategy.md](docs/strategy.md) — market study summary and decisions AD-27..AD-31.

When the strategic plan and the engineering architecture disagree, **the architecture document wins for code-level decisions**; escalate the conflict to the tech lead so v2.x can be updated.

## Commands

```bash
make dev          # uv sync, install pre-commit hooks, start local FalkorDB
make falkordb     # FalkorDB without Docker: brew redis + module from GitHub releases, persisted in ~/.local/share/offramp
make falkordb-browser  # FalkorDB Browser UI (from source, Node) on http://localhost:3000; connect to localhost:6379
# LLM annotations need LLM_API_KEY in .env (Anthropic; default model claude-sonnet-5); every scan so far used --skip-annotations
make test         # pytest unit + integration
make lint         # ruff format + ruff check
make typecheck    # mypy --strict
make smoke        # end-to-end smoke test against scratch org
make clean        # remove caches and build artifacts
```

CLI entry points (added incrementally per phase):

```bash
uv run offramp extract --org <alias> --out <dir>
uv run offramp xray --org <alias> --out <dir>
uv run offramp compare --a <extract-dir> --b <extract-dir> [--a-is-subset]   # repo vs org diff (C25)
uv run offramp verify --from <extract-dir> --org <alias> --recipes config/verify_recipes.example.json [--roundtrip]   # trace validation (C26)
uv run offramp verify --from <extract-dir> --log <apex-debug-log>   # offline, from a saved log
uv run offramp sync --org <alias> --library <dir> [--interval 3600]   # scan into the library + record the change log (D.6)
uv run offramp changes --library <dir> --org <alias> [--last N | --component <category>:<api_name>]
uv run offramp generate --process <id> --out <dir>
uv run offramp deploy --artifact <dir> --target <env>
uv run offramp shadow start --process <id>
uv run offramp cutover advance --process <id>
```

## Stack

- **Python 3.12**, `uv` for deps, ruff (format+lint), mypy strict, pytest + pytest-asyncio
- **Pydantic v2** for all data schemas (Component, Dependency, AST, ShadowComparison, etc.)
- **FastAPI** + **MCP server SDK** for the gateway (C12)
- **Temporal** (Python SDK 1.16+) for Tier 2 durable workflows
- **LangGraph** for Tier 3 judgment-required agents (run as Temporal activities for durability)
- **simple-salesforce 1.12.9** for REST + Bulk API 2.0
- **Salesforce Pub/Sub API gRPC client** (Avro encoding) for CDC + Platform Events
- **FalkorDB** (Cypher) for the Component knowledge graph — two graphs: `<org alias>` (the X-Ray dependency graph, written by `offramp xray` unless `--no-graph-db`) and `offramp_knowledge` (the cross-org process library, written by `offramp kg ingest --falkordb`). Both are optional mirrors; the file store and in-memory graph are the source of truth.
- **Postgres 16** for app state + shadow store
- **Own Apex tokenizer/reference extractor** in `src/extract/apex` (AD-31; summit-ast is archived, do not add it); regex classifiers for LWC (`src/extract/lwc`) and Aura (`src/extract/aura`); own Flow XML normalizer in `src/extract/categories/flow.py`
- **Salto** (NaCl) + **sf CLI** for metadata extraction
- **Engram** (internal Rust + Python SDK) for provenance; **F44** for Base L2 Merkle anchoring of sensitive decisions
- **Kafka (MSK) prod / Redis Streams dev** via pluggable event-bus abstraction

## Key architecture decisions

The 20 ADs in v2.1 §3 plus these deltas from research:

- **AD-21**: Pub/Sub 72h-cliff reconciliation in `src/validate/reconcile`
- **AD-22**: 7th divergence category `gap_event_full_refetch_required`
- **AD-23**: OoE test suite explicitly covers mixed-DML setup/non-setup boundaries
- **AD-24**: MCP gateway implements `/limits` polling + per-process API quota allocation
- **AD-25**: JWT cert rotation runbook + automated quarterly sandbox rotation test
- **AD-26**: SF API version pinned to **66.0 (Spring '26)**; upgrade cadence one release behind GA
- **AD-27**: X-Ray first; translation/validation/cutover deferred to year two
- **AD-28**: Own parsers produce every dependency edge; `MetadataComponentDependency` is a cross-check only (it is Beta, 2,000-row capped, unfilterable by name)
- **AD-29**: Two extraction paths, REST/Tooling first, sf CLI second; both feed `src/extract/pull/source_tree.py`
- **AD-30**: Every edge carries `evidence` + `confidence`, surfaced in the X-Ray report
- **AD-31**: No summit-ast; tokenizer-based Apex analysis behind the `ApexAnalysis` contract
- **AD-32**: The knowledge library is the product asset. Every automation becomes a `ProcessDefinition` (`src/core/process.py`), stored content-addressed in `src/knowledge`; identical logic across scans/orgs is one entry. Never add Salesforce-only vocabulary to the process model when a neutral term exists.

## File structure

```
src/
├── core/            # shared models, secrets, utils
├── extract/         # C1–C4, C19–C21: pull (source_tree, tooling_api, sf_cli), apex, schema, dispatch, lwc, ooe_audit
├── understand/      # C5–C6, C22–C23: dependencies, impact, process_ir, graph, annotate, cluster, orphan, xray report
├── knowledge/       # C24: content-addressed process library (store, render, falkor) — the reusable asset (AD-32)
├── generate/        # C7–C9: tier1, tier2, tier3, formula, adapters
├── runtime/         # C10–C11: ooe state machine, rules engine
├── mcp/             # C12: gateway, tools, quota, anchoring
├── validate/        # C13–C15: shadow, compare_mode, reconcile
├── cutover/         # C16: router, saga, orchestrator, parity_report
├── engram/          # C17: provenance client
├── event_bus/       # C18: pluggable bus
└── cli/             # offramp CLI entry points

tests/
├── unit/
├── integration/     # scratch-org-backed
├── ooe_runtime/     # 200+ cases — see §18.3 of v2.1
└── load/            # benchmark targets per Phase 5 gate
```

## Stack-specific pitfalls (from research)

These are the things that will silently bite. **Read these before writing code that touches any of them.**

1. **Pub/Sub API 72h replay-id cliff** — if a subscriber lags >3 days, replay state is gone. C15 `src/validate/reconcile` handles this; do not bypass it. Set Kafka retention ≥96h.
2. **CDC gap events** are header-only (no field data). Naive consumers silently lose deltas. Always check `event.ChangeEventHeader.changeType` and trigger full re-fetch on `GAP_*`.
3. **Mixed-DML exceptions** in Apex (setup + non-setup objects in same transaction). The OoE runtime test suite covers this — when adding cases that touch User/Group/PermissionSet alongside Account/Contact/Lead, expect to enforce the boundary.
4. **API call quota is org-wide**, not per-integration. Enterprise: 100K + 1K/user/24h, rolling window; **Developer Edition: 15K/24h, and one X-Ray scan costs ~600 calls** (≈440 Tooling queries because `Metadata` is fetched one record at a time, plus 35 describes, 35 FieldDefinition queries, per-object aggregates, retrieves, and report describes). Twelve scans plus deploys exhausted a DE on 2026-09-06 (`REQUEST_LIMIT_EXCEEDED`, even `/limits` refused). The MCP gateway's quota allocator must not be bypassed; never call simple-salesforce directly from runtime code; the scan should read `/limits` first and refuse or degrade (skip data profile, skip per-record Metadata for surfaces) when the budget is short.
5. **Flow deploys as inactive** by default outside production. If we ever deploy back to SF (e.g., to remove deprecated Flows during cutover), automate post-deploy activation via Tooling API.
6. **Approval Process has no CDC.** Must fire Platform Event from Apex trigger on `ProcessInstance` or poll. The Tier 2 translator handles this — don't try to subscribe to a `ProcessInstanceChangeEvent` channel; it doesn't exist.
7. **JWT cert rotation has no warning.** Salesforce will silently let your Connected App start failing when the cert expires. Cert rotation runbook in `docs/runbooks/jwt_cert_rotation.md`; automated quarterly rotation test enforces this.
8. **No Flow-to-code converter exists.** Don't try to be clever — Flow translation is manual via the Translation Matrix in the v2.1 plan §5.4. The translator emits skeletons; humans review.
9. **Salesforce Order of Execution has 21 steps** with specific re-fire (step 12) and cascade semantics. **Do not implement OoE logic outside `src/runtime/ooe`.** All transaction semantics live there.
10. **Camunda 8 self-managed requires Enterprise license** for prod since Oct 2024. We chose Temporal for this reason — do not introduce Zeebe.
11. **n8n Sustainable Use License** prohibits SaaS-product use. If we ever bundle n8n as a no-code lane, internal automation only.
12. **`MetadataComponentDependency` is Beta** (still at v68.0). 2,000 rows per Tooling query, 100,000 via Bulk 2.0, no `queryMore`/`OFFSET`, no filter by component name, reports omitted, Bulk queries fail in Developer Edition. Query it per `MetadataComponentType`, never as one unbounded query, and never make it the only source of an edge (AD-28).
13. **Metadata API retrieve caps**: 10,000 files / 39 MB compressed per retrieve. The sf CLI pull client batches `package.xml` per type and re-splits on failure.
14. **Tooling API quirks that bit us**: `Metadata` / `FullName` on ValidationRule, WorkflowRule, CustomField, Layout, FlexiPage are only queryable one record at a time (list Ids, then fetch each); `TableEnumOrId` is a CustomObject Id (`01I…`) for custom objects, not a name; CronTrigger, AsyncApexJob, ProcessDefinition, PermissionSet, FieldPermissions, Report are REST objects, not Tooling objects; `PlatformEventChannelMember` exposes `EventChannel` + `SelectedEntity` (`Deal__ChangeEvent` → `Deal__c`). Confirmed against a live Developer Edition (Gate A, 2026-09-06): AssignmentRule / AutoResponseRule carry `EntityDefinitionId` on Tooling (`SobjectType` only on REST); EscalationRule has no Tooling or REST object at all (Metadata API only); WorkflowAlert / WorkflowTask have no `Name` column (fetch `FullName` with the per-record `Metadata` query); a per-record `Metadata` fetch can answer HTTP 500 for Salesforce-internal rows (the `CssDetail` layout), so every such loop must skip and count failures; `analytics/reports/<id>/describe` is FORBIDDEN for some Salesforce-shipped reports.
15. **`CategoryName` has 30 members**: 22 automation categories (the original 21 plus `aura_bundle`) and eight *surface* categories (page layout, Lightning page, permission set, profile, report, custom tab, custom application, path assistant). Surfaces never fire on save and never count as automation usage; use `AUTOMATION_CATEGORIES` when you mean the 22. Custom metadata *records* are not a category: they are `cmt_record` graph nodes (`ConfigRecord` in FalkorDB) linking their type, the fields/classes their values name, and the Apex that reads the type.
16. **Categories registry is lazy.** `offramp.extract.categories.base.get_extractor` imports the extractor modules on first call to avoid the LWC ↔ registry circular import. Do not import `offramp.extract.lwc.bundle` from `_passthrough.py`.
17. **SOQL `LIKE` treats `_` as a single-character wildcard.** `QualifiedApiName LIKE '%__e'` matched 749 entities (`OrderShare`, `RecordType`…) in a stock org, and `EntityDefinition` rejects the `\_` escape, so confirm suffixes client-side (`endswith("__e")`).
18. **Metadata API `retrieve` returns Metadata API format**, not source format: `Lead.assignmentRules`, `Sales_User.permissionset`, no `-meta.xml`. `src/extract/pull/mdapi.py` renames on unzip; the source tree reader never sees raw retrieve output. Also `simple-salesforce` `retrieve_zip` base64-decodes unconditionally and crashes while the job is still `InProgress`: poll `check_retrieve_status` first.
19. **sf CLI 2.x redacts `accessToken` in `sf org display --json`**; the token comes from `sf org auth show-access-token --json`. Source-format deploys consult the org's source tracking and silently skip files it believes are unchanged (`NothingToDeploy`); `scripts/scratch_org.sh` converts to Metadata API format and deploys with `--metadata-dir` for that reason, and `sf project delete source` deletes the **local** file too.
20. **Metadata that the parser accepts but Salesforce will not deploy** (learned deploying the fixture): Account sharing rules need an `accountSettings` child (case/contact/opportunity access levels), and the error message blames org `AccountSettings`; Lightning pages reference custom LWCs by bare name (`leadCard`, not `c:leadCard`), put fields inside a `flexipage:fieldSection` facet, and reject the Flow component (`flowruntime:flowRuntimeForFlexipage`) in every region we tried; report columns are unqualified standard names (`LAST_NAME`, `CREATED_DATE`) and the Lead report type is `LeadList`; every Flow node needs `label` + `locationX/Y`, decisions need `defaultConnectorLabel`, rules need `conditionLogic`; new orgs cannot create Process Builder processes (the fixture's `processType=Workflow` flow is skipped at deploy).
21. **Tooling `Metadata` JSON is not the XML with the brackets changed.** Every flow value carries *all* keys with `null` for the unused ones (`{"stringValue": null, "elementReference": "$Record.Id", …}`): take the first non-null key, never the first key present, or every value reads as None. Booleans are JSON `true` vs XML `"true"`, resources come back in a different order, and Salesforce strips defaulted values on save (ObjectProvided screen-field names, `userHierarchyField` approver `Manager`, `whenMultipleApprovers`, first-step `rejectBehavior`). `src/understand/process_ir.py` canonicalises all of this so the authored copy and the retrieved copy of one automation hash to one library entry (AD-32); when adding a field to a `ProcessDefinition`, ask what Salesforce does to it on save.
22. **REST `describe` hides fields the running user has no FLS on.** A field deployed through the Metadata API has FLS for nobody until a profile or permission set grants it, so it is invisible to describe yet live for automation (13 of 13 custom Lead fields existed; describe showed 8, the earlier snapshot 5). Tooling `FieldDefinition` (`WHERE EntityDefinitionId = 'Lead'`, no `IsCustom` column, no relationship filter) ignores FLS; `supplement_from_field_definitions` adds what describe hid, flagged `hidden_from_describe`.
23. **Report columns are report-type aliases, not field names**: `LAST_NAME`, `CREATED_DATE`, `LEAD.STATUS`. Map them (`_report_field`) or the graph grows phantom `Lead.LAST_NAME` field nodes. Metadata API ZIP member names are percent-encoded (`Account-Account %28Marketing%29 Layout`); decode on unzip or every such layout appears twice.
24. **Apex is case-insensitive and real code uses it**: Easy Spaces calls `customerServices.getCustomerFields()` and `testDataFactory.makeMarkets()`. A "capitalised = class" heuristic misses those; the analyzer records lower-case call qualifiers as `candidate_class_references` and the graph builder keeps only the ones that name a real class (never reported as unresolved). Related: `Outer.Inner` types resolve to the outer class; `System`/`Schema`/`Database` types (`Security`, `AccessType`, `SObjectAccessDecision`…) are in `_PLATFORM_TYPES`, not unresolved classes; `Customer_Fields__mdt.Customer_City__r.QualifiedApiName` stops at the metadata-relationship field, which is the real dependency.
25. **`MetadataComponentDependency` names custom objects without their suffix** (`Territory` for `Territory__c`, `Customer_Fields` for `Customer_Fields__mdt`), reports standard page templates as `FlexiPage` refs (`flexipage:tabset`), and points record-page assignments object → page. The cross-check resolves suffixes (custom first: a standard `Territory` object exists), skips `flexipage:*`, and accepts a parser edge in either direction. LWC composition is two channels: `<c-error-panel>` in templates and `from "c/ldsUtils"` in JS. Aura bundles are not extracted (Easy Spaces wraps LWCs in Aura for App Builder); those rows stay API-only by design until Aura becomes a category.
26. **SFDX source trees are multi-root and contain decoys.** `sfdx-project.json` lists several `packageDirectories`, each with its own `main/default`, and one object can be split across them (Easy Spaces defines `Reservation__c` in `es-base-objects` and adds fields in `es-base-code`); `.git/objects` looks like a metadata `objects/` folder to a naive glob. `SourceTree.roots` reads every package root, merges split objects, and skips `.git`/`node_modules`/`.sfdx`/`.sf`. Custom metadata *records* live in `customMetadata/<Type>.<Record>.md-meta.xml` and are read by `read_cmt_records_from_source`.
27. **`FieldDefinition.DataType` reports metadata-relationship fields as `Picklist()`**, so the FLS-independent schema supplement cannot see that `Customer_Fields__mdt.Customer_City__c` points at `FieldDefinition`; the source path (field XML with `referenceTo`) can. Fetch the Tooling `CustomField.Metadata` for `MetadataRelationship` fields when the org path needs the target.
28. **Lightning Message Service is a dependency channel.** `@salesforce/messageChannel/X__c` imports plus `publish`/`subscribe` couple LWCs that never import each other (four Easy Spaces bundles share two channels). Modelled as an external `LightningMessageChannel` node with an edge from every bundle on it. Flows reference UI in two more ways — `extensionName` on screen fields and `actionType=component` actions — both now `lwc_bundles` references; when the name is an Aura bundle it stays unresolved by design.
29. **Validation is three layers, and only the third is behavioural.** (1) Structural: two extraction paths reconciled, the Dependency API cross-check, evidence + confidence per edge, fidelity + notes per process definition. (2) Differential: `offramp compare` diffs two extract outputs component by component (repo vs org with `--a-is-subset`; schema-only edge differences are expected and reported apart). (3) Behavioural: `offramp verify` runs a flow in the org under a debug trace (`DebugLevel` Workflow=FINER via Tooling POST, `TraceFlag` on the traced user, DML from a recipe or the Flow REST action, `ApexLog` body fetched as **text**, never through `restful`) and checks visited elements, element kinds, transitions, decision outcomes (`FLOW_RULE_DETAIL`), action targets (`FLOW_ACTIONCALL_DETAIL`) and DML against the `ProcessDefinition`; `--roundtrip` renders the definition back to Flow XML (`knowledge/flow_xml.py`, **elements in Metadata API schema order** or Salesforce misreads the start element), deploys it active as `<Name>_rt` with its own label, invokes it too for autolaunched flows, requires the copy to trace identically, then deactivates and deletes its versions through Tooling (deleting the last version removes the definition). Confirmed live on 2026-09-10: real logs name flows by **label**; `FLOW_CREATE_INTERVIEW_END` carries `id|label`; `FLOW_START_INTERVIEW_END` arrives after the *first* element because the rest run deferred, so id-less lines (`FLOW_ELEMENT_ERROR`, `DML_BEGIN`) belong to the interview that last logged an element; a subflow's elements are logged under the parent's interview after `FLOW_SUBFLOW_DETAIL`. Screen flows need a user and scheduled flows are not invocable on demand: both report `not_verifiable`. A runtime error the org raises (an unverified sender, a missing permission) is its own status, `runtime_error`, with the path still checked.
30. **Things the live verify found in the org, not in the model** (and one in the model: a lookup's `outputReference` was missing from `ProcessDefinition`, so a rendered copy passed a null record id downstream — carried as `Step.extras["output"]` now; identity tests cannot catch a value lost *before* rendering, only execution can)**.** An email alert addressed to the record owner has zero recipients (`Probably Limit Exceeded or 0 recipients`) when an assignment rule has given the record to a queue with no email address — the fixture's Inside Sales queue carries a placeholder the deploy script substitutes. An after-save record-triggered flow that calls Apex which makes a synchronous callout always fails (`CalloutException: uncommitted work pending`) and Salesforce rejects the *whole save* (`CANNOT_EXECUTE_FLOW_TRIGGER`) — the fixture's `LeadScoringService` now enqueues a Queueable with `Database.AllowsCallouts`; X-Ray should flag that pattern statically (after-save flow → invocable Apex with a callout) — not built yet. Email alerts fail until the Default Workflow User's email is verified (Setup → Process Automation Settings), which also makes every record-triggered flow that sends one reject the save. The SOAP Metadata API has answered a retrieve with an empty 404 after a few hundred REST calls on one session; `mdapi_retrieve` retries once on a fresh session. `sf project delete source` deletes the **local** file too; flow copies are removed by deleting their versions via Tooling, not via destructive changes.

## Conventions

- **Type hints** on every function signature; mypy strict enforced in CI.
- **Pydantic v2** for boundaries (extraction, MCP gateway, generated artifacts). Plain dataclasses for internal-only structs.
- **structlog** (`src/core/logging.py`) — never bare `print()`.
- **Conventional commits** (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- **Engram-anchor every consequential decision**: extraction outputs, annotations, translations, shadow comparisons, routing decisions. The Engram anchor IS the audit trail; if it's not anchored, it didn't happen.
- **Single-tenant per customer** (AD-10). No code path may assume multi-tenant data sharing.
- **Pin SF API version** to `66.0` in `SF_API_VERSION` env var; never read from a "current" alias.
- **Scratch-org-backed integration tests** — recorded responses live in `tests/integration/fixtures/`; regenerate with `make refresh-fixtures` against a clean scratch org.

## When you don't know

1. Check the v2.1 plan first — it covers strategic intent.
2. Check `docs/architecture.md` for component contracts.
3. Check `docs/research.md` for the rationale behind a tech choice.
4. Use Context7 MCP for live SDK docs (Temporal, FastAPI, Pydantic, simple-salesforce).
5. Salesforce-specific quirks: prefer the `mcp__runpod-docs__search_runpod_documentation`-style targeted search over guessing — Salesforce API behavior changes between releases.
