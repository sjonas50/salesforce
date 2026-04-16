"""Pub/Sub subscriber lag monitor (AD-21).

Tracks how far behind ``last_event_at`` for a process is. If it exceeds the
configured threshold (default 60 hours, leaving 12h headroom before the
72h Pub/Sub replay-id retention cliff), trigger reconciliation: REST-based
full record re-fetch + replay-id reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from offramp.core.logging import get_logger
from offramp.validate.shadow.store import ShadowStore

log = get_logger(__name__)


@dataclass(frozen=True)
class LagSnapshot:
    process_id: str
    last_event_at: datetime | None
    lag_hours: float | None
    threshold_hours: int
    needs_reconciliation: bool

    @property
    def status(self) -> str:
        if self.last_event_at is None:
            return "no_events_yet"
        if self.needs_reconciliation:
            return "reconciliation_required"
        return "healthy"


@dataclass
class LagMonitor:
    """Polls the shadow store for replay-state freshness."""

    store: ShadowStore
    threshold_hours: int = 60

    async def snapshot(self, process_id: str) -> LagSnapshot:
        state = await self.store.get_replay_state(process_id)
        if state is None or state["last_event_at"] is None:
            return LagSnapshot(
                process_id=process_id,
                last_event_at=None,
                lag_hours=None,
                threshold_hours=self.threshold_hours,
                needs_reconciliation=False,
            )
        last = state["last_event_at"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        lag = (datetime.now(UTC) - last) / timedelta(hours=1)
        needs = lag > self.threshold_hours
        return LagSnapshot(
            process_id=process_id,
            last_event_at=last,
            lag_hours=lag,
            threshold_hours=self.threshold_hours,
            needs_reconciliation=needs,
        )
