"""Platform-neutral process model (the reusable asset).

Every automation X-Ray reverse-engineers is normalized into a
:class:`ProcessDefinition`: what triggers it, the conditions it checks, the
steps it takes, and the data it touches. The model is deliberately free of
Salesforce vocabulary where a general term exists (``update`` not
``recordUpdates``, ``notify`` not ``emailAlert``) so a definition can be
read by a business analyst, diffed across scans, matched across orgs, and
handed to a generator for another platform later.

Identity is content-addressed: :attr:`ProcessDefinition.id` is the SHA-256 of
the canonical definition *without* its provenance, so the same logic found
in two orgs, or in two scans of one org, is one entry in the knowledge
store. :attr:`ProcessDefinition.fingerprint` hashes the *shape* only (kinds
of steps and operators, not names or values) so near-duplicates group into
families.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TriggerKind(StrEnum):
    RECORD_SAVE = "record_save"
    RECORD_DELETE = "record_delete"
    SCHEDULED = "scheduled"
    PLATFORM_EVENT = "platform_event"
    SCREEN = "screen"  # user-driven, interactive
    INVOCATION = "invocation"  # called by code / flow / API
    APPROVAL_SUBMIT = "approval_submit"
    MANUAL = "manual"


class StepKind(StrEnum):
    DECISION = "decision"
    ASSIGN = "assign"  # set variables / fields in memory
    LOOKUP = "lookup"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    CALL_CODE = "call_code"  # Apex class / method
    CALL_PROCESS = "call_process"  # subflow / flow action
    CALL_ACTION = "call_action"  # platform / external action, callout, outbound message
    NOTIFY = "notify"  # email alert, chatter post, custom notification
    TASK = "task"
    WAIT = "wait"
    LOOP = "loop"
    SCREEN = "screen"
    APPROVAL_STEP = "approval_step"
    ESCALATE = "escalate"
    RAISE_ERROR = "raise_error"
    SHARE = "share"
    ROLLBACK = "rollback"
    END = "end"


class Fidelity(StrEnum):
    FULL = "full"  # every step and condition captured
    PARTIAL = "partial"  # structure captured, some logic opaque (formulas, screens)
    REFERENCES_ONLY = "references_only"  # code: effects known, control flow not modelled


class Condition(BaseModel):
    """One comparison, or an opaque expression when the source is a formula."""

    model_config = ConfigDict(extra="forbid")

    left: str = ""  # 'Lead.Country__c', 'IsEnterprise' (a variable)
    operator: str = (
        ""  # EqualTo, NotEqualTo, GreaterThan, Contains, IsNull, ... or '' for expression
    )
    right: Any = None  # literal or {'ref': 'X'}
    expression: str | None = Field(default=None, description="Formula text when not decomposable")


class ConditionGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logic: str = "and"  # and | or | custom ('1 AND (2 OR 3)')
    conditions: list[Condition] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.conditions


class Branch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str = ""
    when: ConditionGroup = Field(default_factory=ConditionGroup)
    next: str | None = None


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: StepKind
    label: str = ""
    object: str | None = None
    fields: list[str] = Field(default_factory=list, description="Qualified fields read or written")
    inputs: dict[str, Any] = Field(default_factory=dict, description="name → literal or {'ref': X}")
    when: ConditionGroup | None = Field(default=None, description="Filter / guard on this step")
    branches: list[Branch] = Field(default_factory=list, description="Decision outcomes")
    default_next: str | None = None
    next: str | None = None
    on_fault: str | None = None
    target: str | None = Field(
        default=None, description="Class, flow, action, template, queue, user"
    )
    extras: dict[str, Any] = Field(default_factory=dict)


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TriggerKind
    object: str | None = None
    events: list[str] = Field(default_factory=list)  # create, update, delete, undelete
    timing: str | None = None  # before | after
    when: ConditionGroup = Field(default_factory=ConditionGroup)
    requires_change: bool = False
    schedule: dict[str, Any] = Field(default_factory=dict)
    entry_points: list[str] = Field(default_factory=list)  # for code: aura_enabled, invocable, ...


class Variable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = ""
    object: str | None = None
    is_input: bool = False
    is_output: bool = False
    expression: str | None = None  # formulas


class ProcessSource(BaseModel):
    """Where one instance of this definition was observed."""

    model_config = ConfigDict(extra="forbid")

    org_alias: str
    category: str
    api_name: str
    component_id: str
    content_hash: str
    scan_id: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProcessDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="", description="SHA-256 of the canonical definition (set by finalize)")
    fingerprint: str = Field(default="", description="SHA-256 of the shape only (set by finalize)")
    name: str
    label: str = ""
    kind: str = Field(description="Source category, e.g. record_triggered_flow, workflow_rule")
    description: str = ""
    trigger: Trigger
    steps: list[Step] = Field(default_factory=list)
    entry: str | None = Field(default=None, description="Id of the first step")
    variables: list[Variable] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    fields_read: list[str] = Field(default_factory=list)
    fields_written: list[str] = Field(default_factory=list)
    calls: list[str] = Field(default_factory=list, description="Code, processes, actions invoked")
    fidelity: Fidelity = Fidelity.FULL
    fidelity_notes: list[str] = Field(
        default_factory=list,
        description="Why fidelity is below full: what the model cannot express yet.",
    )
    active: bool = True
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    sources: list[ProcessSource] = Field(default_factory=list)
    code: str | None = Field(default=None, description="Source body for code-derived definitions")

    # ---- identity ----------------------------------------------------------------

    def canonical(self) -> dict[str, Any]:
        """The definition without provenance, org labels, or free text."""
        return self.model_dump(
            mode="json",
            exclude={
                "id",
                "fingerprint",
                "sources",
                "summary",
                "tags",
                "description",
                "code",
                "label",
            },
        )

    def shape(self) -> dict[str, Any]:
        """Structure only: step kinds, operators, branch counts; no names or values."""
        return {
            "kind": self.kind,
            "trigger": [
                self.trigger.kind.value,
                sorted(self.trigger.events),
                self.trigger.timing or "",
            ],
            "steps": [
                [
                    s.kind.value,
                    len(s.branches),
                    sorted(c.operator for b in s.branches for c in b.when.conditions),
                    sorted(c.operator for c in (s.when.conditions if s.when else [])),
                ]
                for s in self.steps
            ],
        }

    def finalize(self) -> ProcessDefinition:
        self.id = _digest(self.canonical())
        self.fingerprint = _digest(self.shape())
        return self

    # ---- convenience ---------------------------------------------------------------

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def touched_fields(self) -> list[str]:
        return sorted(set(self.fields_read) | set(self.fields_written))


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
