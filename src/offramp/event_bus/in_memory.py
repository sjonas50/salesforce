"""In-process event bus used by tests.

Maintains one ``asyncio.Queue`` per (topic, consumer) so subscribers see every
event published after they subscribe. Not durable. Not multi-process.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from offramp.event_bus.base import Event, now


class InMemoryEventBus:
    """Single-process pub/sub for unit tests and ``make smoke``."""

    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], asyncio.Queue[Event]] = defaultdict(asyncio.Queue)
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, payload: dict[str, Any]) -> Event:
        event = Event(
            topic=topic,
            payload=payload,
            published_at=now(),
            message_id=uuid4().hex,
        )
        async with self._lock:
            for (t, _consumer), q in self._queues.items():
                if t == topic:
                    await q.put(event)
        return event

    async def subscribe(
        self,
        topic: str,
        *,
        consumer: str,
        block_ms: int = 1000,
    ) -> AsyncIterator[Event]:
        # Eagerly create the queue so messages published after subscribe
        # but before the first __anext__ are still received.
        async with self._lock:
            queue = self._queues[(topic, consumer)]
        timeout = block_ms / 1000.0
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                # Surface as a clean stop so callers can poll.
                return
