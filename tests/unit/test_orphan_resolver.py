"""Orphan resolver — channel ordering + confidence."""

from __future__ import annotations

from offramp.core.models import CategoryName, Component, Provenance
from offramp.understand.orphan.resolver import ResolutionInputs, resolve_orphans


def _apex_class(name: str) -> Component:
    return Component(
        org_alias="t",
        category=CategoryName.APEX_CLASS,
        name=name,
        api_name=name,
        content_hash="0" * 64,
        provenance=Provenance(source_tool="t", source_version="0", api_version="66.0"),
    )


def _lwc_calling(class_name: str) -> Component:
    return Component(
        org_alias="t",
        category=CategoryName.LWC_BUNDLE,
        name="leadCard",
        api_name="leadCard",
        raw={"apex_imports": [f"{class_name}.method"]},
        content_hash="0" * 64,
        provenance=Provenance(source_tool="t", source_version="0", api_version="66.0"),
    )


def test_orphan_with_lwc_caller_is_resolved() -> None:
    inputs = ResolutionInputs(
        components=[_apex_class("LeadController"), _lwc_calling("LeadController")],
    )
    report = resolve_orphans(inputs)
    assert len(report.resolved) == 1
    assert report.resolved[0].channel == "lwc_import"
    assert report.unresolved == []


def test_runtime_log_beats_other_channels() -> None:
    inputs = ResolutionInputs(
        components=[_apex_class("LeadController"), _lwc_calling("LeadController")],
        runtime_log_class_invocations={"LeadController"},
    )
    report = resolve_orphans(inputs)
    # Runtime log is highest-confidence channel (0.99) and should win.
    assert report.resolved[0].channel == "runtime_log"
    assert report.resolved[0].confidence >= 0.99


def test_truly_unreferenced_class_is_unresolved() -> None:
    inputs = ResolutionInputs(components=[_apex_class("DeadCode")])
    report = resolve_orphans(inputs)
    assert report.unresolved == ["DeadCode"]
    assert report.resolved == []


def test_named_credential_channel() -> None:
    inputs = ResolutionInputs(
        components=[_apex_class("WebhookHandler")],
        named_credential_endpoints={"WebhookHandler": "https://nc/webhook"},
    )
    report = resolve_orphans(inputs)
    assert report.resolved[0].channel == "named_credential"
    assert "https://nc/webhook" in report.resolved[0].evidence
