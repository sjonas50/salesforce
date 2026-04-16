"""AD-24 quota allocator — per-process budgets + utilization metrics."""

from __future__ import annotations

import pytest

from offramp.mcp.quota import (
    QuotaAllocator,
    QuotaExhausted,
    StaticLimitsSource,
    utilization_metrics,
)


@pytest.mark.asyncio
async def test_unrefreshed_allocator_rejects_calls() -> None:
    alloc = QuotaAllocator(
        source=StaticLimitsSource(daily_max=1000, remaining_provider=lambda: 800)
    )
    alloc.register("p1")
    # No refresh() yet → snapshot is None → remaining_for is 0.
    assert await alloc.remaining_for("p1") == 0
    with pytest.raises(QuotaExhausted):
        await alloc.consume("p1", 1)


@pytest.mark.asyncio
async def test_equal_weight_split() -> None:
    alloc = QuotaAllocator(
        source=StaticLimitsSource(daily_max=1000, remaining_provider=lambda: 1000)
    )
    alloc.register("p1")
    alloc.register("p2")
    await alloc.refresh()
    assert await alloc.remaining_for("p1") == 500
    assert await alloc.remaining_for("p2") == 500


@pytest.mark.asyncio
async def test_weighted_split() -> None:
    alloc = QuotaAllocator(
        source=StaticLimitsSource(daily_max=1000, remaining_provider=lambda: 600)
    )
    alloc.register("hot", weight=4.0)
    alloc.register("cold", weight=1.0)
    await alloc.refresh()
    # 600 * (4/5) = 480, 600 * (1/5) = 120
    assert await alloc.remaining_for("hot") == 480
    assert await alloc.remaining_for("cold") == 120


@pytest.mark.asyncio
async def test_consume_decrements_remaining() -> None:
    alloc = QuotaAllocator(
        source=StaticLimitsSource(daily_max=1000, remaining_provider=lambda: 100)
    )
    alloc.register("p1")
    await alloc.refresh()
    await alloc.consume("p1", 30)
    assert await alloc.remaining_for("p1") == 70


@pytest.mark.asyncio
async def test_quota_exhausted_when_over_share() -> None:
    alloc = QuotaAllocator(source=StaticLimitsSource(daily_max=1000, remaining_provider=lambda: 50))
    alloc.register("p1", weight=1.0)
    alloc.register("p2", weight=1.0)
    await alloc.refresh()
    # p1's share is 25; 30 calls should fail.
    with pytest.raises(QuotaExhausted):
        await alloc.consume("p1", 30)


@pytest.mark.asyncio
async def test_with_budget_charges_then_runs() -> None:
    alloc = QuotaAllocator(
        source=StaticLimitsSource(daily_max=1000, remaining_provider=lambda: 100)
    )
    alloc.register("p1")
    await alloc.refresh()
    ran: list[bool] = []

    async def fn():
        ran.append(True)
        return "ok"

    out = await alloc.with_budget("p1", fn, cost=2)
    assert out == "ok"
    assert ran == [True]
    assert await alloc.remaining_for("p1") == 98


@pytest.mark.asyncio
async def test_utilization_metrics_shape() -> None:
    alloc = QuotaAllocator(
        source=StaticLimitsSource(daily_max=1000, remaining_provider=lambda: 200)
    )
    alloc.register("p1")
    await alloc.refresh()
    await alloc.consume("p1", 50)
    metrics = utilization_metrics(alloc)
    assert "p1" in metrics
    assert metrics["p1"]["consumed"] == 50
    assert 0 < metrics["p1"]["utilization"] < 1
