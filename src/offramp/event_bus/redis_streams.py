"""Redis Streams backend (dev / scratch infra).

The wire calls sit behind a clear NotImplementedError so callers get a
helpful message until the redis client is added. The full implementation
lands when the first cross-component message ships (extract →
understand handoff).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from offramp.event_bus.base import Event


class RedisStreamsEventBus:
    """Redis Streams backed event bus.

    Configuration is read from :class:`offramp.core.config.InfraSettings`.
    """

    def __init__(self, url: str) -> None:
        self.url = url

    async def publish(self, topic: str, payload: dict[str, Any]) -> Event:
        raise NotImplementedError(
            "RedisStreamsEventBus.publish is not implemented — add `redis>=5` to "
            "deps and wire the client. Use `InMemoryEventBus` for tests until then."
        )

    def subscribe(
        self,
        topic: str,
        *,
        consumer: str,
        block_ms: int = 1000,
    ) -> AsyncIterator[Event]:
        raise NotImplementedError(
            "RedisStreamsEventBus.subscribe is not implemented (see publish)."
        )
