"""Deterministic translation-tier hints from static facts (v2.1 plan §5.4 tier mapping).

The LLM annotator receives these as facts to confirm or contradict; a
contradiction lowers confidence and flags the component for review. Rules are
conservative: they only claim a tier when a concrete signal exists.

* ``tier2_temporal`` — anything that waits, calls out, runs later or needs a
  human step: callouts / named credentials, async Apex (queueable, batch,
  future, scheduled), wait steps, approval processes, orchestrations,
  scheduled / platform-event flows, time-based escalations, outbound messages.
* ``tier3_langgraph`` — interpretation of unstructured input: inbound-email
  handlers, text classification signals (the model is asked to look for more).
* ``tier1_rules`` — everything else: synchronous validation, computation,
  assignment, field updates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from offramp.core.models import CategoryName, Component
from offramp.core.process import ProcessDefinition, StepKind, TriggerKind

_TIER2_CATEGORIES = {
    CategoryName.APPROVAL_PROCESS,
    CategoryName.FLOW_ORCHESTRATION,
    CategoryName.SCHEDULE_TRIGGERED_FLOW,
    CategoryName.PLATFORM_EVENT_TRIGGERED_FLOW,
    CategoryName.ESCALATION_RULE,
    CategoryName.PLATFORM_EVENT,
}
_TIER2_ENTRY_POINTS = {"batchable", "schedulable", "queueable", "future"}
_TIER3_ENTRY_POINTS = {"inbound_email"}
_TIER2_STEP_KINDS = {StepKind.WAIT, StepKind.CALL_ACTION}
# Flow "actions" that only navigate or render UI: not external work, not a wait.
_UI_ACTION_TYPES = {"openRecordAction", "component", "quickAction", "createDraftFromOrgTemplate"}


@dataclass
class TierHint:
    tier: str
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, list[str]] = field(default_factory=dict)  # named facts for the dossier


def class_facts(components: list[Component]) -> dict[str, dict[str, list[str]]]:
    """Per Apex class (lower-case name): callouts and async targets, for callers' hints."""
    out: dict[str, dict[str, list[str]]] = {}
    for c in components:
        if c.category is not CategoryName.APEX_CLASS or not isinstance(c.raw, dict):
            continue
        a = c.raw.get("analysis") or {}
        out[c.name.lower()] = {
            "callouts": [str(x) for x in a.get("callouts") or []],
            "async": [
                str(x.get("target_class") or x.get("mechanism")) for x in a.get("async_calls") or []
            ],
        }
    return out


def tier_hint(
    component: Component,
    definitions: list[ProcessDefinition],
    classes: dict[str, dict[str, list[str]]] | None = None,
) -> TierHint:
    """Deterministic tier for one component; ``classes`` lets a caller inherit its callee's signals."""
    reasons: list[str] = []
    signals: dict[str, list[str]] = {}
    raw = component.raw if isinstance(component.raw, dict) else {}
    analysis = raw.get("analysis") or {}
    cat = component.category
    classes = classes or {}

    entry_points = [str(e) for e in (raw.get("entry_points") or analysis.get("entry_points") or [])]
    callouts = [str(c) for c in analysis.get("callouts") or []]
    creds = [str(c) for c in analysis.get("named_credentials") or []]
    async_calls = [
        f"{a.get('mechanism')}→{a.get('target_class') or '?'}"
        for a in analysis.get("async_calls") or []
    ]
    if entry_points:
        signals["entry_points"] = entry_points
    if callouts:
        signals["callouts"] = callouts
    if creds:
        signals["named_credentials"] = creds
    if async_calls:
        signals["async_calls"] = async_calls

    tier3 = False
    if set(e.lower() for e in entry_points) & _TIER3_ENTRY_POINTS:
        tier3 = True
        reasons.append("inbound email handler interprets free text")

    tier2 = False
    if cat in _TIER2_CATEGORIES:
        tier2 = True
        reasons.append(f"{cat.value.replace('_', ' ')} is asynchronous or long-running by nature")
    if callouts or creds:
        tier2 = True
        reasons.append(f"makes callouts ({', '.join(callouts or creds)})")
    if async_calls:
        tier2 = True
        reasons.append(f"enqueues async work ({', '.join(async_calls)})")
    if set(e.lower() for e in entry_points) & _TIER2_ENTRY_POINTS:
        tier2 = True
        reasons.append(
            "runs as async Apex ("
            + ", ".join(e for e in entry_points if e.lower() in _TIER2_ENTRY_POINTS)
            + ")"
        )
    for d in definitions:
        if d.trigger.kind in {
            TriggerKind.SCHEDULED,
            TriggerKind.PLATFORM_EVENT,
            TriggerKind.APPROVAL_SUBMIT,
        }:
            tier2 = True
            reasons.append(f"{d.name}: triggered by {d.trigger.kind.value.replace('_', ' ')}")
        waits = [
            s.id
            for s in d.steps
            if s.kind in _TIER2_STEP_KINDS
            and str(s.extras.get("action_type", "")) not in _UI_ACTION_TYPES
        ]
        if waits:
            tier2 = True
            reasons.append(f"{d.name}: wait / external action steps ({', '.join(waits[:4])})")
        subflows = [s.id for s in d.steps if s.kind is StepKind.CALL_PROCESS]
        if subflows:
            signals.setdefault("subflows", []).extend(subflows)
        code = [s.target or s.id for s in d.steps if s.kind is StepKind.CALL_CODE]
        if code:
            signals.setdefault("calls_code", []).extend(code)
            for target in code:
                facts = classes.get(target.lower())
                if facts and (facts["callouts"] or facts["async"]):
                    tier2 = True
                    reasons.append(
                        f"{d.name}: calls Apex {target}, which "
                        + " and ".join(
                            x
                            for x in (
                                "makes callouts" if facts["callouts"] else "",
                                f"enqueues async work ({', '.join(facts['async'])})"
                                if facts["async"]
                                else "",
                            )
                            if x
                        )
                    )
        if raw.get("outbound_messages"):
            tier2 = True
            reasons.append("sends outbound messages")

    if tier3:
        return TierHint("tier3_langgraph", reasons, signals)
    if tier2:
        return TierHint("tier2_temporal", reasons, signals)
    reasons.append("synchronous, no callouts, no async work, no waits")
    return TierHint("tier1_rules", reasons, signals)
