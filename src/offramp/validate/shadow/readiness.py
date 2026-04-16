"""Readiness scoring (architecture §10.5).

Each translated process gets a 0-100 score derived from observed shadow
behavior over a rolling window (default 30 days). The score blends:

* clean-rate: % of observations with diverged=False
* severity-weighted: each diverged observation deducts based on its severity
* coverage: enough events to be statistically meaningful

A process scoring >= 98 for 14 consecutive days is cutover-eligible. Below
98, the cutover orchestrator (Phase 5) refuses to advance the staged
traffic shift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from offramp.validate.shadow.store import ShadowStore


@dataclass(frozen=True)
class ReadinessScore:
    process_id: str
    window_days: int
    total_events: int
    clean_events: int
    diverged_events: int
    avg_severity: float
    score: int  # 0-100
    cutover_eligible: bool
    reason: str


@dataclass
class ReadinessScorer:
    """Computes readiness from the shadow store's append-only window."""

    store: ShadowStore
    window_days: int = 30
    min_events_for_eligibility: int = 100
    eligibility_threshold: int = 98

    async def score(self, process_id: str) -> ReadinessScore:
        since = datetime.now(UTC) - timedelta(days=self.window_days)
        rows = await self.store.readiness_window(process_id, since=since)
        total = len(rows)
        if total == 0:
            return ReadinessScore(
                process_id=process_id,
                window_days=self.window_days,
                total_events=0,
                clean_events=0,
                diverged_events=0,
                avg_severity=0.0,
                score=0,
                cutover_eligible=False,
                reason="no shadow events recorded yet",
            )
        diverged = sum(1 for r in rows if r["diverged"])
        clean = total - diverged
        clean_rate = clean / total
        sev_total = sum(r["severity"] for r in rows if r["diverged"])
        avg_sev = (sev_total / diverged) if diverged else 0.0
        # Score blends clean-rate with severity. A 99% clean rate where the
        # 1% has severity 100 should not score 99 — the rare-but-bad case
        # matters. Subtract avg-severity-weighted penalty from clean_rate * 100.
        raw_score = clean_rate * 100 - (diverged / total) * (avg_sev * 0.5)
        score = max(0, min(100, round(raw_score)))

        # Eligibility: enough events + score over threshold.
        eligible = total >= self.min_events_for_eligibility and score >= self.eligibility_threshold
        if total < self.min_events_for_eligibility:
            reason = (
                f"only {total} events in {self.window_days}-day window; "
                f"need {self.min_events_for_eligibility} for eligibility"
            )
        elif score < self.eligibility_threshold:
            reason = (
                f"score {score} < threshold {self.eligibility_threshold} "
                f"(diverged_rate={diverged / total:.2%}, avg_severity={avg_sev:.0f})"
            )
        else:
            reason = f"score {score} >= {self.eligibility_threshold} on {total} events"
        return ReadinessScore(
            process_id=process_id,
            window_days=self.window_days,
            total_events=total,
            clean_events=clean,
            diverged_events=diverged,
            avg_severity=avg_sev,
            score=score,
            cutover_eligible=eligible,
            reason=reason,
        )
