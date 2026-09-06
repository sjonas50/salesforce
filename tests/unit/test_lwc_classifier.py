"""LWC classifier — UI-only / mixed / business-logic-heavy classification."""

from __future__ import annotations

from offramp.core.models import CategoryName
from offramp.extract.lwc.bundle import LWCClassification, analyze_js


def test_ui_only_short_file() -> None:
    src = """
    import { LightningElement } from 'lwc';
    export default class HelloWorld extends LightningElement {}
    """
    out = analyze_js("hello.js", src)
    assert out.classification is LWCClassification.UI_ONLY
    assert out.apex_imports == ()


def test_mixed_with_a_couple_of_apex_imports() -> None:
    src = """
    import { LightningElement, wire } from 'lwc';
    import getLead from '@salesforce/apex/LeadController.getLead';

    export default class LeadCard extends LightningElement {
        @wire(getLead, { leadId: '$recordId' })
        wiredLead;
    }
    """
    out = analyze_js("leadCard.js", src)
    assert "LeadController.getLead" in out.apex_imports
    assert out.wire_calls == 1
    assert out.classification in {LWCClassification.MIXED, LWCClassification.BUSINESS_LOGIC_HEAVY}


def test_business_logic_heavy_file() -> None:
    src = "\n".join(
        [
            "import { LightningElement, wire } from 'lwc';",
            "import a from '@salesforce/apex/A.x';",
            "import b from '@salesforce/apex/B.y';",
            "import c from '@salesforce/apex/C.z';",
            "export default class Big extends LightningElement {",
            "  @wire(a) wa;",
            "  @wire(b) wb;",
            "  @wire(c) wc;",
            "  m1() { a({k:1}).then((r) => { if (r) {} }); }",
            "  m2() { b({k:1}).then((r) => { if (r) {} }); }",
            "  m3() { c({k:1}).then((r) => { if (r) {} }); }",
            "  m4() { fetch('/api'); }",
            "  m5() { if (1) { if (2) { switch(3){case 1: break;}}} }",
            "}",
        ]
    )
    out = analyze_js("big.js", src)
    assert out.classification is LWCClassification.BUSINESS_LOGIC_HEAVY
    assert len(out.apex_imports) == 3


def test_lwc_child_components_from_templates() -> None:
    from offramp.extract.lwc.bundle import _HTML_CHILD_RE, _kebab_to_camel

    html = """<template>
      <c-error-panel errors={errors}></c-error-panel>
      <c-reservation-tile record={r} onclick={pick}/>
      <lightning-card title="x"><c-pill-list></c-pill-list></lightning-card>
    </template>"""
    found = sorted({_kebab_to_camel(m) for m in _HTML_CHILD_RE.findall(html)})
    assert found == ["errorPanel", "pillList", "reservationTile"]


def test_lwc_message_channels_are_references() -> None:
    from offramp.extract.lwc.bundle import _MESSAGE_CHANNEL_RE

    js = "import TILE from '@salesforce/messageChannel/Tile_Selection__c';\nimport FLOW from \"@salesforce/messageChannel/Flow_Status_Change__c\";"
    assert sorted(set(_MESSAGE_CHANNEL_RE.findall(js))) == [
        "Flow_Status_Change__c",
        "Tile_Selection__c",
    ]


def test_aura_bundle_references() -> None:
    from pathlib import Path

    from offramp.extract.categories.base import get_extractor
    from offramp.extract.pull.reconciler import ReconciledRecord

    root = Path(__file__).parents[1] / "integration/fixtures/sample_org/aura/leadCardAura"
    files = {p.name: p.read_text() for p in root.iterdir()}
    rec = ReconciledRecord(
        category=CategoryName.AURA_BUNDLE,
        api_name="leadCardAura",
        namespace=None,
        payload={"path": "aura/leadCardAura", "files": files},
    )
    out = get_extractor(CategoryName.AURA_BUNDLE).parse_payload(rec)
    refs = out["references"]
    assert refs["apex_classes"] == ["LeadController"]
    assert refs["apex_methods"] == ["LeadController.getLeadScore"]
    assert refs["lwc_bundles"] == ["leadCard", "leadScoredEvent"]
    assert refs["flows"] == ["CaptureLeadDetails"]
    assert refs["message_channels"] == ["Lead_Selection__c"]
    assert refs["objects"] == ["Lead"]
    assert refs["fields"] == ["Lead.Company", "Lead.Routed__c", "Lead.Score__c"]
    assert refs["custom_labels"] == ["Capture_Lead"]
    assert out["kind"] == "component" and out["classification"] == "mixed"
