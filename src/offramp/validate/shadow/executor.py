"""Shadow executor — drive translated artifacts from CDC events.

For each incoming :class:`CDCEvent`:

1. Reconstruct the post-state record (CDC payload merged with shadow store).
2. Open a :class:`ForkedDataEnv` for this transaction.
3. Run the OoE runtime against the record using the loaded :class:`RulesEngine`.
4. Diff the runtime-produced post-state against the CDC payload.
5. Categorize the divergence (or absence thereof).
6. Anchor the comparison in Engram and persist to the shadow store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from offramp.core.logging import get_logger
from offramp.engram.client import EngramClient
from offramp.runtime.ooe.state_machine import (
    OoERuntime,
    TransactionContext,
    ValidationFailedError,
)
from offramp.validate.shadow.categorize import categorize
from offramp.validate.shadow.cdc_event import CDCEvent
from offramp.validate.shadow.data_env import ForkedDataEnv
from offramp.validate.shadow.diff import field_diff
from offramp.validate.shadow.store import ShadowStore, event_record_id

log = get_logger(__name__)


@dataclass
class ShadowExecutionOutcome:
    """One executor pass against a single CDC event."""

    replay_id: str
    diverged: bool
    category: str | None
    severity: int
    field_diffs: dict[str, tuple[Any, Any]]
    trace: dict[str, Any]
    anchor_id: str | None
    divergence_row_id: int | None


@dataclass
class ShadowExecutor:
    """Drives one process's shadow validation."""

    process_id: str
    runtime: OoERuntime
    store: ShadowStore
    engram: EngramClient
    data_env_factory: Any  # callable: () -> ForkedDataEnv

    async def execute_event(self, event: CDCEvent) -> ShadowExecutionOutcome:
        """Run one CDC event through the shadow pipeline + record outcome."""
        sobject = event.header.entity_name
        record_id = event_record_id(event)

        # 1. Pre-state: shadow store + production read-through (the data env
        # handles fall-through). 2. Post-state from CDC.
        env: ForkedDataEnv = self.data_env_factory()
        pre_state = await env.read(sobject, record_id) or {}
        cdc_post_state = {**pre_state, **event.fields, "Id": record_id}
        # Strip the embedded ChangeEventHeader if it leaked through.
        cdc_post_state.pop("ChangeEventHeader", None)

        # 3. Drive the runtime.
        trace: dict[str, Any] = {}
        runtime_post_state: dict[str, Any] = dict(cdc_post_state)
        try:
            ctx: TransactionContext = self.runtime.execute_save(
                sobject=sobject,
                record=runtime_post_state,
            )
            trace = {
                "trace": ctx.trace(),
                "aborted": ctx.aborted,
                "abort_reason": ctx.abort_reason,
                "rule_results": [
                    {
                        "rule_id": r.rule_id,
                        "passed": r.passed,
                        "kind": r.kind,
                    }
                    for r in ctx.rule_results
                ],
            }
        except ValidationFailedError as exc:
            trace = {"aborted": True, "abort_reason": str(exc)}

        # 4. Diff. Skip 'Id' since both should agree.
        diffs = field_diff(cdc_post_state, runtime_post_state, ignore={"Id"})

        # 5. Categorize.
        result = categorize(event=event, field_diffs=diffs, trace=trace)

        # 6. Anchor + persist.
        anchor = await self.engram.anchor(
            "shadow.executor",
            {
                "process_id": self.process_id,
                "replay_id": event.replay_id,
                "diverged": result.diverged,
                "category": result.category.value if result.category else None,
                "field_diff_keys": sorted(diffs.keys()),
            },
        )
        row_id = await self.store.write_divergence(
            process_id=self.process_id,
            replay_id=event.replay_id,
            diverged=result.diverged,
            category=result.category.value if result.category else None,
            field_diffs={k: list(v) for k, v in diffs.items()},
            trace=trace,
            anchor_id=anchor.anchor_id,
            severity=result.severity,
        )
        await self.store.update_replay_state(
            process_id=self.process_id,
            replay_id=event.replay_id,
        )

        # Update the shadow record so subsequent events see the runtime view.
        if event.header.change_type.value in {"CREATE", "UPDATE", "UNDELETE"}:
            await self.store.upsert_record(
                sobject=sobject,
                record_id=record_id,
                fields=runtime_post_state,
                replay_id=event.replay_id,
            )
        elif event.header.change_type.value == "DELETE":
            await self.store.delete_record(sobject, record_id)

        log.debug(
            "shadow.executor.processed",
            process=self.process_id,
            replay_id=event.replay_id,
            diverged=result.diverged,
            category=str(result.category) if result.category else None,
        )

        return ShadowExecutionOutcome(
            replay_id=event.replay_id,
            diverged=result.diverged,
            category=result.category.value if result.category else None,
            severity=result.severity,
            field_diffs=diffs,
            trace=trace,
            anchor_id=anchor.anchor_id,
            divergence_row_id=row_id,
        )
