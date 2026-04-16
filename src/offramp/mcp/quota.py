"""AD-24: org-wide API quota allocator for the MCP gateway.

Salesforce's per-edition daily API limit is shared across ALL integrations,
all users, and every component the runtime calls. Without per-process
allocation, one runaway shadow run can starve cutover writes.

The allocator polls ``/limits`` on a schedule, splits the remaining budget
across registered processes by configured weight, and rejects calls that
would push a process over its share. Phase 3 ships the allocator + a
test-friendly in-memory ``LimitsSource``; the production REST poller lands
when the real Salesforce backend is wired (Phase 5 deploy).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from offramp.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class LimitsSnapshot:
    """One ``/limits`` response."""

    daily_api_requests_max: int
    daily_api_requests_remaining: int
    snapshot_at: float  # monotonic time


class LimitsSource(Protocol):
    """How the allocator finds out about current org-wide quota."""

    async def fetch(self) -> LimitsSnapshot: ...


@dataclass
class StaticLimitsSource:
    """Test-friendly source — returns a configured snapshot."""

    daily_max: int
    remaining_provider: Callable[[], int]

    async def fetch(self) -> LimitsSnapshot:
        return LimitsSnapshot(
            daily_api_requests_max=self.daily_max,
            daily_api_requests_remaining=self.remaining_provider(),
            snapshot_at=time.monotonic(),
        )


class QuotaExhausted(RuntimeError):
    """Process attempted to make a call but its allocation is exhausted."""


@dataclass
class ProcessAllocation:
    """One process's slice of the org-wide budget."""

    process_id: str
    weight: float = 1.0
    consumed: int = 0


@dataclass
class QuotaAllocator:
    """Per-process API budget enforcement.

    Backed by:
    * a :class:`LimitsSource` that reports org-wide remaining capacity
    * a per-process registration with a ``weight`` that determines its
      proportional share
    * a poll loop (caller-driven via :meth:`refresh`) — Phase 5 will run it
      as a background task when the gateway is deployed
    """

    source: LimitsSource
    poll_interval_s: float = 60.0
    _snapshot: LimitsSnapshot | None = None
    _allocations: dict[str, ProcessAllocation] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def register(self, process_id: str, *, weight: float = 1.0) -> None:
        if weight <= 0:
            raise ValueError("weight must be positive")
        self._allocations[process_id] = ProcessAllocation(process_id=process_id, weight=weight)

    async def refresh(self) -> LimitsSnapshot:
        """Pull a fresh ``/limits`` snapshot."""
        snap = await self.source.fetch()
        async with self._lock:
            self._snapshot = snap
        log.debug(
            "mcp.quota.refreshed",
            remaining=snap.daily_api_requests_remaining,
            max=snap.daily_api_requests_max,
        )
        return snap

    async def remaining_for(self, process_id: str) -> int:
        """Return how many calls ``process_id`` may still make right now."""
        async with self._lock:
            if self._snapshot is None:
                # Caller forgot to refresh — be cautious and reject.
                return 0
            alloc = self._allocations.get(process_id)
            if alloc is None:
                return 0
            total_weight = sum(a.weight for a in self._allocations.values())
            share = int(self._snapshot.daily_api_requests_remaining * (alloc.weight / total_weight))
            return max(share - alloc.consumed, 0)

    async def consume(self, process_id: str, n: int = 1) -> None:
        """Account for ``n`` calls about to be made; raise if exhausted."""
        remaining = await self.remaining_for(process_id)
        if remaining < n:
            raise QuotaExhausted(
                f"process {process_id!r} would exceed its quota share "
                f"(remaining={remaining}, requested={n})"
            )
        async with self._lock:
            self._allocations[process_id].consumed += n

    async def with_budget(
        self,
        process_id: str,
        fn: Callable[[], Awaitable[object]],
        *,
        cost: int = 1,
    ) -> object:
        """Run ``fn()`` after charging ``cost`` calls against ``process_id``."""
        await self.consume(process_id, cost)
        return await fn()


def utilization_metrics(allocator: QuotaAllocator) -> dict[str, dict[str, float]]:
    """Snapshot per-process utilization for the observability stack.

    Reads internal state without taking the lock — safe at observation time
    because we only need a best-effort reading.
    """
    snap = allocator._snapshot
    out: dict[str, dict[str, float]] = {}
    if snap is None:
        return out
    total_weight = sum(a.weight for a in allocator._allocations.values()) or 1
    for pid, alloc in allocator._allocations.items():
        share = snap.daily_api_requests_remaining * (alloc.weight / total_weight)
        out[pid] = {
            "weight": alloc.weight,
            "consumed": alloc.consumed,
            "share": share,
            "utilization": (alloc.consumed / share) if share > 0 else 1.0,
        }
    return out
