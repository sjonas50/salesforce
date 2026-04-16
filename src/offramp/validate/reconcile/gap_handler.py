"""Gap-event handler.

When a CDC event arrives with a ``GAP_*`` change_type the field-level
deltas have been dropped by Salesforce — we cannot reconstruct the change
from the event itself. The handler triggers a full record re-fetch via
the resync path so the shadow store catches up.

GAP_OVERFLOW means the channel was overwhelmed; we re-fetch every
``record_id`` listed in the event header and reset the replay-id to the
event after the gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from offramp.core.logging import get_logger
from offramp.validate.reconcile.resync import Resyncer
from offramp.validate.shadow.cdc_event import CDCEvent

log = get_logger(__name__)


@dataclass
class GapHandler:
    """Drives REST re-fetch on gap events."""

    resyncer: Resyncer

    async def handle(self, event: CDCEvent) -> dict[str, Any]:
        if not event.is_gap:
            return {"handled": False, "reason": "not a gap event"}
        record_ids = list(event.header.record_ids)
        sobject = event.header.entity_name
        log.warning(
            "shadow.reconcile.gap_event",
            sobject=sobject,
            change_type=event.header.change_type.value,
            record_count=len(record_ids),
        )
        outcomes: list[dict[str, Any]] = []
        for rid in record_ids:
            outcome = await self.resyncer.resync_record(sobject=sobject, record_id=rid)
            outcomes.append(outcome)
        return {
            "handled": True,
            "sobject": sobject,
            "record_count": len(record_ids),
            "outcomes": outcomes,
        }
