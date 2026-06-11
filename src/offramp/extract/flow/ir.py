"""Flow Intermediate Representation — a comprehensive, typed Flow AST.

Salesforce stores every Flow variant in the same ``.flow-meta.xml`` schema. The
:class:`FlowIR` captures *all* of it: every element type, every connector (the
control-flow edges), every resource (variables / formulas / choices), and the
data each element reads and writes. This is the payload that makes genuine
reverse-engineering possible — you cannot reconstruct a Flow's behavior from a
flat list of element names, you need the connector graph and the data deps.

The IR is deliberately one rich :class:`FlowElement` model rather than 18
subclasses: the element types share ~80% of their structure (name, label,
connectors, location), and a single model keeps the graph loader and the
translators simple. Type-specific fields are optional; ``raw`` is the escape
hatch for anything not yet promoted to a typed field.

Reference for the schema: Salesforce Metadata API "Flow" type.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FlowElementType(StrEnum):
    """Every executable Flow element kind we model."""

    START = "start"
    ASSIGNMENT = "assignment"
    DECISION = "decision"
    LOOP = "loop"
    RECORD_CREATE = "record_create"
    RECORD_UPDATE = "record_update"
    RECORD_DELETE = "record_delete"
    RECORD_LOOKUP = "record_lookup"
    RECORD_ROLLBACK = "record_rollback"
    ACTION_CALL = "action_call"
    APEX_PLUGIN_CALL = "apex_plugin_call"
    SUBFLOW = "subflow"
    SCREEN = "screen"
    WAIT = "wait"
    COLLECTION_PROCESSOR = "collection_processor"
    TRANSFORM = "transform"
    STEP = "step"
    ORCHESTRATED_STAGE = "orchestrated_stage"
    CUSTOM_ERROR = "custom_error"


class ConnectorKind(StrEnum):
    """How one element hands control to the next.

    Distinguishing these is the whole point — a fault connector is an error
    path, a rule connector is a conditional branch, a loop-next connector is the
    body of an iteration. A naive parser that collapses them all to "next" loses
    the Flow's actual semantics.
    """

    NEXT = "next"  # plain <connector>
    FAULT = "fault"  # <faultConnector> — error handling path
    DEFAULT = "default"  # decision/wait <defaultConnector>
    RULE = "rule"  # decision <rules><connector> — conditional branch
    LOOP_NEXT = "loop_next"  # loop <nextValueConnector> — body
    LOOP_END = "loop_end"  # loop <noMoreValuesConnector> — after loop
    WAIT_EVENT = "wait_event"  # wait <waitEvents><connector>
    SCHEDULED_PATH = "scheduled_path"  # start <scheduledPaths><connector>
    IMMEDIATE = "immediate"  # start immediate <connector>


# Salesforce SObject-typed Flow variable dataTypes.
_SOBJECT_DATA_TYPES = {"SObject", "sobject"}


class FlowConnector(BaseModel):
    """One outgoing edge from an element."""

    model_config = ConfigDict(frozen=True)

    target: str = Field(description="targetReference — the next element's name")
    kind: ConnectorKind = ConnectorKind.NEXT
    is_go_to: bool = Field(default=False, description="<isGoTo>true</isGoTo> — back-edge / GOTO")
    label: str | None = Field(default=None, description="Branch label (rule name, event name, …)")


class FlowCondition(BaseModel):
    """A single boolean condition (decision rule / filter / wait event)."""

    model_config = ConfigDict(frozen=True)

    left: str = Field(description="leftValueReference, e.g. $Record.Amount")
    operator: str = Field(default="", description="EqualTo, GreaterThan, IsChanged, …")
    right: str | None = Field(default=None, description="Literal value or referenced element")
    right_kind: str | None = Field(
        default=None, description="stringValue|numberValue|booleanValue|elementReference|…"
    )


class FlowDecisionRule(BaseModel):
    """One branch of a decision element."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str | None = None
    condition_logic: str = Field(default="and", description="and|or|custom logic string")
    conditions: list[FlowCondition] = Field(default_factory=list)
    connector: FlowConnector | None = None


class FlowFieldWrite(BaseModel):
    """A field the element writes (record create/update input assignment)."""

    model_config = ConfigDict(frozen=True)

    field: str
    value: str | None = None
    value_kind: str | None = None


class FlowFieldRead(BaseModel):
    """A field the element reads (filter / queried field / output mapping)."""

    model_config = ConfigDict(frozen=True)

    field: str
    operator: str | None = None
    value: str | None = None
    value_kind: str | None = None


class FlowElement(BaseModel):
    """One node in the Flow's execution graph.

    Only the fields relevant to ``element_type`` are populated; the rest stay at
    their defaults. ``raw`` preserves the original element dict so nothing is
    silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    element_type: FlowElementType
    label: str | None = None
    description: str | None = None
    location_x: float | None = None
    location_y: float | None = None

    # Control flow — every outgoing edge, typed by ConnectorKind.
    connectors: list[FlowConnector] = Field(default_factory=list)

    # Data: records & fields
    object: str | None = Field(default=None, description="Target SObject for DML / lookup")
    input_reference: str | None = Field(default=None, description="record-update target reference")
    collection_reference: str | None = Field(default=None, description="loop collection reference")
    field_writes: list[FlowFieldWrite] = Field(default_factory=list)
    field_reads: list[FlowFieldRead] = Field(default_factory=list)
    filter_logic: str | None = None
    queried_fields: list[str] = Field(default_factory=list)

    # Decisions
    rules: list[FlowDecisionRule] = Field(default_factory=list)

    # External invocations
    action_name: str | None = None
    action_type: str | None = None
    flow_name: str | None = Field(default=None, description="subflow target Flow API name")
    apex_class: str | None = Field(default=None, description="apexPluginCall / apex action class")

    # Free references this element makes to resources/elements (for REFERENCES edges)
    references: list[str] = Field(default_factory=list)

    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    def calls_apex(self) -> str | None:
        """The Apex class this element invokes, if any."""
        if self.apex_class:
            return self.apex_class
        if self.action_type == "apex" and self.action_name:
            return self.action_name
        return None


class FlowResourceKind(StrEnum):
    """Non-executable Flow resources."""

    VARIABLE = "variable"
    CONSTANT = "constant"
    FORMULA = "formula"
    TEXT_TEMPLATE = "text_template"
    CHOICE = "choice"
    DYNAMIC_CHOICE_SET = "dynamic_choice_set"
    STAGE = "stage"


class FlowResource(BaseModel):
    """A Flow variable / constant / formula / choice."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: FlowResourceKind
    data_type: str | None = None
    object_type: str | None = Field(default=None, description="SObject type for SObject variables")
    is_collection: bool = False
    is_input: bool = False
    is_output: bool = False
    expression: str | None = Field(default=None, description="formula expression / template body")
    references: list[str] = Field(
        default_factory=list, description="resources referenced by a formula/template"
    )

    @property
    def is_sobject(self) -> bool:
        return self.data_type in _SOBJECT_DATA_TYPES and self.object_type is not None


class FlowScheduledPath(BaseModel):
    """A scheduled path off a record-triggered Flow's start."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str | None = None
    offset_number: int | None = None
    offset_unit: str | None = None
    time_source: str | None = None
    connector: FlowConnector | None = None


class FlowStart(BaseModel):
    """The Flow's entry point + trigger configuration."""

    model_config = ConfigDict(extra="forbid")

    trigger_type: str | None = Field(default=None, description="RecordAfterSave, Scheduled, …")
    record_trigger_type: str | None = Field(
        default=None, description="Create|Update|CreateAndUpdate|Delete"
    )
    object: str | None = None
    filter_logic: str | None = None
    filters: list[FlowFieldRead] = Field(default_factory=list)
    schedule_frequency: str | None = None
    run_in_mode: str | None = None
    connector: FlowConnector | None = Field(default=None, description="immediate path")
    scheduled_paths: list[FlowScheduledPath] = Field(default_factory=list)
    location_x: float | None = None
    location_y: float | None = None


class FlowIR(BaseModel):
    """The complete, typed reverse-engineering of one Flow."""

    model_config = ConfigDict(extra="forbid")

    api_name: str
    label: str | None = None
    api_version: str = "66.0"
    process_type: str = ""
    status: str = "Active"
    run_in_mode: str | None = None
    interview_label: str | None = None

    start: FlowStart | None = None
    elements: list[FlowElement] = Field(default_factory=list)
    resources: list[FlowResource] = Field(default_factory=list)

    # ---- Derived views the graph loader + translators consume ----

    def element_by_name(self, name: str) -> FlowElement | None:
        for el in self.elements:
            if el.name == name:
                return el
        return None

    def control_flow_edges(self) -> list[tuple[str, FlowConnector]]:
        """(source element name, connector) for every edge, including the start.

        The start's edges use the sentinel source name ``"__start__"``.
        """
        edges: list[tuple[str, FlowConnector]] = []
        if self.start is not None:
            if self.start.connector is not None:
                edges.append(("__start__", self.start.connector))
            for sp in self.start.scheduled_paths:
                if sp.connector is not None:
                    edges.append(("__start__", sp.connector))
        for el in self.elements:
            for conn in el.connectors:
                edges.append((el.name, conn))
        return edges

    def referenced_objects(self) -> set[str]:
        """SObjects this Flow touches (DML targets, lookups, start object, SObject vars)."""
        objs: set[str] = set()
        if self.start and self.start.object:
            objs.add(self.start.object)
        for el in self.elements:
            if el.object:
                objs.add(el.object)
        for r in self.resources:
            if r.is_sobject and r.object_type:
                objs.add(r.object_type)
        return objs

    def written_objects(self) -> set[str]:
        """SObjects this Flow mutates (create/update/delete)."""
        mutating = {
            FlowElementType.RECORD_CREATE,
            FlowElementType.RECORD_UPDATE,
            FlowElementType.RECORD_DELETE,
        }
        objs: set[str] = set()
        for el in self.elements:
            if el.element_type in mutating and el.object:
                objs.add(el.object)
        # record-update via inputReference resolves to the start object when triggered.
        if self.start and self.start.object:
            for el in self.elements:
                if el.element_type is FlowElementType.RECORD_UPDATE and not el.object:
                    objs.add(self.start.object)
        return objs

    def invoked_subflows(self) -> set[str]:
        return {
            el.flow_name
            for el in self.elements
            if el.element_type is FlowElementType.SUBFLOW and el.flow_name
        }

    def called_apex(self) -> set[str]:
        out: set[str] = set()
        for el in self.elements:
            cls = el.calls_apex()
            if cls:
                out.add(cls)
        return out
