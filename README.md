# Salesforce Off-Ramp

Engineering implementation of the Off-Ramp platform.

- **Strategic plan:** [Salesforce-OffRamp-Build-Plan-v2.1.docx](Salesforce-OffRamp-Build-Plan-v2.1.docx)
- **Research:** [docs/research.md](docs/research.md)
- **Architecture:** [docs/architecture.md](docs/architecture.md)
- **Build plan (phased):** [docs/build-plan.md](docs/build-plan.md)
- **Project conventions:** [CLAUDE.md](CLAUDE.md)

## Quickstart

```bash
make dev          # uv sync + install pre-commit hooks
make lint         # ruff format check + lint
make typecheck    # mypy strict
make test         # unit tests
make smoke        # end-to-end smoke (mocked SF in Phase 0)
```

See `make help` for the full target list.
