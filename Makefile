# Salesforce Off-Ramp — developer workflow targets.
# All commands run via `uv run` so no pre-activated venv is needed.

.PHONY: help dev sync falkordb falkordb-stop falkordb-browser falkordb-browser-stop test test-unit test-integration test-ooe lint lint-fix typecheck smoke clean refresh-fixtures hooks xray-fixture gate

help:  ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

dev: sync hooks apex-parser falkordb  ## Full developer setup: install deps + git hooks + Apex grammar + local FalkorDB.

sync:  ## Install/update Python dependencies.
	uv sync --all-extras --group dev

apex-parser: tools/apex-parser/node_modules/@apexdevtools/apex-parser  ## Install the grammar-backed Apex parser (Node); the tokenizer is the fallback without it.

tools/apex-parser/node_modules/@apexdevtools/apex-parser: tools/apex-parser/package.json tools/apex-parser/package-lock.json
	@command -v npm >/dev/null || { echo "npm missing: install Node (brew install node)"; exit 1; }
	cd tools/apex-parser && npm ci --no-audit --no-fund
	@touch $@

hooks:  ## Install pre-commit git hooks.
	uv run pre-commit install --install-hooks

# FalkorDB without Docker: Redis (brew) + the FalkorDB module from GitHub releases.
FALKORDB_VERSION ?= v4.20.4
FALKORDB_DIR ?= $(HOME)/.local/share/offramp
FALKORDB_SO := $(FALKORDB_DIR)/falkordb.so
FALKORDB_ASSET := $(shell uname -s | tr A-Z a-z)-$(shell uname -m | sed 's/aarch64/arm64/; s/arm64/arm64v8/; s/x86_64/x64/')

$(FALKORDB_SO):
	@mkdir -p $(FALKORDB_DIR)
	curl -fsSL -o $@ https://github.com/FalkorDB/FalkorDB/releases/download/$(FALKORDB_VERSION)/falkordb-$(FALKORDB_ASSET).so
	chmod +x $@

falkordb: $(FALKORDB_SO)  ## Start FalkorDB (Redis + module) on localhost:6379 in the background.
	@command -v redis-server >/dev/null || { echo "redis-server missing: brew install redis"; exit 1; }
	@redis-cli ping >/dev/null 2>&1 && echo "redis already running on 6379" || \
	  redis-server --port 6379 --loadmodule $(FALKORDB_SO) --daemonize yes --dir $(FALKORDB_DIR) \
	    --logfile $(FALKORDB_DIR)/redis.log --pidfile $(FALKORDB_DIR)/redis.pid --save "" --appendonly yes
	@sleep 1; redis-cli GRAPH.LIST >/dev/null && echo "FalkorDB ready (graphs persist in $(FALKORDB_DIR))"

falkordb-stop:  ## Stop the local FalkorDB.
	@redis-cli shutdown 2>/dev/null || true

# FalkorDB Browser (the web UI the Docker image bundles) from source: Node + Next.js.
FALKORDB_BROWSER_DIR ?= $(FALKORDB_DIR)/falkordb-browser
FALKORDB_BROWSER_PORT ?= 3000

$(FALKORDB_BROWSER_DIR)/package.json:
	@command -v npm >/dev/null || { echo "npm missing: install Node (brew install node)"; exit 1; }
	@mkdir -p $(FALKORDB_DIR)
	git clone -q --depth 1 https://github.com/FalkorDB/falkordb-browser.git $(FALKORDB_BROWSER_DIR)

$(FALKORDB_BROWSER_DIR)/node_modules: $(FALKORDB_BROWSER_DIR)/package.json
	cd $(FALKORDB_BROWSER_DIR) && npm install --silent

$(FALKORDB_BROWSER_DIR)/.env.local: $(FALKORDB_BROWSER_DIR)/package.json
	@cd $(FALKORDB_BROWSER_DIR) && cp .env.local.template .env.local && \
	  sed -i '' "s|^AUTH_SECRET=.*|AUTH_SECRET=$$(openssl rand -hex 32)|; \
	             s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=\"$$(openssl rand -hex 32)\"|; \
	             s|^PORT=.*|PORT=$(FALKORDB_BROWSER_PORT)|; \
	             s|^AUTH_URL=.*|AUTH_URL=http://localhost:$(FALKORDB_BROWSER_PORT)/|; \
	             s|^ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=http://localhost:$(FALKORDB_BROWSER_PORT)|" .env.local

falkordb-browser: falkordb $(FALKORDB_BROWSER_DIR)/node_modules $(FALKORDB_BROWSER_DIR)/.env.local  ## Open the FalkorDB Browser UI on localhost:3000 (connect with host localhost, port 6379, no credentials).
	@if curl -s -o /dev/null http://localhost:$(FALKORDB_BROWSER_PORT)/; then echo "browser already running"; else \
	  cd $(FALKORDB_BROWSER_DIR) && (nohup npm run dev > $(FALKORDB_DIR)/browser.log 2>&1 & echo $$! > $(FALKORDB_DIR)/browser.pid); \
	  for i in $$(seq 1 60); do curl -s -o /dev/null http://localhost:$(FALKORDB_BROWSER_PORT)/ && break; sleep 2; done; fi
	@echo "FalkorDB Browser: http://localhost:$(FALKORDB_BROWSER_PORT)/  (host localhost, port 6379, no user/password)"
	@command -v open >/dev/null && open http://localhost:$(FALKORDB_BROWSER_PORT)/ || true

falkordb-browser-stop:  ## Stop the FalkorDB Browser dev server.
	@[ -f $(FALKORDB_DIR)/browser.pid ] && pkill -P $$(cat $(FALKORDB_DIR)/browser.pid) 2>/dev/null; \
	  [ -f $(FALKORDB_DIR)/browser.pid ] && kill $$(cat $(FALKORDB_DIR)/browser.pid) 2>/dev/null; \
	  rm -f $(FALKORDB_DIR)/browser.pid; pkill -f "next dev" 2>/dev/null || true

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

smoke:  ## End-to-end smoke test (mocked SF backend).
	uv run pytest -m smoke

xray-fixture:  ## Run the full X-Ray pipeline on the fixture org (no FalkorDB, no LLM) and verify the report.
	uv run offramp xray --fixture tests/integration/fixtures/sample_org --out out/xray --no-graph-db --skip-annotations
	uv run python scripts/verify_xray.py out/xray
	uv run python scripts/verify_extract_coverage.py out/xray/extract --min-categories 21

gate:  ## Build-plan v0.2 gate: lint + typecheck + tests + fixture X-Ray.
	$(MAKE) lint typecheck test xray-fixture

clean:  ## Remove caches and build artifacts.
	rm -rf .ruff_cache .mypy_cache .pytest_cache .coverage htmlcov coverage.xml
	rm -rf build dist *.egg-info
	rm -rf out artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

refresh-fixtures:  ## Re-record integration test fixtures against a scratch org.
	@echo "TODO (Phase 0.10): wire up scratch-org fixture refresh."
	@exit 1
