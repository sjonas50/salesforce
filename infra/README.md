# Infrastructure (Phase 0 stubs)

Phase 0 ships placeholders so engineers know where production infra will live.
Real Terraform + Helm chart authorship lands in Phase 5 (tasks 5.8 / 5.9)
once the runtime components are stable enough to deploy.

## Layout

```
infra/
├── terraform/        Azure / AWS modules; AKS / EKS clusters and namespaces
├── helm/             Helm chart for the Off-Ramp platform
└── operator/         On-prem Kubernetes operator (secrets, CDC ingestion)
```

## Status

- [ ] Terraform AKS module (single-tenant namespace per customer)
- [ ] Terraform EKS module (Fisher and other AWS-committed customers)
- [ ] Helm chart skeleton with one Service (MCP gateway)
- [ ] On-prem operator skeleton (Phase 5.9)

See [docs/build-plan.md](../docs/build-plan.md) Phase 5.8 / 5.9.
