"""Dynamic dispatch resolver: confidence scoring + framework detection."""

from __future__ import annotations

from offramp.extract.dispatch.class_resolver import resolve
from offramp.extract.dispatch.cmt_reader import CMTRecord
from offramp.extract.dispatch.framework_detectors import detect


def test_exact_match_scores_one() -> None:
    edges = resolve(
        [CMTRecord("Trigger_Action__mdt", "X", {"Apex_Class__c": "LeadHandler"})],
        {"LeadHandler"},
    )
    assert len(edges) == 1
    assert edges[0].handler_class == "LeadHandler"
    assert edges[0].confidence == 1.0


def test_case_insensitive_match_scores_point_nine() -> None:
    edges = resolve(
        [CMTRecord("Trigger_Action__mdt", "X", {"Apex_Class__c": "leadhandler"})],
        {"LeadHandler"},
    )
    assert len(edges) == 1
    assert edges[0].confidence == 0.9


def test_unmatched_value_drops_silently() -> None:
    edges = resolve(
        [CMTRecord("Trigger_Action__mdt", "X", {"Apex_Class__c": "TotallyMissing"})],
        {"LeadHandler"},
    )
    assert edges == []


def test_trigger_actions_framework_detected_strongly() -> None:
    signals = detect({"LeadHandler"}, {"Trigger_Action__mdt"})
    assert any(s.framework == "trigger_actions" and s.confidence >= 0.9 for s in signals)


def test_kevin_ohara_framework_detected() -> None:
    classes = {"TriggerHandler", "LeadTriggerHandler", "AccountTriggerHandler"}
    signals = detect(classes, set())
    assert any(s.framework == "kevin_ohara" and s.confidence >= 0.8 for s in signals)
