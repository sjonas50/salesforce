# Strategy: X-Ray vs Sweep

**Date:** 2026-09-05. Full market study with sources: https://claude.ai/code/artifact/93e8fe56-c7c4-4b4e-adba-7d068deb1800

## Facts that drive the plan

- ServiceNow acquired Sweep on 2026-09-03 (ServiceNow spokesperson to CTech; Salesforce Ben 2026-09-04). No press release. Salesforce's Partner Program Agreement §9.2 permits immediate termination when a partner is acquired by a competitor; §9.3 permits termination on 30 days' notice. Delisting is a scenario, not an announced action.
- Salesforce's `MetadataComponentDependency` is still **Beta** at API v68.0: 2,000 rows per Tooling query, 100,000 via Bulk 2.0, no filter by name, no `OFFSET`/`queryMore`, reports omitted. Salesforce archived its own `dependencies-cli` in May 2025.
- Free floor: Lightning Flow Scanner (per-Flow linting), Org Check (health scan, basic dependency flowcharts), Migrate to Flow (simple conversions).
- Sweep pricing: $2.5K / $5K / $7K per month with 2 / 5 / 10 seats and 1 / 1 / 3 sandboxes; admin seat add-on $6K/yr; sandbox add-on $20K/yr. Most-cited G2 gap: cannot execute cleanup.
- Elements.cloud from ~$10K/yr; reviewers cite density and learning curve. Gearset $215–$320/user/mo, DevOps-first. Metazoa: own dependency engine, desktop, retrospective.
- Workflow Rules / Process Builder: end of support 2025-12-31, still running, no shutdown announced. Migration demand is tech-debt driven, not deadline driven.

## Decisions

1. One product for twelve months: **X-Ray**. See AD-27.
2. Position: "the org intelligence layer that shows its work, built for Salesforce, owned by no rival."
3. Pricing: per production org by size, unlimited users, free full-graph scan. Team ~$1.2K–$1.8K/mo; Enterprise ~$4K–$6K/mo with multi-org, SSO, API/MCP, cleanup execution.
4. Channel: OAuth-connected external SaaS first; AppExchange connector after revenue. SI/consultant partner license.
5. Differentiator no rival has: execution-order impact analysis from the OoE runtime.
6. Immediate marketing: factual "what the Sweep acquisition means for your org" page with a free graph export.

## Open questions

- Does Salesforce act on §9.2 against Sweep? Watch the AppExchange listing weekly.
- Observed edge accuracy of Elements and Metazoa in the field (five architect reference calls).
- Is Tooling `Flow.Metadata` viable at scale? Test on a scratch org.
- Is "org readiness for Agentforce" a budget line? Ask SIs.
