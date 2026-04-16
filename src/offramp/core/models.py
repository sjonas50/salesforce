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
    """The 21 Salesforce automation categories defined in the v2.1 reference.

    Order matches the v2.1 reference document (§7.3 table). Do not reorder
    without coordinating with the OoE Surface Audit (C4) which keys on this enum.
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
    """One piece of Salesforce automation as extracted by Phase 1.

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


class Dependency(BaseModel):
    """Edge in the Component graph."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    target_id: UUID
    kind: DependencyKind
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    notes: str | None = None


class AST(BaseModel):
    """Parsed AST attached to a Component (Phase 1 output, Phase 3 input)."""

    model_config = ConfigDict(extra="forbid")

    component_id: UUID
    parser: str = Field(description="e.g. 'summit-ast', 'lightning-flow-scanner', 'tree-sitter'")
    parser_version: str
    tree: dict[str, Any] = Field(description="Parser-native serialization")


class TranslationArtifact(BaseModel):
    """A generated runtime artifact for one Component / process (Phase 3 output)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    component_id: UUID
    tier: Tier
    code_path: str = Field(description="Path inside the generated package")
    code_hash: str = Field(description="SHA-256 of generated code; Engram anchor key")
    translator_version: str
    is_dual_target: bool = Field(default=False, description="Tier1↔Tier2 boundary case")


class ShadowComparison(BaseModel):
    """One observation from the shadow executor (Phase 4 output)."""

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
    """One per-record cutover routing decision (Phase 5 output)."""

    model_config = ConfigDict(extra="forbid")

    process_id: UUID
    record_id: str
    routed_to: Annotated[str, Field(pattern="^(salesforce|runtime)$")]
    stage_percent: Annotated[int, Field(ge=0, le=100)]
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    engram_anchor: str
    f44_anchor: str | None = None
