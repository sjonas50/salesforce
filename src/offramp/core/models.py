"""Pydantic models shared across phases.

These are the contract types that flow between Extract → Understand → Generate
→ Validate → Cutover. Adding a field here implies updating every consumer
(see ``scripts/check_matrix_fixtures.py`` for the pre-commit guard on
translation-matrix changes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class CategoryName(StrEnum):
    """The 21 Salesforce automation categories (v2.1 reference §7.3) plus the
    five *surface* categories X-Ray needs for "where is this used": page
    layouts, Lightning pages, permission sets, profiles, reports.

    Surface categories never fire on save (no OoE step) and count as UI /
    security / reporting references rather than automation. Order matches
    the v2.1 reference for the first 21; do not reorder without coordinating
    with the OoE Surface Audit (C4) which keys on this enum.
    """

    RECORD_TRIGGERED_FLOW = "record_triggered_flow"
    SCREEN_FLOW = "screen_flow"
    SCHEDULE_TRIGGERED_FLOW = "schedule_triggered_flow"
    PLATFORM_EVENT_TRIGGERED_FLOW = "platform_event_triggered_flow"
    AUTOLAUNCHED_FLOW = "autolaunched_flow"
    FLOW_ORCHESTRATION = "flow_orchestration"
    APEX_TRIGGER = "apex_trigger"
    APEX_CLASS = "apex_class"
    VALIDATION_RULE = "validation_rule"
    FORMULA_FIELD = "formula_field"
    WORKFLOW_RULE = "workflow_rule"
    PROCESS_BUILDER = "process_builder"
    APPROVAL_PROCESS = "approval_process"
    ASSIGNMENT_RULE = "assignment_rule"
    AUTO_RESPONSE_RULE = "auto_response_rule"
    ESCALATION_RULE = "escalation_rule"
    SHARING_RULE = "sharing_rule"
    ROLLUP_SUMMARY = "rollup_summary"
    PLATFORM_EVENT = "platform_event"
    CHANGE_DATA_CAPTURE = "change_data_capture"
    LWC_BUNDLE = "lwc_bundle"
    AURA_BUNDLE = "aura_bundle"
    # ---- surface categories (UI / security / reporting) ----
    PAGE_LAYOUT = "page_layout"
    FLEXIPAGE = "flexipage"
    PERMISSION_SET = "permission_set"
    PROFILE = "profile"
    REPORT = "report"
    CUSTOM_TAB = "custom_tab"
    CUSTOM_APPLICATION = "custom_application"
    PATH_ASSISTANT = "path_assistant"


AUTOMATION_CATEGORIES: frozenset[CategoryName] = frozenset(
    c
    for c in CategoryName
    if c
    not in {
        CategoryName.PAGE_LAYOUT,
        CategoryName.FLEXIPAGE,
        CategoryName.PERMISSION_SET,
        CategoryName.PROFILE,
        CategoryName.REPORT,
        CategoryName.CUSTOM_TAB,
        CategoryName.CUSTOM_APPLICATION,
        CategoryName.PATH_ASSISTANT,
    }
)
UI_CATEGORIES: frozenset[CategoryName] = frozenset(
    {CategoryName.PAGE_LAYOUT, CategoryName.FLEXIPAGE}
)
SECURITY_CATEGORIES: frozenset[CategoryName] = frozenset(
    {CategoryName.PERMISSION_SET, CategoryName.PROFILE}
)
REPORTING_CATEGORIES: frozenset[CategoryName] = frozenset({CategoryName.REPORT})


class Tier(StrEnum):
    """Execution tier assignment for a translated component."""

    TIER1_RULES = "tier1_rules"
    TIER2_TEMPORAL = "tier2_temporal"
    TIER3_LANGGRAPH = "tier3_langgraph"


class DivergenceCategory(StrEnum):
    """Shadow-execution divergence categorization (architecture §10.4 + AD-22)."""

    TRANSLATION_ERROR = "translation_error"
    OOE_ORDERING_MISMATCH = "ooe_ordering_mismatch"
    GOVERNOR_LIMIT_BEHAVIOR = "governor_limit_behavior"
    NON_DETERMINISTIC_TRIGGER_ORDERING = "non_deterministic_trigger_ordering"
    FORMULA_EDGE_CASE = "formula_edge_case"
    TEST_ENVIRONMENT_ARTIFACT = "test_environment_artifact"
    GAP_EVENT_FULL_REFETCH_REQUIRED = "gap_event_full_refetch_required"  # AD-22


class Provenance(BaseModel):
    """Where a record came from. Embedded in every extracted artifact."""

    model_config = ConfigDict(frozen=True)

    source_tool: str = Field(description="e.g. 'salto', 'sf_cli', 'tooling_api'")
    source_version: str
    api_version: str = "66.0"
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Component(BaseModel):
    """One piece of Salesforce automation as extracted by the extract engine.

    The ``content_hash`` is the canonical fingerprint anchored in Engram. Two
    Components with the same hash are guaranteed semantically identical.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    org_alias: str
    category: CategoryName
    name: str = Field(description="Salesforce-facing developer name")
    api_name: str | None = Field(default=None, description="Fully qualified name if applicable")
    namespace: str | None = None
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw parser output (XML-as-dict, NaCl-as-dict, etc.)",
    )
    content_hash: str = Field(description="SHA-256 of canonical JSON; Engram anchor key")
    provenance: Provenance


class DependencyKind(StrEnum):
    """Edge type in the Component dependency graph."""

    CALLS = "calls"
    DISPATCHES = "dispatches"
    DEPENDS_ON = "depends_on"
    REFERENCES = "references"
    TRIGGERS = "triggers"
    OWNS = "owns"
    PARTICIPATES_IN = "participates_in"
    ESCALATES_TO = "escalates_to"
    COMPENSATES = "compensates"


class EvidenceChannel(StrEnum):
    """How an edge was discovered (AD-30). Every edge carries exactly one."""

    APEX_PARSE = "apex_parse"
    FLOW_XML = "flow_xml"
    FORMULA = "formula"
    WORKFLOW_XML = "workflow_xml"
    RULE_XML = "rule_xml"  # assignment / escalation / auto-response / sharing / approval
    ROLLUP_XML = "rollup_xml"
    LWC_IMPORT = "lwc_import"
    AURA_MARKUP = "aura_markup"  # Aura component markup + controller/helper JS
    CMT_DISPATCH = "cmt_dispatch"
    CMT_RECORD = "cmt_record"  # custom metadata rows as configuration nodes
    SCHEMA = "schema"  # lookup / master-detail relationship
    PATH = "path"  # object inferred from file path (objects/<Object>/...)
    DEPENDENCY_API = "dependency_api"  # MetadataComponentDependency row (cross-check)
    CRON = "cron"
    LAYOUT_XML = "layout_xml"  # page layouts, Lightning pages
    PERMISSION_XML = "permission_xml"  # permission sets, profiles (FLS, class/flow access)
    REPORT_XML = "report_xml"


class Dependency(BaseModel):
    """Edge in the Component graph.

    ``source_id`` / ``target_id`` reference either a :class:`Component` or a
    :class:`SchemaNode`. ``evidence`` records the channel that produced the
    edge and ``confidence`` how sure we are; both are surfaced in the X-Ray
    report (AD-30).
    """

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    target_id: UUID
    kind: DependencyKind
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    evidence: EvidenceChannel = EvidenceChannel.PATH
    notes: str | None = None
    corroborated_by_api: bool = Field(
        default=False,
        description="True when a MetadataComponentDependency row agrees with this edge.",
    )


class SchemaNodeKind(StrEnum):
    OBJECT = "object"
    FIELD = "field"
    RECORD_TYPE = "record_type"


class SchemaNode(BaseModel):
    """A data-model node: an sObject, a field, or a record type.

    Schema nodes are first-class graph participants so automation can be
    linked to the data it reads and writes (build plan v0.2, C21).
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    org_alias: str
    kind: SchemaNodeKind
    api_name: str = Field(
        description="Object: 'Account'; field: 'Account.Industry'; RT: 'Account.Partner'"
    )
    object_name: str
    label: str = ""
    field_type: str | None = Field(default=None, description="Salesforce field type for fields")
    reference_to: list[str] = Field(
        default_factory=list, description="Lookup / master-detail targets"
    )
    relationship_name: str | None = None
    custom: bool = False
    picklist_values: list[str] = Field(default_factory=list)
    formula: str | None = None
    required: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class FieldProfile(BaseModel):
    """How populated one field is, from an aggregate query over live records."""

    model_config = ConfigDict(extra="forbid")

    api_name: str
    non_null: int
    fill_rate: Annotated[float, Field(ge=0.0, le=1.0)]


class ObjectProfile(BaseModel):
    """Record volume + field population for one sObject."""

    model_config = ConfigDict(extra="forbid")

    object_name: str
    record_count: int | None = Field(default=None, description="None when the count query failed")
    last_modified: datetime | None = None
    fields: dict[str, FieldProfile] = Field(
        default_factory=dict, description="keyed by 'Object.Field'"
    )
    error: str | None = None


class DataProfile(BaseModel):
    """Data-level evidence: record counts and field fill rates (build plan D.8)."""

    model_config = ConfigDict(extra="forbid")

    org_alias: str
    objects: dict[str, ObjectProfile] = Field(default_factory=dict)
    sampled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = "aggregate_queries"

    def field(self, qualified: str) -> FieldProfile | None:
        obj, _, _ = qualified.partition(".")
        op = self.objects.get(obj)
        return op.fields.get(qualified) if op else None


class SchemaSnapshot(BaseModel):
    """Data model of one org at extraction time."""

    model_config = ConfigDict(extra="forbid")

    org_alias: str
    nodes: list[SchemaNode] = Field(default_factory=list)
    source: str = Field(default="source_tree", description="'source_tree' | 'describe'")

    def objects(self) -> list[SchemaNode]:
        return [n for n in self.nodes if n.kind is SchemaNodeKind.OBJECT]

    def fields(self) -> list[SchemaNode]:
        return [n for n in self.nodes if n.kind is SchemaNodeKind.FIELD]

    def by_api_name(self) -> dict[str, SchemaNode]:
        return {n.api_name: n for n in self.nodes}


class AST(BaseModel):
    """Parsed AST attached to a Component (extract output, translator input)."""

    model_config = ConfigDict(extra="forbid")

    component_id: UUID
    parser: str = Field(description="e.g. 'summit-ast', 'lightning-flow-scanner', 'tree-sitter'")
    parser_version: str
    tree: dict[str, Any] = Field(description="Parser-native serialization")


class TranslationArtifact(BaseModel):
    """A generated runtime artifact for one Component / process (translator output)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    component_id: UUID
    tier: Tier
    code_path: str = Field(description="Path inside the generated package")
    code_hash: str = Field(description="SHA-256 of generated code; Engram anchor key")
    translator_version: str
    is_dual_target: bool = Field(default=False, description="Tier1↔Tier2 boundary case")


class ShadowComparison(BaseModel):
    """One observation from the shadow executor."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    process_id: UUID
    cdc_event_replay_id: str | None = Field(
        default=None,
        description="None means the comparison was driven by Compare Mode log replay.",
    )
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    diverged: bool
    category: DivergenceCategory | None = Field(
        default=None,
        description="Required when diverged=True.",
    )
    field_diffs: dict[str, tuple[Any, Any]] = Field(
        default_factory=dict,
        description="Field name -> (production_value, runtime_value).",
    )
    engram_anchor: str | None = None


class RoutingDecision(BaseModel):
    """One per-record cutover routing decision."""

    model_config = ConfigDict(extra="forbid")

    process_id: UUID
    record_id: str
    routed_to: Annotated[str, Field(pattern="^(salesforce|runtime)$")]
    stage_percent: Annotated[int, Field(ge=0, le=100)]
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    engram_anchor: str
    f44_anchor: str | None = None
