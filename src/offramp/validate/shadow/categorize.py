"""Divergence categorization (architecture §10.4 + AD-22 7th category).

Inputs:
* the field-level diff from :mod:`offramp.validate.shadow.diff`
* the OoE runtime trace (records exception types, step ordering)
* the source CDC event (gap events route directly to AD-22)

Output: a :class:`offramp.core.models.DivergenceCategory` plus a severity
score (0-100) used by the readiness window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from offramp.core.models import DivergenceCategory
from offramp.validate.shadow.cdc_event import CDCEvent


@dataclass(frozen=True)
class CategorizationResult:
    diverged: bool
    category: DivergenceCategory | None
    severity: int  # 0-100
    explanation: str


def categorize(
    *,
    event: CDCEvent,
    field_diffs: dict[str, tuple[Any, Any]],
    trace: dict[str, Any],
) -> CategorizationResult:
    """Apply the 7-bucket categorization rules in priority order."""
    # AD-22: gap events bypass everything else.
    if event.is_gap:
        return CategorizationResult(
            diverged=True,
            category=DivergenceCategory.GAP_EVENT_FULL_REFETCH_REQUIRED,
            severity=80,
            explanation=(
                f"gap event ({event.header.change_type.value}) on "
                f"{event.header.entity_name} — must trigger REST re-fetch (AD-21)"
            ),
        )

    if not field_diffs and not trace.get("aborted"):
        return CategorizationResult(
            diverged=False,
            category=None,
            severity=0,
            explanation="no divergence",
        )

    # OoE runtime exceptions -> ordering / cascade misbehavior.
    if trace.get("aborted"):
        reason = trace.get("abort_reason", "") or ""
        if "MixedDML" in reason:
            return CategorizationResult(
                diverged=True,
                category=DivergenceCategory.OOE_ORDERING_MISMATCH,
                severity=60,
                explanation=f"runtime aborted with mixed-DML: {reason}",
            )
        if "Cascade" in reason:
            return CategorizationResult(
                diverged=True,
                category=DivergenceCategory.OOE_ORDERING_MISMATCH,
                severity=70,
                explanation=f"cascade depth exceeded: {reason}",
            )
        if "validation" in reason.lower():
            return CategorizationResult(
                diverged=True,
                category=DivergenceCategory.TRANSLATION_ERROR,
                severity=50,
                explanation=f"validation rule fired in runtime but production accepted: {reason}",
            )

    # Numeric-only diffs are usually formula edge cases (precision, rounding).
    numeric_only = field_diffs and all(
        _looks_numeric(p) or _looks_numeric(r) for p, r in field_diffs.values()
    )
    if numeric_only:
        return CategorizationResult(
            diverged=True,
            category=DivergenceCategory.FORMULA_EDGE_CASE,
            severity=40,
            explanation=f"numeric divergence on {sorted(field_diffs)}",
        )

    # Trigger ordering — the trace flagged a non-deterministic interleave.
    if trace.get("non_deterministic_ordering_observed"):
        return CategorizationResult(
            diverged=True,
            category=DivergenceCategory.NON_DETERMINISTIC_TRIGGER_ORDERING,
            severity=55,
            explanation="multiple triggers fired in differing orders across runs",
        )

    # Governor-limit shaped: production has a value the runtime doesn't
    # because the runtime has no governor limits — flagged by the runtime
    # noting "governor_limit_avoided" in trace metadata.
    if trace.get("governor_limit_avoided"):
        return CategorizationResult(
            diverged=True,
            category=DivergenceCategory.GOVERNOR_LIMIT_BEHAVIOR,
            severity=35,
            explanation="runtime executed logic SF would have skipped per governor limits",
        )

    # Test-environment artifact: explicit hint from the executor (e.g. running
    # in shadow mode with a different running user).
    if trace.get("test_env_artifact"):
        return CategorizationResult(
            diverged=True,
            category=DivergenceCategory.TEST_ENVIRONMENT_ARTIFACT,
            severity=10,
            explanation=trace.get("test_env_artifact_reason", "test environment difference"),
        )

    # Fallback: default to a translation error so engineers get pinged.
    return CategorizationResult(
        diverged=True,
        category=DivergenceCategory.TRANSLATION_ERROR,
        severity=50,
        explanation=f"field-level divergence on {sorted(field_diffs)}",
    )


def _looks_numeric(v: Any) -> bool:
    return isinstance(v, int | float) or (
        isinstance(v, str) and v.replace(".", "", 1).lstrip("-").isdigit()
    )
