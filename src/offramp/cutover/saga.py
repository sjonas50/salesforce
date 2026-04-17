"""Saga compensation (architecture §11.3).

Every runtime activity declares a compensation. On rollback, the saga walks
the recorded activities in reverse order and runs each compensation. For
actions without a meaningful compensation (an email already sent, an LLM
inference cost), the metadata says so explicitly and the orchestrator
requires human confirmation before including the action in cutover.

The framework is intentionally small — it has to be obvious to read because
saga bugs are silent and expensive.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class CompensationKind(StrEnum):
    """How the activity can be reversed."""

    UNDO = "undo"  # idempotent reversal (delete the record we created)
    OFFSET = "offset"  # follow-up action (send a correction email)
    LOG_ONLY = "log_only"  # no real reversal (LLM call cost, audit entry)
    REQUIRES_HUMAN = "requires_human"  # cutover must pause for sign-off


@dataclass(frozen=True)
class ActivitySpec:
    """Static description of one activity that participates in the saga."""

    name: str
    compensation_kind: CompensationKind
    description: str = ""
    # Compensation function — None when kind is LOG_ONLY or REQUIRES_HUMAN.
    compensate: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None


@dataclass
class ActivityRecord:
    """One executed activity, recorded so the saga can compensate it."""

    activity: ActivitySpec
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SagaTransaction:
    """One saga's activity list."""

    saga_id: str
    activities: list[ActivityRecord] = field(default_factory=list)

    def record(
        self, activity: ActivitySpec, inputs: dict[str, Any], outputs: dict[str, Any]
    ) -> None:
        self.activities.append(ActivityRecord(activity=activity, inputs=inputs, outputs=outputs))

    def has_irreversible_actions(self) -> bool:
        """True if any recorded activity requires human sign-off to roll back."""
        return any(
            a.activity.compensation_kind is CompensationKind.REQUIRES_HUMAN for a in self.activities
        )


@dataclass
class CompensationResult:
    """Per-activity outcome of a rollback attempt."""

    activity_name: str
    kind: CompensationKind
    succeeded: bool
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompensationOutcome:
    """Aggregate rollback result."""

    saga_id: str
    fully_compensated: bool
    paused_for_human: bool
    results: list[CompensationResult] = field(default_factory=list)


async def compensate(saga: SagaTransaction) -> CompensationOutcome:
    """Walk the saga's activities in reverse and run each compensation.

    Stops at the first ``REQUIRES_HUMAN`` activity, returning
    ``paused_for_human=True`` so the orchestrator knows to surface it.
    """
    out = CompensationOutcome(saga_id=saga.saga_id, fully_compensated=True, paused_for_human=False)
    for record in reversed(saga.activities):
        spec = record.activity
        if spec.compensation_kind is CompensationKind.REQUIRES_HUMAN:
            out.results.append(
                CompensationResult(
                    activity_name=spec.name,
                    kind=spec.compensation_kind,
                    succeeded=False,
                    reason="human sign-off required before continuing rollback",
                )
            )
            out.paused_for_human = True
            out.fully_compensated = False
            return out
        if spec.compensation_kind is CompensationKind.LOG_ONLY:
            out.results.append(
                CompensationResult(
                    activity_name=spec.name,
                    kind=spec.compensation_kind,
                    succeeded=True,
                    reason="log-only — no real reversal needed",
                )
            )
            continue
        if spec.compensate is None:
            out.results.append(
                CompensationResult(
                    activity_name=spec.name,
                    kind=spec.compensation_kind,
                    succeeded=False,
                    reason="compensation kind requires a function but none registered",
                )
            )
            out.fully_compensated = False
            continue
        try:
            payload = await spec.compensate({"inputs": record.inputs, "outputs": record.outputs})
        except Exception as exc:
            out.results.append(
                CompensationResult(
                    activity_name=spec.name,
                    kind=spec.compensation_kind,
                    succeeded=False,
                    reason=f"compensation raised {type(exc).__name__}: {exc}",
                )
            )
            out.fully_compensated = False
            continue
        out.results.append(
            CompensationResult(
                activity_name=spec.name,
                kind=spec.compensation_kind,
                succeeded=True,
                reason="compensation completed",
                payload=payload,
            )
        )
    return out
