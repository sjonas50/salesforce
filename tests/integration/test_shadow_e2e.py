"""Phase 4 end-to-end test against real Postgres + the OoE runtime.

Drives the full shadow pipeline:
  synthetic CDC stream -> shadow executor -> diff -> categorize -> Postgres
                                                                -> readiness
                                                                -> dashboard
                                                                -> compliance export

Requires the offramp-postgres container (started during Phase 4 setup) on
``localhost:5432`` with the ``offramp_shadow`` database.

Also exercises:
* AD-21 lag monitor with a forced-ancient last_event_at
* AD-22 gap-event categorization round-trip via the synthetic source
* Compare Mode replay through the same shadow store
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any

import pytest

from offramp.engram.client import InMemoryEngramClient
from offramp.runtime.ooe.state_machine import OoERuntime
from offramp.runtime.rules.engine import Rule, RulesEngine
from offramp.validate.compare_mode.log_parser import parse as parse_log
from offramp.validate.compare_mode.replay_harness import ReplayHarness
from offramp.validate.compare_mode.state_reconstructor import StateReconstructor
from offramp.validate.reconcile.lag_monitor import LagMonitor
from offramp.validate.shadow.cdc_event import ChangeType
from offramp.validate.shadow.compliance import export_compliance_report
from offramp.validate.shadow.dashboard import render_dashboard
from offramp.validate.shadow.data_env import ForkedDataEnv
from offramp.validate.shadow.executor import ShadowExecutor
from offramp.validate.shadow.readiness import ReadinessScorer
from offramp.validate.shadow.store import open_store
from offramp.validate.shadow.synthetic import SyntheticSource


def _postgres_reachable() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _postgres_reachable(), reason="Postgres not reachable on :5432"),
]

SHADOW_DSN = os.environ.get(
    "POSTGRES_SHADOW_DSN", "postgresql://offramp:offramp@localhost:5432/offramp_shadow"
)


def _engine_with_email_required() -> RulesEngine:
    """Tier 1 rule registry: Lead must have a non-blank Email."""
    engine = RulesEngine()

    def vr(record: dict[str, Any], _ctx: dict[str, Any]) -> bool:
        # Returns True when the validation FAILS (i.e. Email is blank).
        e = record.get("Email")
        return e is None or (isinstance(e, str) and e.strip() == "")

    engine.register(
        Rule(
            rule_id="Lead.HasEmail",
            sobject="Lead",
            ooe_step=6,  # CUSTOM_VALIDATION
            fn=vr,
            kind="validation",
            error_message_template="Email is required.",
        )
    )
    return engine


@pytest.mark.asyncio
async def test_shadow_executor_records_clean_pass() -> None:
    """Lead with email — shadow comparison reports no divergence."""
    process_id = f"test_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    runtime = OoERuntime(rules=_engine_with_email_required())

    src = SyntheticSource()
    src.register_entity("Lead", {"Email": "string", "Status": "string"})
    src.add_create("Lead", "00Q1", {"Email": "x@example.com", "Status": "New"})

    async with open_store(SHADOW_DSN) as store:
        await store.reset()

        async def no_prod_read(_s: str, _r: str) -> dict[str, Any] | None:
            return None

        executor = ShadowExecutor(
            process_id=process_id,
            runtime=runtime,
            store=store,
            engram=engram,
            data_env_factory=lambda: ForkedDataEnv(
                store=store, production_read=no_prod_read, process_id=process_id
            ),
        )
        events = [ev async for ev in src.stream(topics=[])]
        outcomes = [await executor.execute_event(ev) for ev in events]

        assert len(outcomes) == 1
        assert outcomes[0].diverged is False

        scorer = ReadinessScorer(
            store=store, min_events_for_eligibility=1, eligibility_threshold=90
        )
        score = await scorer.score(process_id)
        assert score.total_events == 1
        assert score.diverged_events == 0
        assert score.score >= 95


@pytest.mark.asyncio
async def test_shadow_executor_flags_translation_error_when_runtime_aborts() -> None:
    """Lead without email — runtime aborts; shadow records a divergence."""
    process_id = f"test_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    runtime = OoERuntime(rules=_engine_with_email_required())

    src = SyntheticSource()
    src.register_entity("Lead", {"Email": "string", "Status": "string"})
    src.add_create("Lead", "00Q1", {"Email": None, "Status": "New"})

    async with open_store(SHADOW_DSN) as store:
        await store.reset()

        async def no_prod_read(_s: str, _r: str) -> dict[str, Any] | None:
            return None

        executor = ShadowExecutor(
            process_id=process_id,
            runtime=runtime,
            store=store,
            engram=engram,
            data_env_factory=lambda: ForkedDataEnv(
                store=store, production_read=no_prod_read, process_id=process_id
            ),
        )
        events = [ev async for ev in src.stream(topics=[])]
        outcomes = [await executor.execute_event(ev) for ev in events]

        assert outcomes[0].diverged is True
        assert outcomes[0].category == "translation_error"


@pytest.mark.asyncio
async def test_gap_event_routes_to_ad22_category() -> None:
    process_id = f"test_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    runtime = OoERuntime(rules=RulesEngine())

    src = SyntheticSource()
    src.register_entity("Account", {"Name": "string"})
    src.add_gap("Account", "001ABC", change_type=ChangeType.GAP_UPDATE)

    async with open_store(SHADOW_DSN) as store:
        await store.reset()

        async def no_prod_read(_s: str, _r: str) -> dict[str, Any] | None:
            return None

        executor = ShadowExecutor(
            process_id=process_id,
            runtime=runtime,
            store=store,
            engram=engram,
            data_env_factory=lambda: ForkedDataEnv(
                store=store, production_read=no_prod_read, process_id=process_id
            ),
        )
        events = [ev async for ev in src.stream(topics=[])]
        outcomes = [await executor.execute_event(ev) for ev in events]

        assert outcomes[0].category == "gap_event_full_refetch_required"
        assert outcomes[0].severity >= 70


@pytest.mark.asyncio
async def test_lag_monitor_flags_old_replay_state() -> None:
    process_id = f"test_{uuid.uuid4().hex[:8]}"
    async with open_store(SHADOW_DSN) as store:
        await store.reset()
        # Force an ancient last_event_at by writing then patching.
        await store.update_replay_state(process_id=process_id, replay_id="0001")
        # Bypass DAO to backdate.
        pool = await store.connect()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE replay_state SET last_event_at = now() - interval '72 hours'"
                " WHERE process_id=$1",
                process_id,
            )

        lag = LagMonitor(store=store, threshold_hours=60)
        snap = await lag.snapshot(process_id)
        assert snap.needs_reconciliation is True
        assert snap.lag_hours is not None and snap.lag_hours >= 60


@pytest.mark.asyncio
async def test_dashboard_and_compliance_export(tmp_path: Path) -> None:
    process_id = f"test_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    runtime = OoERuntime(rules=_engine_with_email_required())

    src = SyntheticSource()
    src.register_entity("Lead", {"Email": "string"})
    # 5 clean + 1 diverged + 1 gap = 7 events.
    for i in range(5):
        src.add_create("Lead", f"00Q{i}", {"Email": f"a{i}@x"})
    src.add_create("Lead", "00QFAIL", {"Email": None})  # validation will fire
    src.add_gap("Lead", "00QGAP")

    async with open_store(SHADOW_DSN) as store:
        await store.reset()

        async def no_prod_read(_s: str, _r: str) -> dict[str, Any] | None:
            return None

        executor = ShadowExecutor(
            process_id=process_id,
            runtime=runtime,
            store=store,
            engram=engram,
            data_env_factory=lambda: ForkedDataEnv(
                store=store, production_read=no_prod_read, process_id=process_id
            ),
        )
        events = [ev async for ev in src.stream(topics=[])]
        for ev in events:
            await executor.execute_event(ev)

        scorer = ReadinessScorer(
            store=store, min_events_for_eligibility=1, eligibility_threshold=50
        )
        lag = LagMonitor(store=store)
        await render_dashboard(
            process_id=process_id,
            store=store,
            scorer=scorer,
            lag=lag,
            out_path=tmp_path / "dashboard.html",
        )
        assert (tmp_path / "dashboard.html").stat().st_size > 1024

        result = await export_compliance_report(
            process_id=process_id,
            store=store,
            scorer=scorer,
            lag=lag,
            engram=engram,
            out_path=tmp_path / "compliance.json",
        )
        payload = json.loads((tmp_path / "compliance.json").read_text())
        assert payload["schema_version"] == "1.0"
        assert payload["readiness"]["total_events"] == 7
        assert payload["readiness"]["diverged_events"] == 2  # validation + gap
        assert result.divergences_exported == 7


@pytest.mark.asyncio
async def test_compare_mode_replay_records_findings() -> None:
    process_id = f"test_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    runtime = OoERuntime(rules=_engine_with_email_required())

    log_text = (
        "13:00:00.000 (1000)|EXECUTION_STARTED\n"
        "13:00:00.001 (1001)|USER_INFO|[EXTERNAL]|0050000000000ABC|english|EST|UTF-8\n"
        "13:00:00.020 (20000)|DML_BEGIN|[123]|Op:Insert|Type:Lead|Rows:1\n"
        "13:00:00.040 (40000)|VALIDATION_FAIL|VALIDATION_FAIL|Name:Lead.HasEmail|Email is required\n"
        "13:00:00.090 (90000)|EXECUTION_FINISHED\n"
    )
    transactions, stats = parse_log(log_text)
    assert stats.transactions == 1

    async with open_store(SHADOW_DSN) as store:
        await store.reset()
        reconstructor = StateReconstructor(store=store)
        harness = ReplayHarness(
            runtime=runtime,
            reconstructor=reconstructor,
            store=store,
            engram=engram,
            process_id=process_id,
        )
        outcomes = []
        for txn in transactions:
            outcomes.extend(await harness.replay(txn))

        # Runtime fires Lead.HasEmail (record has no email), log also fires it.
        # → no divergence.
        assert all(not o.diverged for o in outcomes)


# Sanity-check that asyncio is happy when running under pytest-asyncio.
def test_imports_work() -> None:
    assert asyncio is not None
