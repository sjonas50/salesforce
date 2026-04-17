"""Phase 5.12 dry-run cutover: 0 -> 1 -> 5 -> 25 -> 50 -> 100 with simulated rollbacks.

Drives the full Phase 5 surface against real Postgres:
* RoutingTable upserts to Postgres
* CutoverOrchestrator decides advance / hold / rollback / immediate-rollback
* Saga compensation runs in reverse on rollbacks
* Engram + F44 anchored every transition
* Post-cutover monitor detects regression after stage 100

Requires the offramp-postgres container on localhost:5432.
"""

from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from offramp.cutover.orchestrator import CutoverOrchestrator, TransitionKind
from offramp.cutover.post_cutover_monitor import PostCutoverMonitor
from offramp.cutover.provenance import CutoverProvenance
from offramp.cutover.router import RoutingConfig
from offramp.cutover.saga import (
    ActivitySpec,
    CompensationKind,
    SagaTransaction,
)
from offramp.engram.client import InMemoryEngramClient
from offramp.mcp.routing import RoutingTable
from offramp.validate.shadow.readiness import ReadinessScorer
from offramp.validate.shadow.store import open_store


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


ROUTING_DSN = "postgresql://offramp:offramp@localhost:5432/offramp"
SHADOW_DSN = "postgresql://offramp:offramp@localhost:5432/offramp_shadow"


async def _seed_clean_window(store, *, process_id: str, n: int = 200) -> None:
    """Insert clean readiness events so the scorer reports score >= 98."""
    for i in range(n):
        await store.write_divergence(
            process_id=process_id,
            replay_id=f"seed_{i:06d}",
            diverged=False,
            category=None,
            field_diffs={},
            trace={},
            anchor_id=None,
            severity=0,
        )


async def _seed_dirty_window(
    store,
    *,
    process_id: str,
    n: int = 200,
    dirty_pct: int = 30,
    severity: int = 50,
) -> None:
    """Insert events with `dirty_pct`% diverged at the given severity.

    Score formula (from ReadinessScorer): clean_rate*100 - (dirty/total) * (avg_sev * 0.5).
    Pick (dirty_pct, severity) to land in the score band you want:
      * 5%  / 50 -> score ~94 -> ROLLBACK   (<95, >=90)
      * 20% / 80 -> score ~72 -> IMMEDIATE_ROLLBACK (<90)
      * 100%/ 80 -> score ~60 -> IMMEDIATE_ROLLBACK
    """
    for i in range(n):
        diverged = (i % 100) < dirty_pct
        await store.write_divergence(
            process_id=process_id,
            replay_id=f"dirty_{i:06d}",
            diverged=diverged,
            category="translation_error" if diverged else None,
            field_diffs={"Status": ["Approved", "Pending"]} if diverged else {},
            trace={},
            anchor_id=None,
            severity=severity if diverged else 0,
        )


async def _backdate_routing_so_dwell_complete(routing: RoutingTable, process_id: str) -> None:
    """Force the in-memory + Postgres entered_stage_at into the past."""
    cfg = await routing.get_config(process_id)
    assert cfg is not None
    pool = await routing.connect()
    long_ago = datetime.now(UTC) - timedelta(hours=72)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE routing_config SET entered_stage_at=$1 WHERE process_id=$2",
            long_ago,
            process_id,
        )
    # Reload so the in-memory snapshot picks up the backdate.
    await routing.reload()


@pytest.mark.asyncio
async def test_full_dry_run_progression_with_rollback_and_resume() -> None:
    """1->5->25->50->100, with a rollback at 25% and an instant rollback at 100%."""
    process_id = f"dry_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    routing = RoutingTable(dsn=ROUTING_DSN)
    await routing.connect()
    await routing.reload()
    try:
        async with open_store(SHADOW_DSN) as store:
            await store.reset()
            scorer = ReadinessScorer(
                store=store, min_events_for_eligibility=50, eligibility_threshold=98
            )
            provenance = CutoverProvenance(engram=engram)
            orch = CutoverOrchestrator(routing=routing, scorer=scorer, provenance=provenance)

            # === Phase 1: begin ===
            begin = await orch.begin(process_id=process_id)
            assert begin["began"] is True
            assert begin["config"]["stage_percent"] == 1

            # Seed a clean readiness window + advance dwell.
            await _seed_clean_window(store, process_id=process_id, n=120)
            await _backdate_routing_so_dwell_complete(routing, process_id)

            # === Phase 2: advance 1 -> 5 ===
            decision = await orch.evaluate(process_id)
            assert decision.kind is TransitionKind.ADVANCE
            assert decision.from_percent == 1 and decision.to_percent == 5
            outcome = await orch.apply(decision)
            assert outcome["applied"] is True
            assert outcome["to_percent"] == 5

            # === Phase 3: advance 5 -> 25 ===
            await _backdate_routing_so_dwell_complete(routing, process_id)
            decision = await orch.evaluate(process_id)
            assert decision.kind is TransitionKind.ADVANCE
            assert decision.to_percent == 25
            await orch.apply(decision)

            # === Phase 4: simulate divergence -> rollback 25 -> 5 ===
            # Mild divergence (5% dirty at severity 50) -> score ~94, in the
            # ROLLBACK band (>=90, <95).
            await store.reset()
            await _seed_dirty_window(store, process_id=process_id, n=200, dirty_pct=5, severity=50)
            decision = await orch.evaluate(process_id)
            assert decision.kind is TransitionKind.ROLLBACK, (
                f"expected ROLLBACK; got {decision.kind} (score {decision.readiness_score})"
            )
            assert decision.to_percent == 5
            outcome = await orch.apply(decision)
            assert outcome["applied"] is True
            assert outcome["to_percent"] == 5

            # === Phase 5: clean again -> resume to 100 ===
            await store.reset()
            await _seed_clean_window(store, process_id=process_id, n=200)
            for target in (25, 50, 100):
                await _backdate_routing_so_dwell_complete(routing, process_id)
                decision = await orch.evaluate(process_id)
                assert decision.kind is TransitionKind.ADVANCE, (
                    f"expected advance at target {target}, got {decision.kind}: {decision.reason}"
                )
                await orch.apply(decision)

            cfg = await routing.get_config(process_id)
            assert cfg is not None
            assert cfg.stage_percent == 100

            # === Phase 6: post-cutover regression -> instant rollback ===
            await store.reset()
            # Score will plummet — 100% diverged.
            await _seed_dirty_window(store, process_id=process_id, n=200, dirty_pct=100)

            monitor = PostCutoverMonitor(
                routing=routing,
                scorer=scorer,
                provenance=provenance,
                orchestrator=orch,
                regression_threshold=95,
                auto_rollback=True,
            )
            alert = await monitor.check(process_id)
            assert alert is not None
            assert alert.auto_rollback_triggered is True

            cfg = await routing.get_config(process_id)
            assert cfg is not None
            assert cfg.stage_percent == 0  # instant rollback to zero
    finally:
        await routing.close()


@pytest.mark.asyncio
async def test_immediate_rollback_requires_human_confirm() -> None:
    """Score < 90 produces an immediate_rollback decision that must be confirmed."""
    process_id = f"imm_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    routing = RoutingTable(dsn=ROUTING_DSN)
    await routing.connect()
    await routing.reload()
    try:
        async with open_store(SHADOW_DSN) as store:
            await store.reset()
            scorer = ReadinessScorer(
                store=store, min_events_for_eligibility=50, eligibility_threshold=98
            )
            provenance = CutoverProvenance(engram=engram)
            orch = CutoverOrchestrator(routing=routing, scorer=scorer, provenance=provenance)
            await orch.begin(process_id=process_id)
            await _seed_dirty_window(store, process_id=process_id, n=200, dirty_pct=80)

            decision = await orch.evaluate(process_id)
            assert decision.kind is TransitionKind.IMMEDIATE_ROLLBACK
            assert decision.requires_human_signoff is True
            assert decision.to_percent == 0

            # Without confirmed=True, apply refuses.
            outcome = await orch.apply(decision, confirmed=False)
            assert outcome["applied"] is False

            # With confirmed=True, applies.
            outcome = await orch.apply(decision, confirmed=True)
            assert outcome["applied"] is True
            cfg = await routing.get_config(process_id)
            assert cfg is not None
            assert cfg.stage_percent == 0
    finally:
        await routing.close()


@pytest.mark.asyncio
async def test_saga_pause_blocks_rollback_until_confirmed() -> None:
    """A saga with a REQUIRES_HUMAN action pauses rollback until --confirm."""
    process_id = f"saga_{uuid.uuid4().hex[:8]}"
    engram = InMemoryEngramClient()
    routing = RoutingTable(dsn=ROUTING_DSN)
    await routing.connect()
    await routing.reload()
    try:
        async with open_store(SHADOW_DSN) as store:
            await store.reset()
            scorer = ReadinessScorer(
                store=store, min_events_for_eligibility=50, eligibility_threshold=98
            )
            provenance = CutoverProvenance(engram=engram)
            orch = CutoverOrchestrator(routing=routing, scorer=scorer, provenance=provenance)
            await orch.begin(process_id=process_id)
            # Mild dirty window (5%/sev50) -> ROLLBACK; saga must pause before applying.
            await _seed_dirty_window(store, process_id=process_id, n=200, dirty_pct=5, severity=50)

            saga = SagaTransaction(saga_id="s1")
            saga.record(
                ActivitySpec(
                    name="email_to_customer",
                    compensation_kind=CompensationKind.REQUIRES_HUMAN,
                ),
                {},
                {},
            )

            decision = await orch.evaluate(process_id)
            assert decision.kind is TransitionKind.ROLLBACK
            outcome = await orch.apply(decision, saga=saga)
            assert outcome["applied"] is False
            assert "saga compensation incomplete" in outcome["reason"]

            # Caller acknowledges + retries with confirmed=True -> applies.
            outcome2 = await orch.apply(decision, saga=saga, confirmed=True)
            assert outcome2["applied"] is True
    finally:
        await routing.close()


@pytest.mark.asyncio
async def test_routing_table_persists_across_reload() -> None:
    """RoutingTable.reload() pulls config from Postgres after a fresh connection."""
    process_id = f"persist_{uuid.uuid4().hex[:8]}"
    routing = RoutingTable(dsn=ROUTING_DSN)
    await routing.connect()
    try:
        await routing.upsert(process_id=process_id, stage_percent=25, hash_seed="seed")
    finally:
        await routing.close()

    fresh = RoutingTable(dsn=ROUTING_DSN)
    await fresh.connect()
    try:
        await fresh.reload()
        cfg = await fresh.get_config(process_id)
        assert cfg is not None
        assert cfg.stage_percent == 25
        assert cfg.hash_seed == "seed"
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_per_record_routing_uses_persisted_config() -> None:
    """End-to-end: routing decisions reflect the persisted stage_percent."""
    process_id = f"route_{uuid.uuid4().hex[:8]}"
    routing = RoutingTable(dsn=ROUTING_DSN)
    await routing.connect()
    try:
        await routing.upsert(process_id=process_id, stage_percent=50, hash_seed="abc")
        rt_count = 0
        for i in range(500):
            if await routing.route(process_id, f"r{i}") == "runtime":
                rt_count += 1
        assert 200 < rt_count < 300  # roughly 50%
    finally:
        await routing.close()


@pytest.mark.asyncio
async def test_routing_default_safe_when_no_config() -> None:
    """Unknown process IDs always route to salesforce (the safe default)."""
    routing = RoutingTable(dsn=ROUTING_DSN)
    await routing.connect()
    try:
        await routing.reload()
        target = await routing.route("nonexistent_process", "any_record")
        assert target == "salesforce"
    finally:
        await routing.close()


# Suppress unused import warnings — we wire RoutingConfig only as a typing hint.
_ = RoutingConfig
