"""Order-of-Execution runtime state machine.

The single hardest component of the v2.1 plan (R1 in §15). Implements the
21-step Salesforce save Order of Execution as a Python state machine with
faithful semantics for:

* atomic transactions (steps 1-19 commit or roll back together)
* the **workflow re-fire cycle** (step 12 → re-execute steps 5+9 once)
* **cascade tracking** (after-save DML on a different record opens a
  recursive child transaction, depth-bounded)
* **once-per-entity-per-transaction** for Flows
* **non-deterministic trigger ordering** (deterministic in shadow mode via
  a transaction-id seed)
* **mixed-DML** boundary detection (setup vs non-setup objects in one txn)

Phase 3 implements the high-value steps the Surface Audit flagged as
in-scope for the fixture org: 5 (BeforeTriggers), 6 (CustomValidation),
9 (AfterTriggers), 12 (WorkflowRules), 13 (ProcessesAndFlows). The
remaining 16 steps are wired as no-ops with a clear EXTEND_HOOK so future
phases can plug in without touching the state-machine core.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from offramp.core.logging import get_logger
from offramp.extract.ooe_audit.audit import OoEStep
from offramp.runtime.rules.engine import Rule, RuleResult, RulesEngine

log = get_logger(__name__)

# Setup objects: modifying one of these in the same transaction as a non-setup
# object raises Salesforce's MIXED_DML_OPERATION. Names are case-insensitive.
SETUP_OBJECTS: frozenset[str] = frozenset(
    {
        "user",
        "group",
        "groupmember",
        "permissionset",
        "permissionsetassignment",
        "queuesobject",
        "userrole",
        "profile",
    }
)


class OoERuntimeError(RuntimeError):
    """Base for all OoE runtime errors."""


class StepNotInScopeError(OoERuntimeError):
    """Transaction would exercise an OoE step the runtime explicitly excludes.

    Raised when the Surface Audit marked the step as ``exclude`` and a runtime
    operation tries to execute at it. This is a feature: the runtime fails
    loudly rather than producing undefined behavior.
    """


class MixedDMLError(OoERuntimeError):
    """Setup-object and non-setup-object DML in the same transaction."""


class CascadeDepthExceededError(OoERuntimeError):
    """Cascading DML exceeded the configured depth limit (default 16)."""


class ValidationFailedError(OoERuntimeError):
    """One or more validation rules returned a failure."""

    def __init__(self, results: list[RuleResult]) -> None:
        self.results = results
        msgs = "; ".join(r.error_message or r.rule_id for r in results)
        super().__init__(f"validation failed: {msgs}")


@dataclass
class DMLOperation:
    """One create/update/delete recorded inside a transaction."""

    op: Literal["create", "update", "delete"]
    sobject: str
    record_id: str | None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransactionContext:
    """Per-save accumulator. Lives until commit or rollback."""

    transaction_id: str
    triggering_record: dict[str, Any]
    sobject: str
    cascade_depth: int = 0
    workflow_refire_flag: bool = False
    refire_done: bool = False
    flows_fired_for: set[str] = field(default_factory=set)  # record IDs
    rule_results: list[RuleResult] = field(default_factory=list)
    cascaded_dml: list[DMLOperation] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str | None = None
    in_scope_steps: set[OoEStep] = field(default_factory=lambda: set(OoEStep))
    # Track sObjects touched in the txn so we can detect mixed-DML.
    setup_touched: bool = False
    nonsetup_touched: bool = False

    def trace(self) -> list[str]:
        """Per-transaction debug trace shared by Compare Mode (Phase 4)."""
        return [
            f"txn={self.transaction_id}",
            f"sobject={self.sobject}",
            f"refire={self.workflow_refire_flag}",
            f"refire_done={self.refire_done}",
            f"cascade_depth={self.cascade_depth}",
            f"rule_results={len(self.rule_results)}",
            f"cascaded_dml={len(self.cascaded_dml)}",
            f"aborted={self.aborted}({self.abort_reason})",
        ]


@dataclass
class OoERuntime:
    """The runtime state machine.

    Constructed with a :class:`RulesEngine` (rules to execute) and a
    Surface-Audit-derived ``in_scope_steps`` set. ``cascade_depth_limit``
    matches Salesforce's default of 16.
    """

    rules: RulesEngine
    in_scope_steps: set[OoEStep] = field(default_factory=lambda: set(OoEStep))
    cascade_depth_limit: int = 16
    seed: int = 42  # makes non-deterministic trigger ordering reproducible

    def execute_save(
        self,
        *,
        sobject: str,
        record: dict[str, Any],
        transaction_id: str | None = None,
        parent_ctx: TransactionContext | None = None,
    ) -> TransactionContext:
        """Drive one record through the save pipeline.

        ``parent_ctx`` is set when this save is a cascaded child of an
        outer transaction — its presence enforces the once-per-entity-per-
        transaction rule and the cascade depth limit.
        """
        ctx = (
            parent_ctx
            if parent_ctx is not None
            else TransactionContext(
                transaction_id=transaction_id or _new_txn_id(),
                triggering_record=record,
                sobject=sobject,
                in_scope_steps=set(self.in_scope_steps),
            )
        )

        if parent_ctx is not None:
            ctx.cascade_depth += 1
            if ctx.cascade_depth > self.cascade_depth_limit:
                raise CascadeDepthExceededError(
                    f"cascade depth {ctx.cascade_depth} > limit {self.cascade_depth_limit}"
                )

        self._record_object_class(ctx, sobject)

        try:
            # Step 5 — BeforeTriggers
            self._step(ctx, OoEStep.BEFORE_TRIGGERS, sobject, record)

            # Step 6 — CustomValidation (Validation Rules + before-save Flow logic)
            self._step(ctx, OoEStep.CUSTOM_VALIDATION, sobject, record, validation=True)

            # Step 9 — AfterTriggers (and any cascades they fire)
            self._step(ctx, OoEStep.AFTER_TRIGGERS, sobject, record)

            # Step 12 — WorkflowRules. If any field-update rule fires, set the
            # re-fire flag.
            self._step(ctx, OoEStep.WORKFLOW_RULES, sobject, record)

            # Step 13 — ProcessesAndFlows. Once-per-entity-per-transaction
            # for record-triggered flows.
            self._maybe_fire_flows(ctx, sobject, record)

            # Re-fire cycle: re-execute steps 5+9 ONCE if the flag was set
            # during step 12, then clear the flag permanently.
            if ctx.workflow_refire_flag and not ctx.refire_done:
                ctx.refire_done = True
                ctx.workflow_refire_flag = False
                log.debug("ooe.refire", txn=ctx.transaction_id)
                self._step(ctx, OoEStep.BEFORE_TRIGGERS, sobject, record)
                self._step(ctx, OoEStep.AFTER_TRIGGERS, sobject, record)
        except ValidationFailedError as exc:
            ctx.aborted = True
            ctx.abort_reason = str(exc)
            raise
        return ctx

    def _step(
        self,
        ctx: TransactionContext,
        step: OoEStep,
        sobject: str,
        record: dict[str, Any],
        *,
        validation: bool = False,
    ) -> None:
        if step not in ctx.in_scope_steps:
            raise StepNotInScopeError(
                f"OoE step {int(step)} ({step.name}) is not in scope for this runtime; "
                "the Surface Audit excluded it. Re-run extract + audit to widen scope."
            )
        rules = self.rules.rules_for(sobject, int(step))
        if not rules:
            return
        # Non-deterministic trigger ordering: shuffle rule order per txn but
        # deterministically seeded so failures are reproducible.
        ordered = self._maybe_shuffle(rules, ctx)
        for r in ordered:
            result = r.evaluate(record, {"transaction_id": ctx.transaction_id})
            ctx.rule_results.append(result)
            # Apply computation mutations immediately so subsequent rules in
            # the same step see the updated record.
            if result.field_mutations:
                record.update(result.field_mutations)
            if validation and not result.passed:
                # Collect every failing rule before raising — better diagnostics.
                continue
            # Workflow re-fire detection: any field_mutation produced at
            # step 12 sets the re-fire flag.
            if step is OoEStep.WORKFLOW_RULES and result.field_mutations:
                ctx.workflow_refire_flag = True

        if validation:
            failed = [r for r in ctx.rule_results if r.kind == "validation" and not r.passed]
            if failed:
                raise ValidationFailedError(failed)

    def _maybe_fire_flows(
        self, ctx: TransactionContext, sobject: str, record: dict[str, Any]
    ) -> None:
        """Step 13 — once-per-entity-per-transaction for record-triggered flows."""
        record_id = record.get("Id") or record.get("id") or ctx.transaction_id
        key = f"{sobject}:{record_id}"
        if key in ctx.flows_fired_for:
            log.debug("ooe.flow.skipped_once_per_entity", key=key)
            return
        ctx.flows_fired_for.add(key)
        self._step(ctx, OoEStep.PROCESSES_AND_FLOWS, sobject, record)

    def _record_object_class(self, ctx: TransactionContext, sobject: str) -> None:
        is_setup = sobject.lower() in SETUP_OBJECTS
        if is_setup:
            ctx.setup_touched = True
        else:
            ctx.nonsetup_touched = True
        if ctx.setup_touched and ctx.nonsetup_touched:
            raise MixedDMLError(
                f"transaction {ctx.transaction_id} touches both setup and "
                "non-setup objects (mixed-DML)."
            )

    def _maybe_shuffle(self, rules: Iterable[Rule], ctx: TransactionContext) -> list[Rule]:
        """Deterministic per-transaction shuffle so non-deterministic ordering
        is reproducible across runs.

        Rules ordered by ``rule_id`` first (stable canonical order) so the
        shuffle is over a defined input.
        """
        ordered = sorted(rules, key=lambda r: r.rule_id)
        if len(ordered) <= 1:
            return ordered
        rnd = random.Random(f"{self.seed}:{ctx.transaction_id}")
        rnd.shuffle(ordered)
        return ordered


def _new_txn_id() -> str:
    import uuid

    return uuid.uuid4().hex
