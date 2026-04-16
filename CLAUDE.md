# Salesforce Off-Ramp

Three-product platform that reverse-engineers a Salesforce org's automation surface, translates each component to the appropriate execution tier (deterministic rules, durable workflows, or AI agents), validates via shadow execution against live production traffic, and migrates incrementally with cryptographically-provenanced rollback.

**Products:** X-Ray (diagnostic) → Agent Factory (translation + runtime) → Shadow Mode (validation + regression detection).

## Source-of-truth documents

These are the **canonical** specs. Read them before changing anything significant.

- [Salesforce-OffRamp-Build-Plan-v2.1.docx](Salesforce-OffRamp-Build-Plan-v2.1.docx) — strategic build plan (34 weeks, M0–M13). Immutable except via formal v2.x revision.
- [docs/research.md](docs/research.md) — independent technology evaluation + 6 gap items (Appendix A) integrated into AD-21..AD-26.
- [docs/architecture.md](docs/architecture.md) — engineering architecture spec; components C1–C18.
- [docs/build-plan.md](docs/build-plan.md) — code-level phase plan with runnable test gates.

When the strategic plan and the engineering architecture disagree, **the architecture document wins for code-level decisions**; escalate the conflict to the tech lead so v2.x can be updated.

## Commands

```bash
make dev          # uv sync, install pre-commit hooks, start local infra
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
- **FalkorDB** (Cypher) for the Component knowledge graph
- **Postgres 16** for app state + shadow store
- **tree-sitter-javascript** for LWC analysis; **summit-ast** for Apex parsing; **lightning-flow-scanner-core** for Flows
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

## File structure

```
src/
├── core/            # shared models, secrets, utils
├── extract/         # C1–C4: pull, dispatch, lwc, ooe_audit
├── understand/      # C5–C6: graph, annotate, cluster, orphan, xray report
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
4. **API call quota is org-wide**, not per-integration. Enterprise: 100K + 1K/user/24h, rolling window. The MCP gateway's quota allocator must not be bypassed; never call simple-salesforce directly from runtime code.
5. **Flow deploys as inactive** by default outside production. If we ever deploy back to SF (e.g., to remove deprecated Flows during cutover), automate post-deploy activation via Tooling API.
6. **Approval Process has no CDC.** Must fire Platform Event from Apex trigger on `ProcessInstance` or poll. The Tier 2 translator handles this — don't try to subscribe to a `ProcessInstanceChangeEvent` channel; it doesn't exist.
7. **JWT cert rotation has no warning.** Salesforce will silently let your Connected App start failing when the cert expires. Cert rotation runbook in `docs/runbooks/jwt_cert_rotation.md`; automated quarterly rotation test enforces this.
8. **No Flow-to-code converter exists.** Don't try to be clever — Flow translation is manual via the Translation Matrix in the v2.1 plan §5.4. The translator emits skeletons; humans review.
9. **Salesforce Order of Execution has 21 steps** with specific re-fire (step 12) and cascade semantics. **Do not implement OoE logic outside `src/runtime/ooe`.** All transaction semantics live there.
10. **Camunda 8 self-managed requires Enterprise license** for prod since Oct 2024. We chose Temporal for this reason — do not introduce Zeebe.
11. **n8n Sustainable Use License** prohibits SaaS-product use. If we ever bundle n8n as a no-code lane, internal automation only.

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
