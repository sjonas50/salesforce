# Salesforce Off-Ramp — developer workflow targets.
# All commands run via `uv run` so no pre-activated venv is needed.

.PHONY: help dev sync test test-unit test-integration test-ooe lint lint-fix typecheck smoke clean refresh-fixtures hooks

help:  ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

dev: sync hooks  ## Full developer setup: install deps + git hooks.

sync:  ## Install/update Python dependencies.
	uv sync --all-extras --group dev

hooks:  ## Install pre-commit git hooks.
	uv run pre-commit install --install-hooks

test: test-unit  ## Run the default test suite (unit only — fast).

test-unit:  ## Run unit tests.
	uv run pytest -m "not integration and not smoke and not load"

test-integration:  ## Run integration tests (require external services).
	uv run pytest -m integration

test-ooe:  ## Run the OoE runtime test suite.
	uv run pytest -m ooe

lint:  ## Lint check (ruff).
	uv run ruff check .
	uv run ruff format --check .

lint-fix:  ## Lint + format with auto-fix.
	uv run ruff check --fix .
	uv run ruff format .

typecheck:  ## Type-check with mypy strict.
	uv run mypy

smoke:  ## End-to-end smoke test (currently uses mocked SF; real scratch org TODO Phase 0.10).
	uv run pytest -m smoke

clean:  ## Remove caches and build artifacts.
	rm -rf .ruff_cache .mypy_cache .pytest_cache .coverage htmlcov coverage.xml
	rm -rf build dist *.egg-info
	rm -rf out artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

refresh-fixtures:  ## Re-record integration test fixtures against a scratch org.
	@echo "TODO (Phase 0.10): wire up scratch-org fixture refresh."
	@exit 1
