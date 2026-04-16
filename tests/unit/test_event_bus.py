"""Phase 0.9: in-memory event bus contract."""

from __future__ import annotations

import asyncio

import pytest

from offramp.event_bus.in_memory import InMemoryEventBus
from offramp.event_bus.redis_streams import RedisStreamsEventBus


@pytest.mark.asyncio
async def test_publish_then_subscribe_delivers_event() -> None:
    bus = InMemoryEventBus()

    async def consume() -> dict[str, object]:
        async for ev in bus.subscribe("topic.x", consumer="c1", block_ms=500):
            return ev.payload
        return {}

    consumer_task = asyncio.create_task(consume())
    # Yield once so the consumer is registered before we publish.
    await asyncio.sleep(0.01)
    await bus.publish("topic.x", {"k": "v"})
    payload = await asyncio.wait_for(consumer_task, timeout=1.0)
    assert payload == {"k": "v"}


@pytest.mark.asyncio
async def test_subscribe_returns_when_idle() -> None:
    bus = InMemoryEventBus()
    received: list[dict[str, object]] = []
    async for ev in bus.subscribe("idle.topic", consumer="c2", block_ms=50):
        received.append(ev.payload)
    assert received == []


@pytest.mark.asyncio
async def test_redis_backend_publish_not_yet_implemented() -> None:
    bus = RedisStreamsEventBus(url="redis://localhost:6379")
    with pytest.raises(NotImplementedError):
        await bus.publish("anything", {})
