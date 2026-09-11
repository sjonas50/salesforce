"""Static health checks and generated verify recipes (schema + process model, no org)."""

from __future__ import annotations

from typing import Any

from offramp.core.models import (
    CategoryName,
    Component,
    Provenance,
    SchemaNode,
    SchemaNodeKind,
    SchemaSnapshot,
)
from offramp.core.process import (
    Branch,
    Condition,
    ConditionGroup,
    ProcessDefinition,
    Step,
    StepKind,
    Trigger,
    TriggerKind,
    Variable,
)
from offramp.understand.health import run_health_checks, summarize
from offramp.verify.recipes import generate_recipes, value_for

ORG = "unit"


def _field(obj: str, name: str, ftype: str, **kw: Any) -> SchemaNode:
    return SchemaNode(
        org_alias=ORG,
        kind=SchemaNodeKind.FIELD,
        api_name=f"{obj}.{name}",
        object_name=obj,
        field_type=ftype,
        **kw,
    )


SCHEMA = SchemaSnapshot(
    org_alias=ORG,
    nodes=[
        SchemaNode(org_alias=ORG, kind=SchemaNodeKind.OBJECT, api_name="Lead", object_name="Lead"),
        _field("Lead", "Name", "Text", required=True, raw={"createable": False}),
        _field("Lead", "LastName", "Text", required=True),
        _field("Lead", "Company", "Text", required=True),
        _field("Lead", "Email", "Email"),
        _field("Lead", "Country__c", "Text", custom=True),
        _field("Lead", "Score__c", "Number", custom=True),
        _field("Lead", "OwnerId", "Lookup", required=True, reference_to=["User"]),
        _field(
            "Lead",
            "Status",
            "Picklist",
            picklist_values=["Open - Not Contacted", "Working - Contacted", "Closed - Converted"],
        ),
        _field("Lead", "Rating", "Picklist", picklist_values=["Hot", "Warm", "Cold"]),
        _field("Case", "Priority", "Picklist", picklist_values=["High", "Medium", "Low"]),
    ],
)


def _component(cat: CategoryName, name: str, raw: dict[str, Any]) -> Component:
    return Component(
        org_alias=ORG,
        category=cat,
        name=name,
        api_name=name,
        raw=raw,
        content_hash="0" * 64,
        provenance=Provenance(source_tool="unit", source_version="0"),
    )


def _flow(
    name: str,
    *,
    kind: str = "record_triggered_flow",
    trigger: Trigger,
    steps: list[Step] | None = None,
    variables: list[Variable] | None = None,
) -> ProcessDefinition:
    return ProcessDefinition(
        name=name, kind=kind, trigger=trigger, steps=steps or [], variables=variables or []
    )


def _cond(left: str, op: str, right: Any) -> Condition:
    return Condition(left=left, operator=op, right=right)


LEAD_ROUTING = _flow(
    "LeadRouting",
    trigger=Trigger(
        kind=TriggerKind.RECORD_SAVE,
        object="Lead",
        events=["create", "update"],
        timing="after",
        when=ConditionGroup(conditions=[_cond("Lead.Status", "EqualTo", "Open - Not Contacted")]),
    ),
    steps=[
        Step(id="ScoreLead", kind=StepKind.CALL_CODE, target="LeadScoringService"),
        Step(
            id="Route",
            kind=StepKind.DECISION,
            branches=[
                Branch(
                    name="Hot",
                    when=ConditionGroup(conditions=[_cond("Lead.Rating", "EqualTo", "Blazing")]),
                )
            ],
        ),
        Step(
            id="AssignOwner",
            kind=StepKind.UPDATE,
            object="Lead",
            inputs={"Lead.Rating": "Hot", "Lead.Status": "Nurturing"},
            extras={"input": "$Record"},
        ),
    ],
)


def test_health_flags_unknown_picklist_values_everywhere() -> None:
    findings = run_health_checks([], [LEAD_ROUTING], SCHEMA)
    picks = [f for f in findings if f.code == "picklist_value_unknown"]
    assert {(f.details["field"], f.details["value"]) for f in picks} == {
        ("Lead.Rating", "Blazing"),
        ("Lead.Status", "Nurturing"),
    }
    assert all(f.severity == "error" for f in picks)
    # rule criteria list alternatives with commas; multi-select with semicolons
    rule = _flow(
        "Case.Standard",
        kind="assignment_rule",
        trigger=Trigger(kind=TriggerKind.RECORD_SAVE, object="Case", events=["create"]),
        steps=[
            Step(
                id="match",
                kind=StepKind.DECISION,
                branches=[
                    Branch(
                        name="e1",
                        when=ConditionGroup(
                            conditions=[_cond("Case.Priority", "EqualTo", "High, Medium")]
                        ),
                    ),
                    Branch(
                        name="e2",
                        when=ConditionGroup(
                            conditions=[_cond("Case.Priority", "EqualTo", "High;Urgent")]
                        ),
                    ),
                ],
            )
        ],
    )
    findings = run_health_checks([], [rule], SCHEMA)
    assert [f.details["value"] for f in findings if f.code == "picklist_value_unknown"] == [
        "Urgent"
    ]


def test_health_callout_in_save_path_and_deferred_variant() -> None:
    sync = _component(
        CategoryName.APEX_CLASS,
        "LeadScoringService",
        {
            "analysis": {"callouts": ["Http"], "async_calls": [], "entry_points": ["invocable"]},
            "references": {"apex_classes": []},
        },
    )
    findings = run_health_checks([sync], [LEAD_ROUTING], SCHEMA)
    hazard = [f for f in findings if f.code == "callout_in_save_path"]
    assert len(hazard) == 1 and hazard[0].severity == "error"
    assert "uncommitted work pending" in hazard[0].message

    deferred = _component(
        CategoryName.APEX_CLASS,
        "LeadScoringService",
        {
            "analysis": {
                "callouts": ["Http"],
                "async_calls": [{"mechanism": "enqueue", "target_class": "ScoreJob"}],
                "entry_points": ["invocable"],
            },
            "references": {"apex_classes": ["ScoreJob"]},
        },
    )
    findings = run_health_checks([deferred], [LEAD_ROUTING], SCHEMA)
    assert not [f for f in findings if f.code == "callout_in_save_path"]
    assert [f.code for f in findings if f.severity == "info" and "callout" in f.code] == [
        "callout_deferred"
    ]

    # a handler without callouts delegating to a service that has one (one hop)
    handler = _component(
        CategoryName.APEX_CLASS,
        "LeadScoringService",
        {
            "analysis": {"callouts": [], "async_calls": [], "entry_points": ["invocable"]},
            "references": {"apex_classes": ["ScoringClient"]},
        },
    )
    client = _component(
        CategoryName.APEX_CLASS,
        "ScoringClient",
        {"analysis": {"callouts": ["HttpRequest"], "async_calls": []}, "references": {}},
    )
    findings = run_health_checks([handler, client], [LEAD_ROUTING], SCHEMA)
    hop = [f for f in findings if f.code == "callout_in_save_path"]
    assert len(hop) == 1 and hop[0].details == {
        "class": "ScoringClient",
        "via": "LeadScoringService",
    }


def test_health_same_object_update_owner_alert_and_triggers() -> None:
    findings = run_health_checks([], [LEAD_ROUTING], SCHEMA)
    same = next(f for f in findings if f.code == "same_object_update_after_save")
    assert same.severity == "info" and same.details["guarded"] is True  # entry condition present
    assert next(f for f in findings if f.code == "no_fault_path").details["steps"] == [
        "ScoreLead",
        "AssignOwner",
    ]

    unguarded = LEAD_ROUTING.model_copy(
        update={"trigger": LEAD_ROUTING.trigger.model_copy(update={"when": ConditionGroup()})}
    )
    assert (
        next(
            f
            for f in run_health_checks([], [unguarded], SCHEMA)
            if f.code == "same_object_update_after_save"
        ).severity
        == "warning"
    )

    assignment = _flow(
        "Lead.RouteToInsideSales",
        kind="assignment_rule",
        trigger=Trigger(kind=TriggerKind.RECORD_SAVE, object="Lead", events=["create"]),
        steps=[
            Step(
                id="entry1",
                kind=StepKind.UPDATE,
                object="Lead",
                inputs={"Lead.OwnerId": {"ref": "Queue:Inside_Sales"}},
            )
        ],
    )
    alert = _flow(
        "Lead.WelcomeNewLead",
        kind="workflow_rule",
        trigger=Trigger(kind=TriggerKind.RECORD_SAVE, object="Lead", events=["create"]),
        steps=[
            Step(
                id="a1",
                kind=StepKind.NOTIFY,
                target="Lead.Welcome_Lead_Alert",
                extras={"channel": "email", "recipients": ["owner"]},
            )
        ],
    )
    findings = run_health_checks([], [assignment, alert], SCHEMA)
    owner = next(f for f in findings if f.code == "owner_alert_after_queue_assignment")
    assert owner.component == "Lead.WelcomeNewLead" and owner.details["queues"] == ["Inside_Sales"]

    triggers = [
        _component(CategoryName.APEX_TRIGGER, "LeadA", {"sobject": "Lead", "status": "Active"}),
        _component(CategoryName.APEX_TRIGGER, "LeadB", {"sobject": "Lead", "status": "Active"}),
        _component(CategoryName.APEX_TRIGGER, "LeadC", {"sobject": "Lead", "status": "Inactive"}),
    ]
    findings = run_health_checks(triggers, [], SCHEMA)
    multi = next(f for f in findings if f.code == "multiple_triggers_per_object")
    assert multi.details["triggers"] == ["LeadA", "LeadB"]
    assert summarize(findings) == {"errors": 0, "warnings": 1, "infos": 0}


def test_recipe_for_record_triggered_flow_satisfies_entry_and_required_fields() -> None:
    validation = _flow(
        "Lead.Email_Required",
        kind="validation_rule",
        trigger=Trigger(
            kind=TriggerKind.RECORD_SAVE,
            object="Lead",
            events=["create", "update"],
            when=ConditionGroup(
                conditions=[
                    Condition(expression="AND(ISPICKVAL(LeadSource, 'Web'), ISBLANK(Email))")
                ]
            ),
        ),
    )
    other = _flow(
        "WarmLeadAlert",
        trigger=Trigger(
            kind=TriggerKind.RECORD_SAVE,
            object="Lead",
            events=["create"],
            when=ConditionGroup(conditions=[_cond("Lead.Rating", "EqualTo", "Warm")]),
        ),
    )
    recipes = generate_recipes([LEAD_ROUTING, validation, other], SCHEMA)
    r = recipes["LeadRouting"]
    assert r["object"] == "Lead" and r["_generated"] is True
    assert r["create"]["Status"] == "Open - Not Contacted"  # entry condition
    assert r["create"]["LastName"] and r["create"]["Company"]  # required
    assert "Name" not in r["create"]  # compound / not createable
    assert r["create"]["Email"] == "verify@example.com"  # ISBLANK guard from the validation rule
    assert r["create"]["Rating"] == "Hot"  # keeps WarmLeadAlert quiet
    assert "WarmLeadAlert" in r["_why"]
    assert "update" not in r
    assert "_needs" not in r  # OwnerId is a lookup but is skipped, not demanded

    w = recipes["WarmLeadAlert"]
    assert w["create"]["Rating"] == "Warm" and w["create"]["Status"] == "Working - Contacted"


def test_recipe_update_only_flow_moves_criteria_to_the_update_step() -> None:
    upd = _flow(
        "OnClose",
        trigger=Trigger(
            kind=TriggerKind.RECORD_SAVE,
            object="Lead",
            events=["update"],
            requires_change=True,
            when=ConditionGroup(
                conditions=[
                    _cond("Lead.Status", "EqualTo", "Closed - Converted"),
                    _cond("Lead.Score__c", "GreaterThan", "10"),
                ]
            ),
        ),
    )
    r = generate_recipes([upd], SCHEMA)["OnClose"]
    assert r["update"] == {"Status": "Closed - Converted", "Score__c": 11}
    assert "Status" not in r["create"]
    assert "become true" in r["_why"]


def test_recipe_autolaunched_flow_gets_typed_inputs_and_a_record() -> None:
    auto = _flow(
        "SendWelcomeEmail",
        kind="autolaunched_flow",
        trigger=Trigger(kind=TriggerKind.INVOCATION),
        steps=[Step(id="GetLead", kind=StepKind.LOOKUP, object="Lead")],
        variables=[
            Variable(name="recordId", type="String", is_input=True),
            Variable(name="threshold", type="Number", is_input=True),
            Variable(name="dryRun", type="Boolean", is_input=True),
            Variable(name="account", type="SObject", object="Account", is_input=True),
        ],
    )
    r = generate_recipes([auto], SCHEMA)["SendWelcomeEmail"]
    assert r["inputs"] == {"recordId": "$record.Id", "threshold": 1, "dryRun": True}
    assert r["object"] == "Lead" and r["create"]["LastName"]
    assert r["_needs"] == ["input account: SObject Account — supply"]


def test_recipe_respects_existing_and_skips_unverifiable_flows() -> None:
    sched = _flow(
        "Nightly",
        kind="schedule_triggered_flow",
        trigger=Trigger(kind=TriggerKind.SCHEDULED, object="Lead"),
    )
    existing = {"LeadRouting": {"object": "Lead", "create": {"LastName": "Mine"}}}
    recipes = generate_recipes([LEAD_ROUTING, sched], SCHEMA, existing=existing)
    assert recipes["LeadRouting"] == existing["LeadRouting"]
    assert "Nightly" not in recipes
    assert generate_recipes([LEAD_ROUTING], SCHEMA, only=["Other"]) == {}


def test_value_for_types() -> None:
    assert value_for(_field("X", "A", "Email")) == "verify@example.com"
    assert value_for(_field("X", "B", "Checkbox")) is False
    assert value_for(_field("X", "C", "Currency")) == 1
    assert value_for(_field("X", "D", "Lookup")) is None
    assert value_for(_field("X", "E", "Picklist", picklist_values=[])) is None
    assert value_for(_field("X", "F", "Formula", formula="1+1")) is None
