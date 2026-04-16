"""LWC classifier — UI-only / mixed / business-logic-heavy classification."""

from __future__ import annotations

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
