"""OoE Surface Audit (C4) — bound the runtime scope.

For each of the 21 OoE steps, count:

* how many extracted Components execute at this step (structural)
* how many were observed firing in the analysis window (frequency, optional)

Steps with zero exercising components are marked for exclusion from the OoE
runtime — the runtime explicitly refuses transactions that would exercise
them. This converts the unbounded compatibility problem into a bounded one
sized to the customer's actual usage.

Step numbering follows the v2.1 reference (architecture §4.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from offramp.core.models import CategoryName, Component


class OoEStep(IntEnum):
    """The 21 steps of the Salesforce save Order of Execution."""

    LOAD_INITIAL_RECORDS = 1
    LOAD_OLD_RECORDS = 2
    SYSTEM_VALIDATION = 3
    PRE_TRIGGER_FLOW = 4
    BEFORE_TRIGGERS = 5
    CUSTOM_VALIDATION = 6
    DUPLICATE_RULES = 7
    SAVE_RECORD = 8
    AFTER_TRIGGERS = 9
    ASSIGNMENT_RULES = 10
    AUTO_RESPONSE_RULES = 11
    WORKFLOW_RULES = 12
    PROCESSES_AND_FLOWS = 13
    ESCALATION_RULES = 14
    ENTITLEMENT_RULES = 15
    ROLLUP_SUMMARY = 16
    SHARING_RULES = 17
    PARENT_ROLLUP = 18
    COMMIT_DML = 19
    POST_COMMIT_LOGIC = 20
    EMAIL_SEND = 21


# Which OoE steps each category typically exercises. A category may map to
# more than one step (e.g. Apex Triggers fire at both 5 and 9 depending on
# trigger event). The runtime will read this same mapping when classifying
# transactions — keep it as the single source of truth.
_CATEGORY_TO_STEPS: dict[CategoryName, set[OoEStep]] = {
    CategoryName.RECORD_TRIGGERED_FLOW: {
        OoEStep.PRE_TRIGGER_FLOW,
        OoEStep.PROCESSES_AND_FLOWS,
    },
    CategoryName.SCREEN_FLOW: set(),  # UI; not on the save path
    CategoryName.SCHEDULE_TRIGGERED_FLOW: set(),  # external scheduler
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW: {OoEStep.PROCESSES_AND_FLOWS},
    CategoryName.AUTOLAUNCHED_FLOW: {OoEStep.PROCESSES_AND_FLOWS},
    CategoryName.FLOW_ORCHESTRATION: {OoEStep.PROCESSES_AND_FLOWS},
    CategoryName.PROCESS_BUILDER: {OoEStep.PROCESSES_AND_FLOWS},
    CategoryName.APEX_TRIGGER: {OoEStep.BEFORE_TRIGGERS, OoEStep.AFTER_TRIGGERS},
    CategoryName.APEX_CLASS: set(),  # invoked from triggers/flows; not a step itself
    CategoryName.VALIDATION_RULE: {OoEStep.CUSTOM_VALIDATION},
    CategoryName.FORMULA_FIELD: set(),  # evaluated lazily, not on save path
    CategoryName.WORKFLOW_RULE: {OoEStep.WORKFLOW_RULES},
    CategoryName.APPROVAL_PROCESS: {OoEStep.POST_COMMIT_LOGIC},
    CategoryName.ASSIGNMENT_RULE: {OoEStep.ASSIGNMENT_RULES},
    CategoryName.AUTO_RESPONSE_RULE: {OoEStep.AUTO_RESPONSE_RULES, OoEStep.EMAIL_SEND},
    CategoryName.ESCALATION_RULE: {OoEStep.ESCALATION_RULES},
    CategoryName.SHARING_RULE: {OoEStep.SHARING_RULES},
    CategoryName.ROLLUP_SUMMARY: {OoEStep.ROLLUP_SUMMARY, OoEStep.PARENT_ROLLUP},
    CategoryName.PLATFORM_EVENT: {OoEStep.POST_COMMIT_LOGIC},
    CategoryName.CHANGE_DATA_CAPTURE: {OoEStep.POST_COMMIT_LOGIC},
    CategoryName.LWC_BUNDLE: set(),  # client-side; not on save path
}


@dataclass
class StepObservation:
    """One row of the surface audit report."""

    step: OoEStep
    structural_count: int
    observed_frequency: int | None = None  # None = no EventLogFile data
    contributing_categories: list[CategoryName] = field(default_factory=list)
    in_scope: bool = False
    priority: str = "exclude"  # 'critical' | 'standard' | 'exclude'


@dataclass
class SurfaceAuditReport:
    """The Surface Audit deliverable."""

    org_alias: str
    total_components: int
    observations: list[StepObservation] = field(default_factory=list)


def classify_steps(category: CategoryName) -> set[OoEStep]:
    """Public accessor — used by the runtime to know which steps to enforce."""
    return _CATEGORY_TO_STEPS.get(category, set())


def audit(
    components: list[Component],
    org_alias: str,
    *,
    observed_frequency_by_step: dict[OoEStep, int] | None = None,
) -> SurfaceAuditReport:
    """Build the report.

    ``observed_frequency_by_step`` comes from EventLogFile when available; if
    omitted, the report is structural-only and priority defaults to
    ``standard`` for any non-empty step.
    """
    structural: dict[OoEStep, int] = {step: 0 for step in OoEStep}
    contributors: dict[OoEStep, set[CategoryName]] = {step: set() for step in OoEStep}

    for c in components:
        for step in classify_steps(c.category):
            structural[step] += 1
            contributors[step].add(c.category)

    obs: list[StepObservation] = []
    for step in OoEStep:
        struct = structural[step]
        freq = observed_frequency_by_step.get(step) if observed_frequency_by_step else None
        if struct == 0 or (freq is not None and freq == 0):
            priority = "exclude"
            in_scope = False
        elif freq is not None and freq < 5:
            priority = "standard"
            in_scope = True
        else:
            priority = "critical" if struct >= 10 else "standard"
            in_scope = True
        obs.append(
            StepObservation(
                step=step,
                structural_count=struct,
                observed_frequency=freq,
                contributing_categories=sorted(contributors[step]),
                in_scope=in_scope,
                priority=priority,
            )
        )

    return SurfaceAuditReport(
        org_alias=org_alias,
        total_components=len(components),
        observations=obs,
    )
