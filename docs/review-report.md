# Code Review Report
**Date:** 2026-04-16
**Status:** PASS WITH NOTES

## Critical Issues (must fix)

1. **Live Anthropic API key hardcoded in `/Users/sjonas/salesforce/.env` (lines 36, 38).** The values look like real `sk-ant-api03-...` keys, not placeholders. Although `.env` is gitignored and never committed (verified via `git ls-files` and `git log`), the key is at rest in the working tree, will be present in any backups/snapshots/screenshots, and any other tool with FS access can exfiltrate it. **Action: rotate the key immediately at console.anthropic.com, then store the new value via the FileSecretSource pattern (`OFFRAMP_SECRETS_DIR`) instead of `.env`.**

## Warnings (should fix)

1. **SOQL injection vector** in `/Users/sjonas/salesforce/src/offramp/validate/reconcile/resync.py:35,69` and `/Users/sjonas/salesforce/src/offramp/validate/shadow/data_env.py:102`. `sobject` and `record_id` are interpolated directly into the SOQL string with `f"... WHERE Id='{record_id}'"`. If either value originates from CDC payload data without prior validation, an attacker controlling a record id (e.g., via a tampered Pub/Sub stream) can break out of the quote and append clauses. **Action: validate `sobject` against an allowlist (a regex like `^[A-Za-z][A-Za-z0-9_]*(__c)?$` plus describe-cache lookup) and `record_id` against the SF id regex `^[A-Za-z0-9]{15,18}$` before interpolation.** The MCP backend offers no parameter binding, so caller-side validation is the only defense.
2. **`asyncio.get_running_loop().run_in_executor(None, ...)` with positional args wrapped in `lambda`** in `/Users/sjonas/salesforce/src/offramp/mcp/sf_backend.py:70-82,94,101,108,116,124`. Using the default executor (a fixed-size thread pool) for blocking SF SDK calls is fine, but the gateway's per-process quota guard runs *before* `connect()`, which can itself block on the JWT exchange — since `_jwt_session_id` raises `NotImplementedError` today, this is latent and only bites in Phase 5. **Action: when wiring real auth, ensure the JWT exchange is async (httpx) so the executor pool isn't starved by handshakes.**
3. **No CI gate for dependency vulnerabilities.** `pyproject.toml` has no `pip-audit`/`safety`/`uv pip audit` step. Pre-commit covers gitleaks + detect-private-key but not CVE scanning. **Action: add `uv tool run pip-audit` to the CI pipeline (couldn't run locally due to a `pip-audit`/Python 3.13 ensurepip ABRT — needs investigation).**
4. **`make smoke` and `make refresh-fixtures` are stubs.** Makefile lines 38-40 and 44-46 either run mocked code or `exit 1`. The `smoke` target advertises end-to-end coverage but exercises in-memory backends only. **Action: align target description with reality (rename to `make smoke-mock`) until the scratch-org wiring lands.**
5. **`out/` directory contains generated artifacts checked-in working tree but not gitignored content** is fine — but `out/cli_artifact/tier3/build_*.py` is generated Python that gets imported by tests. If a malicious extract payload could influence the generator output (e.g., via an Apex annotation field), this becomes a code-execution sink. **Action: confirm the generator's `_safe_id` sanitizer (`src/offramp/generate/tier1.py:40` etc.) handles every component-name input path; consider also linting generated output before exec.**

## Suggestions (nice to have)

1. **Bare `except Exception as exc` in 7 sites** (`src/offramp/extract/categories/_passthrough.py:39`, `src/offramp/runtime/rules/engine.py:58`, `src/offramp/cutover/saga.py:136`, `src/offramp/validate/reconcile/resync.py:38,72`, `src/offramp/understand/annotate.py:231`, `src/offramp/validate/shadow/data_env.py:105`). All log + degrade gracefully, which is the right shape for boundary code, but consider narrowing where possible (`asyncpg.PostgresError`, `httpx.HTTPError`, `ValidationError`) so genuine bugs surface as crashes.
2. **`assert self._pool is not None` repeated 12 times** in `src/offramp/validate/shadow/store.py`. The `connect()` call before each guarantees it, so consider a `@property pool` with a single assertion or restructure to remove the doubled `await self.connect()` + assertion pattern.
3. **`print()` in CLI** (`src/offramp/cli/shadow.py`, `src/offramp/cli/cutover.py`, `src/offramp/cli/generate.py` — 13 sites). CLAUDE.md mandates "never bare `print()`" but CLI human-output is the conventional exception. Consider documenting this exception in CLAUDE.md or routing through a structured `cli_emit()` helper.
4. **Mixed-DML test coverage** (CLAUDE.md pitfall #3) lives in `tests/ooe_runtime/test_cascade_and_mixed_dml.py` — confirmed it exists and the suite passes (163/163 tests). Worth adding a test gate that fails CI if the OoE test count drops below the v2.1 plan §18.3 baseline of 200, since the file currently has fewer than that.
5. **Function size**: `categorize()` in `src/offramp/validate/shadow/categorize.py:29` is 103 lines — split by category for readability.
6. **`uv pip audit` is unreachable** because `pip` isn't in the venv (uv-managed) — adding `uv tool run pip-audit` to the Makefile would let developers run it locally.
7. **`/Users/sjonas/salesforce/.DS_Store`** (root and `src/`) — these are gitignored so they won't be committed, but worth removing from the working tree before sharing the directory.
8. **CLAUDE.md is accurate** — verified against actual file structure, ADs reference correct architecture sections, and the listed dependency versions match `pyproject.toml`.

## Metrics
- Files reviewed: 115 source, 41 test (156 Python total)
- Test count: 163 collected, 163 passing (unit only; integration/load not run)
- Ruff violations: 0 (`All checks passed!`)
- Mypy violations: 0 (strict, 154 source files)
- Security issues: 1 critical (live API key in `.env`), 5 warning (SOQL injection, blocking auth on default executor, no CVE scan in CI, misleading smoke target, generated-code injection sink)
