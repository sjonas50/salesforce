"""Backend-agnostic event bus contract.

Cross-component messages flow through this abstraction. The transport is a
deployment detail (AD-9 in the v2.1 plan) — code that imports from
:mod:`offramp.event_bus.base` works against Redis Streams (dev), Azure Event
Hubs (prod), or NATS (on-prem) interchangeably.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Event:
    """One event on the bus."""

    topic: str
    payload: dict[str, Any]
    published_at: datetime
    message_id: str | None = None  # backend-assigned on publish


class EventBus(Protocol):
    """Async pub/sub contract.

    Backends MUST be safe to use across asyncio tasks (per-call client locking
    where the underlying library is not thread-safe).
    """

    async def publish(self, topic: str, payload: dict[str, Any]) -> Event:
        """Publish a single event to ``topic``."""

    def subscribe(
        self,
        topic: str,
        *,
        consumer: str,
        block_ms: int = 1000,
    ) -> AsyncIterator[Event]:
        """Async-iterate events from ``topic`` for the named ``consumer``.

        Implementations should commit/ack only after the consumer has handled
        the event (use ``async for`` with downstream awaits).
        """


def now() -> datetime:
    """UTC-now helper used by all backends so wire timestamps are consistent."""
    return datetime.now(UTC)
