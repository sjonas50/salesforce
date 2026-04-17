"""Load benchmarks for Phase 5 gate (architecture §17.4 / build-plan 5.11).

Targets:
* MCP gateway p50 < 50ms, p99 < 200ms (against in-memory backend)
* Rules engine p50 < 10ms, p99 < 50ms
* Throughput: 10K transactions per hour MVP

These tests run only when `pytest -m load` is requested so they don't slow
the default suite. Run benchmark output to console for inspection.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from offramp.engram.client import InMemoryEngramClient
from offramp.extract.ooe_audit.audit import OoEStep
from offramp.mcp.server import InMemorySalesforceBackend, MCPGateway
from offramp.runtime.ooe.state_machine import OoERuntime
from offramp.runtime.rules.engine import Rule, RulesEngine

pytestmark = pytest.mark.load


def _percentile(values: list[float], pct: float) -> float:
    sorted_v = sorted(values)
    k = int(len(sorted_v) * pct / 100)
    return sorted_v[min(k, len(sorted_v) - 1)]


@pytest.mark.asyncio
async def test_mcp_gateway_latency_targets() -> None:
    """MCP p50 < 50ms, p99 < 200ms with in-memory backend."""
    gw = MCPGateway(backend=InMemorySalesforceBackend(), engram=InMemoryEngramClient())
    n = 1000
    latencies: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        await gw.sf_create("Account", {"Name": f"Acme-{i}"})
        latencies.append((time.perf_counter() - t0) * 1000)

    p50 = _percentile(latencies, 50)
    p99 = _percentile(latencies, 99)
    mean = statistics.mean(latencies)
    print(f"\nMCP create: n={n} mean={mean:.2f}ms p50={p50:.2f}ms p99={p99:.2f}ms")

    # The architecture targets are wall-clock for production with real
    # backends. Against the in-memory backend our budget should be tighter;
    # we use the production targets as the upper bound.
    assert p50 < 50, f"p50 {p50:.2f}ms exceeds 50ms target"
    assert p99 < 200, f"p99 {p99:.2f}ms exceeds 200ms target"


def test_rules_engine_latency_targets() -> None:
    """Rules engine p50 < 10ms, p99 < 50ms."""
    engine = RulesEngine()
    engine.register(
        Rule(
            rule_id="Lead.HasEmail",
            sobject="Lead",
            ooe_step=int(OoEStep.CUSTOM_VALIDATION),
            fn=lambda r, c: not r.get("Email"),
            kind="validation",
            error_message_template="Email is required",
        )
    )
    runtime = OoERuntime(rules=engine)
    n = 5000
    latencies: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        runtime.execute_save(sobject="Lead", record={"Email": f"x{i}@y.com"})
        latencies.append((time.perf_counter() - t0) * 1000)
    p50 = _percentile(latencies, 50)
    p99 = _percentile(latencies, 99)
    mean = statistics.mean(latencies)
    print(f"\nRules engine save: n={n} mean={mean:.3f}ms p50={p50:.3f}ms p99={p99:.3f}ms")
    assert p50 < 10, f"p50 {p50:.3f}ms exceeds 10ms target"
    assert p99 < 50, f"p99 {p99:.3f}ms exceeds 50ms target"


def test_throughput_target_10k_txn_per_hour() -> None:
    """Calibration: 10K txn/hr ~ 2.78 txn/s. Measure actual TPS sustained."""
    engine = RulesEngine()
    engine.register(
        Rule(
            rule_id="Account.HasName",
            sobject="Account",
            ooe_step=int(OoEStep.CUSTOM_VALIDATION),
            fn=lambda r, c: not r.get("Name"),
            kind="validation",
        )
    )
    runtime = OoERuntime(rules=engine)
    duration_s = 1.0
    t_end = time.monotonic() + duration_s
    n = 0
    while time.monotonic() < t_end:
        runtime.execute_save(sobject="Account", record={"Name": "Acme"})
        n += 1
    tps = n / duration_s
    txn_per_hour = tps * 3600
    print(
        f"\nThroughput: {n} txn in {duration_s:.2f}s = {tps:.0f} TPS = {txn_per_hour:,.0f} txn/hr"
    )
    assert tps >= 100, f"throughput {tps:.0f} TPS below MVP floor (100 TPS)"


@pytest.mark.asyncio
async def test_concurrent_mcp_throughput() -> None:
    """Concurrent gateway calls — verify the in-memory lock isn't a bottleneck."""
    gw = MCPGateway(backend=InMemorySalesforceBackend(), engram=InMemoryEngramClient())

    async def one_create(i: int) -> None:
        await gw.sf_create("Account", {"Name": f"Concurrent-{i}"})

    n = 500
    t0 = time.perf_counter()
    await asyncio.gather(*(one_create(i) for i in range(n)))
    elapsed = time.perf_counter() - t0
    tps = n / elapsed
    print(f"\nConcurrent MCP: {n} creates in {elapsed:.2f}s = {tps:.0f} TPS")
    assert tps >= 200, f"concurrent throughput {tps:.0f} below 200 TPS floor"
