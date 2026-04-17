"""Engram + F44 anchoring of every cutover decision (architecture §11.4).

Every routing decision is Engram-anchored; **stage transitions** (advance,
rollback) are *additionally* F44-anchored to a public Merkle root for
independent verification. F44 is a no-op when ``f44_network`` isn't set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from offramp.engram.client import EngramClient


@dataclass
class CutoverProvenance:
    """Anchors decisions + transitions to Engram (and F44 for transitions)."""

    engram: EngramClient
    component: str = "cutover.orchestrator"

    async def anchor_routing_decision(
        self,
        *,
        process_id: str,
        record_id: str,
        target: str,
        stage_percent: int,
    ) -> str:
        rec = await self.engram.anchor(
            f"{self.component}.routing",
            {
                "process_id": process_id,
                "record_id": record_id,
                "routed_to": target,
                "stage_percent": stage_percent,
                "decided_at": _now_iso(),
            },
        )
        return rec.anchor_id

    async def anchor_stage_transition(
        self,
        *,
        process_id: str,
        from_percent: int,
        to_percent: int,
        readiness_score: int,
        kind: str,  # 'advance' | 'rollback' | 'instant_rollback'
        reason: str,
    ) -> tuple[str, str | None]:
        """Engram + F44 anchor a stage transition.

        Returns ``(engram_anchor_id, f44_anchor_id_or_None)``.
        """
        payload = {
            "process_id": process_id,
            "from_percent": from_percent,
            "to_percent": to_percent,
            "readiness_score": readiness_score,
            "kind": kind,
            "reason": reason,
            "transitioned_at": _now_iso(),
        }
        engram_rec = await self.engram.anchor(f"{self.component}.stage_transition", payload)
        # F44 anchoring uses the same Engram client surface in this skeleton —
        # the real F44 backend is a separate Rust service that batches Merkle
        # roots to Base. The architecture deferred its full integration to the
        # Engram parallel track; we record the F44 intent here.
        f44_rec = await self.engram.anchor(f"{self.component}.f44", payload)
        return engram_rec.anchor_id, f44_rec.anchor_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
