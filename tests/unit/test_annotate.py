"""Annotation: dossiers, tier hints, earned confidence, process narratives, persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from offramp.core.models import CategoryName, Component, Provenance
from offramp.engram.client import InMemoryEngramClient
from offramp.understand.annotate import (
    Annotator,
    _earned_confidence,
    _RateLimiter,
    load_annotations,
    load_process_annotations,
    save_annotations,
    save_process_annotations,
)
from offramp.understand.annotate_context import DossierInputs, build_dossiers
from offramp.understand.clustering import BusinessProcess
from offramp.understand.tier_rules import tier_hint

ORG = "unit"


def _component(cat: CategoryName, name: str, raw: dict[str, Any], **kw: Any) -> Component:
    return Component(
        org_alias=ORG,
        category=cat,
        name=name,
        api_name=name,
        raw=raw,
        content_hash=("%064x" % abs(hash((cat.value, name))))[:64],
        provenance=Provenance(source_tool="unit", source_version="0"),
        **kw,
    )


APEX_BODY = """
public with sharing class LeadScoringService {
    @InvocableMethod public static void score(List<Id> ids) {
        Lead l = [SELECT Id, Email FROM Lead WHERE Id = :ids[0]];
        System.enqueueJob(new ScoreJob(l));
    }
}
"""


def _apex(name: str = "LeadScoringService", **analysis: Any) -> Component:
    base = {
        "callouts": ["Http"],
        "named_credentials": ["ScoringAPI"],
        "async_calls": [{"mechanism": "enqueue", "target_class": "ScoreJob"}],
        "entry_points": ["invocable"],
        "dml": [{"op": "update", "sobject": "Lead"}],
        "soql": [{"sobject": "Lead"}],
    }
    base.update(analysis)
    return _component(
        CategoryName.APEX_CLASS,
        name,
        {
            "has_body": True,
            "body": APEX_BODY,
            "status": "Active",
            "entry_points": base["entry_points"],
            "analysis": base,
            "references": {"fields_written": ["Lead.Score__c"], "apex_classes": ["ScoreJob"]},
        },
    )


class _FakeBackend:
    """Replays canned JSON; records prompts."""

    model = "fake-model"

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.answer = answer or {}
        self.prompts: list[str] = []

    async def complete_json(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        self.prompts.append(user)
        return dict(self.answer)


def _annotator(answer: dict[str, Any] | None = None) -> tuple[Annotator, _FakeBackend]:
    backend = _FakeBackend(answer)
    return (
        Annotator(
            backend=backend,
            engram=InMemoryEngramClient(),
            rate_limiter=_RateLimiter.create(600),
        ),
        backend,
    )


GOOD_ANSWER = {
    "summary": "Scores a lead through an external API asynchronously.",
    "narrative": "Invoked from a flow; reads the Lead; enqueues ScoreJob which calls ScoringAPI.",
    "domain": "sales",
    "complexity_band": "medium",
    "recommended_tier": "tier2_temporal",
    "tier_reasoning": "Callout via named credential and a queueable job.",
    "evidence": ["named credentials: ScoringAPI", "async work: enqueue ScoreJob"],
    "unknowns": [],
    "self_confidence": 0.9,
}


def test_tier_hint_from_static_signals() -> None:
    hint = tier_hint(_apex(), [])
    assert hint.tier == "tier2_temporal"
    assert any("callouts" in r for r in hint.reasons)
    plain = _apex("Plain", callouts=[], named_credentials=[], async_calls=[], entry_points=[])
    assert tier_hint(plain, []).tier == "tier1_rules"
    mail = _apex("Inbound", callouts=[], async_calls=[], entry_points=["inbound_email"])
    assert tier_hint(mail, []).tier == "tier3_langgraph"


def test_dossier_carries_facts_source_and_hint() -> None:
    d = build_dossiers(DossierInputs(components=[_apex()], org_alias=ORG))
    doss = next(iter(d.values()))
    assert doss.source_available and not doss.truncated
    assert "## FACTS" in doss.text and "callout types used: Http" in doss.text
    assert "tier hint from static rules: tier2_temporal" in doss.text
    assert "## SOURCE" in doss.text and "enqueueJob" in doss.text
    assert "## PROCESS MODEL" in doss.text


def test_dossier_truncates_source_to_budget_and_says_so() -> None:
    big = _apex()
    big.raw["body"] = "// x\n" * 5000
    d = build_dossiers(DossierInputs(components=[big], org_alias=ORG, budget=6000))
    doss = next(iter(d.values()))
    assert doss.truncated and "[... truncated" in doss.text
    assert doss.chars <= 6500


def test_managed_code_without_source_is_flagged_not_guessed() -> None:
    hidden = _component(
        CategoryName.APEX_CLASS,
        "PostInstallScript",
        {"has_body": False, "body": "", "analysis": {}, "references": {}, "is_valid": True},
        namespace="devedapp",
    )
    pkgs = [{"namespace": "devedapp", "name": "Developer Edition", "version": "v0.9"}]
    d = next(iter(build_dossiers(DossierInputs(components=[hidden], packages=pkgs)).values()))
    assert not d.source_available
    assert "managed package: Developer Edition v0.9" in d.text
    assert "SOURCE NOT AVAILABLE" in d.text


def test_empty_sharing_rules_are_annotated_deterministically() -> None:
    empty = _component(
        CategoryName.SHARING_RULE,
        "Account",
        {"object": "Account", "criteria_rules": [], "owner_rules": [], "references": {}},
    )
    d = next(iter(build_dossiers(DossierInputs(components=[empty])).values()))
    assert d.deterministic is not None and d.deterministic["confidence"] == 1.0
    real = _component(
        CategoryName.SHARING_RULE,
        "Account",
        {
            "object": "Account",
            "criteria_rules": [{"name": "Strategic", "access_level": "Read"}],
            "owner_rules": [],
            "references": {},
        },
    )
    d2 = next(iter(build_dossiers(DossierInputs(components=[real])).values()))
    assert d2.deterministic is None and "Strategic" in d2.text


def test_earned_confidence_rules() -> None:
    d = next(iter(build_dossiers(DossierInputs(components=[_apex()])).values()))
    conf, review, notes = _earned_confidence(GOOD_ANSWER, d)
    assert conf == 0.95 and not review and any("agrees" in n for n in notes)
    # unknowns cap at 0.8; contradicting the hint caps at 0.6 and flags review
    conf, review, _ = _earned_confidence({**GOOD_ANSWER, "unknowns": ["who calls it"]}, d)
    assert conf == 0.8 and not review
    conf, review, _ = _earned_confidence({**GOOD_ANSWER, "recommended_tier": "tier1_rules"}, d)
    assert conf == 0.6 and review
    # no evidence caps at 0.5; hidden source caps at 0.5 regardless of self-confidence
    conf, _, _ = _earned_confidence({**GOOD_ANSWER, "evidence": []}, d)
    assert conf == 0.5
    d.source_available = False
    conf, _, _ = _earned_confidence(GOOD_ANSWER, d)
    assert conf == 0.5


@pytest.mark.asyncio
async def test_annotate_many_uses_dossiers_and_skips_failures() -> None:
    annotator, backend = _annotator(GOOD_ANSWER)
    empty = _component(
        CategoryName.SHARING_RULE,
        "Case",
        {"object": "Case", "criteria_rules": [], "references": {}},
    )
    comps = [_apex(), empty]
    anns = await annotator.annotate_many(comps)
    assert len(anns) == 2
    llm = next(a for a in anns if not a.deterministic)
    assert llm.recommended_tier == "tier2_temporal" and llm.confidence == 0.95
    assert llm.tier_hint == "tier2_temporal" and llm.evidence and llm.engram_anchor
    det = next(a for a in anns if a.deterministic)
    assert det.model == "rules" and det.confidence == 1.0 and "No sharing rules" in det.summary
    assert len(backend.prompts) == 1 and "## FACTS" in backend.prompts[0]

    # a backend failure skips the component instead of ending the pass
    class _Boom(_FakeBackend):
        async def complete_json(self, system: str, user: str, max_tokens: int) -> dict[str, Any]:
            raise RuntimeError("boom")

    annotator.backend = _Boom()
    assert [a.deterministic for a in await annotator.annotate_many(comps)] == [True]


@pytest.mark.asyncio
async def test_process_annotation_aggregates_members(tmp_path: Path) -> None:
    annotator, backend = _annotator(GOOD_ANSWER)
    a, b = _apex(), _apex("ScoreJob", callouts=["Http"], async_calls=[], entry_points=["queueable"])
    anns = await annotator.annotate_many([a, b])
    backend.answer = {
        "name": "Lead scoring",
        "narrative": "LeadScoringService enqueues ScoreJob which calls the scoring API.",
        "domain": "sales",
        "entry_points": ["flow invocation"],
        "risks": ["callout depends on ScoringAPI availability"],
        "self_confidence": 0.8,
    }
    procs = [
        BusinessProcess("1", "cluster 1", [str(a.id), str(b.id)], object_names=["Lead"]),
        BusinessProcess("2", "lonely", [str(a.id)]),
    ]
    pas = await annotator.annotate_processes(procs, [a, b], anns)
    assert len(pas) == 1
    pa = pas[0]
    assert pa.name == "Lead scoring" and pa.members == ["LeadScoringService", "ScoreJob"]
    assert pa.tier_mix == {"tier2_temporal": 2}
    assert pa.confidence == 0.8  # min(self, mean of members 0.95)
    assert "LeadScoringService — Scores a lead" in backend.prompts[-1]

    out = tmp_path / "process_annotations.json"
    save_process_annotations(pas, out)
    assert load_process_annotations(out)[0].name == "Lead scoring"


@pytest.mark.asyncio
async def test_annotations_roundtrip_by_content_hash(tmp_path: Path) -> None:
    annotator, _ = _annotator(GOOD_ANSWER)
    c = _apex()
    anns = await annotator.annotate_many([c])
    path = tmp_path / "annotations.json"
    save_annotations(anns, [c], path)
    row = json.loads(path.read_text())[0]
    assert row["content_hash"] == c.content_hash and row["needs_review"] is False
    # a re-extracted copy has a new id but the same hash
    again = _apex()
    loaded = load_annotations(path, [again])
    assert len(loaded) == 1 and loaded[0].component_id == str(again.id)
    assert loaded[0].evidence == GOOD_ANSWER["evidence"] and loaded[0].tier_hint == "tier2_temporal"
    # an older file without the new fields still loads
    path.write_text(
        json.dumps(
            [
                {
                    "component_id": "x",
                    "content_hash": again.content_hash,
                    "summary": "old",
                    "domain": "sales",
                    "complexity_band": "low",
                    "recommended_tier": "tier1_rules",
                    "confidence": 0.7,
                    "model": "old",
                }
            ]
        )
    )
    assert load_annotations(path, [again])[0].summary == "old"


def test_extract_json_repairs_escaped_apostrophes() -> None:
    from offramp.understand.annotate import _extract_json

    raw = 'Here you go:\n{"summary": "Routes Leads (status \\\'Open - Not Contacted\\\') by country", "n": 1}'
    assert _extract_json(raw)["summary"].startswith("Routes Leads (status 'Open")
    fenced = '```json\n{"a": 1}\n```'
    assert _extract_json(fenced) == {"a": 1}
    with pytest.raises(ValueError):
        _extract_json("no json here")
