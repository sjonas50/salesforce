"""Cutover orchestrator: advance + rollback driven by readiness scores.

Architecture §11.2 thresholds:
* readiness >= 98 for the dwell time -> auto-advance to next stage
* readiness < 95 at any point -> rollback to previous stage
* readiness < 90 -> immediate-rollback-with-signoff (caller must confirm)

The orchestrator is invoked in a loop (CLI ``offramp cutover advance``,
or a scheduled job in production) and decides whether to act.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from offramp.core.logging import get_logger
from offramp.cutover.provenance import CutoverProvenance
from offramp.cutover.router import next_stage, previous_stage
from offramp.cutover.saga import CompensationOutcome, SagaTransaction, compensate
from offramp.mcp.routing import RoutingTable
from offramp.validate.shadow.readiness import ReadinessScorer

log = get_logger(__name__)


class TransitionKind(StrEnum):
    HOLD = "hold"
    ADVANCE = "advance"
    ROLLBACK = "rollback"
    IMMEDIATE_ROLLBACK = "immediate_rollback"


@dataclass(frozen=True)
class TransitionDecision:
    """One iteration of the orchestrator's decision loop."""

    process_id: str
    kind: TransitionKind
    from_percent: int
    to_percent: int
    readiness_score: int
    reason: str
    requires_human_signoff: bool = False
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CutoverOrchestrator:
    """Drive auto-advance + auto-rollback for a single process."""

    routing: RoutingTable
    scorer: ReadinessScorer
    provenance: CutoverProvenance
    advance_threshold: int = 98
    rollback_threshold: int = 95
    immediate_rollback_threshold: int = 90

    async def evaluate(
        self,
        process_id: str,
        *,
        now: datetime | None = None,
    ) -> TransitionDecision:
        """Decide what to do right now. Does NOT mutate routing — see :meth:`apply`."""
        now = now or datetime.now(UTC)
        cfg = await self.routing.get_config(process_id)
        if cfg is None:
            return TransitionDecision(
                process_id=process_id,
                kind=TransitionKind.HOLD,
                from_percent=0,
                to_percent=0,
                readiness_score=0,
                reason="no routing config — start with `offramp cutover begin`",
            )
        score = await self.scorer.score(process_id)

        if score.score < self.immediate_rollback_threshold:
            return TransitionDecision(
                process_id=process_id,
                kind=TransitionKind.IMMEDIATE_ROLLBACK,
                from_percent=cfg.stage_percent,
                to_percent=0,
                readiness_score=score.score,
                reason=(
                    f"score {score.score} < immediate_rollback_threshold "
                    f"{self.immediate_rollback_threshold}; requires human sign-off"
                ),
                requires_human_signoff=True,
            )
        if score.score < self.rollback_threshold and cfg.stage_percent > 0:
            prev = previous_stage(cfg.stage_percent)
            return TransitionDecision(
                process_id=process_id,
                kind=TransitionKind.ROLLBACK,
                from_percent=cfg.stage_percent,
                to_percent=prev,
                readiness_score=score.score,
                reason=(
                    f"score {score.score} < rollback_threshold {self.rollback_threshold}; "
                    f"reverting to {prev}%"
                ),
            )

        nxt = next_stage(cfg.stage_percent)
        if (
            score.score >= self.advance_threshold
            and cfg.dwell_complete(now=now)
            and nxt is not None
            and score.cutover_eligible
        ):
            return TransitionDecision(
                process_id=process_id,
                kind=TransitionKind.ADVANCE,
                from_percent=cfg.stage_percent,
                to_percent=nxt,
                readiness_score=score.score,
                reason=f"score {score.score} >= {self.advance_threshold} + dwell complete",
            )

        # Hold — describe why so observability has something to show.
        if nxt is None:
            reason = "already at 100%"
        elif score.score < self.advance_threshold:
            reason = f"score {score.score} < advance_threshold {self.advance_threshold}"
        elif not cfg.dwell_complete(now=now):
            remaining = cfg.dwell_remaining(now=now)
            reason = f"dwell incomplete ({remaining} remaining)"
        else:
            reason = "not cutover-eligible per readiness scorer"
        return TransitionDecision(
            process_id=process_id,
            kind=TransitionKind.HOLD,
            from_percent=cfg.stage_percent,
            to_percent=cfg.stage_percent,
            readiness_score=score.score,
            reason=reason,
        )

    async def apply(
        self,
        decision: TransitionDecision,
        *,
        confirmed: bool = False,
        saga: SagaTransaction | None = None,
    ) -> dict[str, Any]:
        """Execute a transition. Confirmations required for IMMEDIATE_ROLLBACK."""
        if decision.kind is TransitionKind.HOLD:
            return {"applied": False, "reason": "hold — no transition"}
        if decision.requires_human_signoff and not confirmed:
            return {
                "applied": False,
                "reason": "human sign-off required (pass confirmed=True)",
                "decision": decision.kind.value,
            }
        compensation: CompensationOutcome | None = None
        if decision.kind in {TransitionKind.ROLLBACK, TransitionKind.IMMEDIATE_ROLLBACK} and saga:
            compensation = await compensate(saga)
            if not compensation.fully_compensated and not confirmed:
                return {
                    "applied": False,
                    "reason": "saga compensation incomplete or paused",
                    "compensation_outcome": compensation,
                }

        cfg = await self.routing.get_config(decision.process_id)
        seed = cfg.hash_seed if cfg else "_seed"
        new_cfg = await self.routing.upsert(
            process_id=decision.process_id,
            stage_percent=decision.to_percent,
            hash_seed=seed,
        )
        engram_anchor, f44_anchor = await self.provenance.anchor_stage_transition(
            process_id=decision.process_id,
            from_percent=decision.from_percent,
            to_percent=decision.to_percent,
            readiness_score=decision.readiness_score,
            kind=decision.kind.value,
            reason=decision.reason,
        )
        return {
            "applied": True,
            "decision": decision.kind.value,
            "from_percent": decision.from_percent,
            "to_percent": decision.to_percent,
            "engram_anchor": engram_anchor,
            "f44_anchor": f44_anchor,
            "compensation_outcome": compensation,
            "new_config": {
                "process_id": new_cfg.process_id,
                "stage_percent": new_cfg.stage_percent,
                "entered_stage_at": new_cfg.entered_stage_at.isoformat(),
            },
        }

    async def begin(
        self,
        *,
        process_id: str,
        hash_seed: str | None = None,
    ) -> dict[str, Any]:
        """Initialize routing for a process at 1% (the first staged percentage)."""
        seed = hash_seed or process_id
        cfg = await self.routing.upsert(
            process_id=process_id,
            stage_percent=1,
            hash_seed=seed,
        )
        engram_anchor, f44_anchor = await self.provenance.anchor_stage_transition(
            process_id=process_id,
            from_percent=0,
            to_percent=1,
            readiness_score=0,
            kind="begin",
            reason="cutover begin — initial stage",
        )
        return {
            "began": True,
            "config": {
                "process_id": cfg.process_id,
                "stage_percent": cfg.stage_percent,
                "hash_seed": cfg.hash_seed,
                "entered_stage_at": cfg.entered_stage_at.isoformat(),
            },
            "engram_anchor": engram_anchor,
            "f44_anchor": f44_anchor,
        }
