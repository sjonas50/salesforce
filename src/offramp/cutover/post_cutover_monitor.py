"""Post-cutover regression detection (architecture §11.5).

After a process reaches 100% cutover, Shadow Mode does not stop — it switches
to a regression-detection role: the Salesforce-side automation continues to
exist (required for compliance + rollback) and every runtime decision is
compared against what Salesforce would have done.

If readiness drops below the configured threshold post-cutover, the monitor
optionally triggers an instant rollback (the same mechanism Phase 5.3 wires
into the routing table).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from offramp.core.logging import get_logger
from offramp.cutover.orchestrator import CutoverOrchestrator
from offramp.cutover.provenance import CutoverProvenance
from offramp.mcp.routing import RoutingTable
from offramp.validate.shadow.readiness import ReadinessScorer

log = get_logger(__name__)


@dataclass(frozen=True)
class RegressionAlert:
    process_id: str
    score: int
    threshold: int
    detected_at: datetime
    auto_rollback_triggered: bool
    explanation: str


@dataclass
class PostCutoverMonitor:
    """Polled by ``offramp cutover monitor`` (or a scheduled job)."""

    routing: RoutingTable
    scorer: ReadinessScorer
    provenance: CutoverProvenance
    orchestrator: CutoverOrchestrator
    regression_threshold: int = 95
    auto_rollback: bool = False  # opt-in; default is alert-only

    async def check(self, process_id: str) -> RegressionAlert | None:
        cfg = await self.routing.get_config(process_id)
        if cfg is None or cfg.stage_percent < 100:
            return None  # only monitors processes already cut over
        score = await self.scorer.score(process_id)
        if score.score >= self.regression_threshold:
            return None
        # Regression — alert + optionally instant-rollback.
        triggered = False
        if self.auto_rollback:
            await self.routing.instant_rollback(process_id)
            await self.provenance.anchor_stage_transition(
                process_id=process_id,
                from_percent=100,
                to_percent=0,
                readiness_score=score.score,
                kind="instant_rollback",
                reason=(
                    f"post-cutover regression: score {score.score} < {self.regression_threshold}"
                ),
            )
            triggered = True
        alert = RegressionAlert(
            process_id=process_id,
            score=score.score,
            threshold=self.regression_threshold,
            detected_at=datetime.now(UTC),
            auto_rollback_triggered=triggered,
            explanation=score.reason,
        )
        log.warning(
            "cutover.post_cutover.regression",
            process=process_id,
            score=score.score,
            threshold=self.regression_threshold,
            auto_rollback_triggered=triggered,
        )
        return alert
