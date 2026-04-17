"""Hash-deterministic per-record traffic router.

Each record's routing decision is a stable function of (process_id, record_id,
hash_seed). The same record is routed the same way every time — avoiding the
"partial flip-flop" failure mode where a record processed by Salesforce on
one event and the runtime on the next produces inconsistent intermediate
state (architecture §11.2).

Stages: 1, 5, 25, 50, 100. Each stage has a configured dwell time during
which the readiness scorer must hold the threshold; the orchestrator
(:mod:`offramp.cutover.orchestrator`) drives the advance/rollback decisions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

# Stage percentages in the order the cutover advances through them.
STAGE_PERCENTS: tuple[int, ...] = (0, 1, 5, 25, 50, 100)

# Default dwell times per stage from architecture §11.2.
_DEFAULT_DWELL: dict[int, timedelta] = {
    0: timedelta(seconds=0),
    1: timedelta(hours=48),
    5: timedelta(hours=24),
    25: timedelta(hours=12),
    50: timedelta(hours=6),
    100: timedelta(hours=0),
}


Target = Literal["salesforce", "runtime"]


@dataclass(frozen=True)
class RoutingConfig:
    """Per-process routing config — the single knob the gateway reads."""

    process_id: str
    stage_percent: int
    hash_seed: str
    entered_stage_at: datetime

    def dwell_remaining(self, *, now: datetime | None = None) -> timedelta:
        """Time remaining until this stage's dwell completes."""
        now = now or datetime.now(UTC)
        dwell = _DEFAULT_DWELL.get(self.stage_percent, timedelta(0))
        elapsed = now - self.entered_stage_at
        return max(timedelta(0), dwell - elapsed)

    def dwell_complete(self, *, now: datetime | None = None) -> bool:
        return self.dwell_remaining(now=now) == timedelta(0)


def route_for_record(config: RoutingConfig, record_id: str) -> Target:
    """Deterministic per-record routing.

    The hash determines the record's "bucket" in [0, 100). Records whose
    bucket is strictly less than the stage_percent route to the runtime;
    everything else stays on Salesforce. Same record + same seed + same
    stage = same decision, every call.
    """
    bucket = _bucket(seed=config.hash_seed, key=f"{config.process_id}:{record_id}")
    return "runtime" if bucket < config.stage_percent else "salesforce"


def _bucket(*, seed: str, key: str) -> int:
    """Map ``key`` to an integer in [0, 100) deterministically."""
    h = hashlib.blake2b(f"{seed}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") % 100


def next_stage(current: int) -> int | None:
    """Return the next stage percent, or ``None`` if already at 100."""
    if current >= STAGE_PERCENTS[-1]:
        return None
    for i, s in enumerate(STAGE_PERCENTS):
        if s == current and i + 1 < len(STAGE_PERCENTS):
            return STAGE_PERCENTS[i + 1]
    # current isn't a recognized stage — treat as 0 and advance.
    return STAGE_PERCENTS[1]


def previous_stage(current: int) -> int:
    """Return the previous stage percent (0 stays at 0)."""
    if current <= STAGE_PERCENTS[0]:
        return STAGE_PERCENTS[0]
    for i, s in enumerate(STAGE_PERCENTS):
        if s == current and i - 1 >= 0:
            return STAGE_PERCENTS[i - 1]
    return STAGE_PERCENTS[0]
