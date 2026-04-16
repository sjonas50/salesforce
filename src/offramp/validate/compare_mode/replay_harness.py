"""Replay parsed debug-log transactions through the OoE runtime.

The output mirrors :mod:`offramp.validate.shadow.executor` so Compare Mode
findings join the same divergence pipeline + readiness scoring as live
Shadow Mode events. This is exactly the point of v2.1 §9.2.6 — Compare
Mode catches ~80% of OoE bugs at ~20% of the infrastructure complexity by
re-using shadow's downstream surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from offramp.core.logging import get_logger
from offramp.engram.client import EngramClient
from offramp.runtime.ooe.state_machine import OoERuntime, ValidationFailedError
from offramp.validate.compare_mode.log_parser import ParsedTransaction
from offramp.validate.compare_mode.state_reconstructor import StateReconstructor
from offramp.validate.shadow.store import ShadowStore

log = get_logger(__name__)


@dataclass
class ReplayOutcome:
    txn_start: str
    sobjects_touched: list[str]
    runtime_aborted: bool
    runtime_validation_failures: list[str]
    log_validation_failures: list[str]
    diverged: bool
    explanation: str
    anchor_id: str | None


@dataclass
class ReplayHarness:
    """Drive Compare Mode replays end-to-end."""

    runtime: OoERuntime
    reconstructor: StateReconstructor
    store: ShadowStore
    engram: EngramClient
    process_id: str

    async def replay(self, txn: ParsedTransaction) -> list[ReplayOutcome]:
        outcomes: list[ReplayOutcome] = []
        states = await self.reconstructor.reconstruct(txn)
        for state in states:
            runtime_failed: list[str] = []
            aborted = False
            try:
                ctx = self.runtime.execute_save(sobject=state.sobject, record=dict(state.pre_state))
                runtime_failed = [
                    r.rule_id for r in ctx.rule_results if r.kind == "validation" and not r.passed
                ]
            except ValidationFailedError as exc:
                aborted = True
                runtime_failed = [r.rule_id for r in exc.results]

            log_failures = list(txn.validation_failures)
            diverged, explanation = _classify_divergence(
                runtime_failed=runtime_failed,
                log_failures=log_failures,
                runtime_aborted=aborted,
            )
            anchor = await self.engram.anchor(
                "compare_mode.replay",
                {
                    "process_id": self.process_id,
                    "txn_start": txn.start.isoformat(),
                    "sobject": state.sobject,
                    "diverged": diverged,
                    "runtime_validation_failures": runtime_failed,
                    "log_validation_failures": log_failures,
                },
            )
            await self.store.write_divergence(
                process_id=self.process_id,
                replay_id=f"compare_mode:{txn.start.isoformat()}:{state.sobject}",
                diverged=diverged,
                category="ooe_ordering_mismatch" if diverged else None,
                field_diffs={"runtime": runtime_failed, "log": log_failures} if diverged else {},
                trace={"compare_mode": True, "explanation": explanation},
                anchor_id=anchor.anchor_id,
                severity=50 if diverged else 0,
            )
            outcomes.append(
                ReplayOutcome(
                    txn_start=txn.start.isoformat(),
                    sobjects_touched=[state.sobject],
                    runtime_aborted=aborted,
                    runtime_validation_failures=runtime_failed,
                    log_validation_failures=log_failures,
                    diverged=diverged,
                    explanation=explanation,
                    anchor_id=anchor.anchor_id,
                )
            )
        return outcomes


def _classify_divergence(
    *,
    runtime_failed: list[str],
    log_failures: list[str],
    runtime_aborted: bool,
) -> tuple[bool, str]:
    runtime_set = set(runtime_failed)
    log_set = set(log_failures)
    if runtime_set == log_set:
        return False, "runtime + log agree on validation outcomes"
    extra_runtime = runtime_set - log_set
    extra_log = log_set - runtime_set
    parts = []
    if extra_runtime:
        parts.append(f"runtime fired but log did not: {sorted(extra_runtime)}")
    if extra_log:
        parts.append(f"log fired but runtime did not: {sorted(extra_log)}")
    if runtime_aborted and not log_failures:
        parts.append("runtime aborted; production accepted the txn")
    return True, "; ".join(parts) or "unknown divergence"


def _pretty_outcome_summary(outcomes: list[ReplayOutcome]) -> dict[str, Any]:
    diverged = sum(1 for o in outcomes if o.diverged)
    return {
        "total": len(outcomes),
        "diverged": diverged,
        "diverged_pct": (100 * diverged // max(len(outcomes), 1)),
    }
