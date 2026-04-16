"""CDC event source contract — real Pub/Sub or synthetic.

Both backends yield ``CDCEvent`` instances; downstream code depends only on
this Protocol so swap-in is a constructor change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from offramp.validate.shadow.cdc_event import CDCEvent


class CDCSource(Protocol):
    """Async iterable of CDC events for one or more topics."""

    async def stream(self, topics: list[str]) -> AsyncIterator[CDCEvent]:
        """Yield events for the requested topics. Long-running."""

    async def close(self) -> None:
        """Cleanly tear down underlying resources (gRPC channel, etc.)."""

    @property
    def latest_replay_id(self) -> str | None:
        """The last replay_id observed — used by the lag monitor."""
