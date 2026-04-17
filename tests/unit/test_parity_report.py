"""Behavioral Parity Report rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from offramp.cutover.parity_report import (
    ParityCategory,
    ParityFinding,
    ParityReport,
    anchor_findings,
    excluded_ooe_step_finding,
    governor_limit_finding,
    render_html,
    render_json,
    write,
)
from offramp.engram.client import InMemoryEngramClient


def _basic_report() -> ParityReport:
    return ParityReport(
        process_id="lead_routing",
        org_alias="fisher_prod",
        findings=[
            excluded_ooe_step_finding(
                finding_id="bp-001", step_number=15, step_name="ENTITLEMENT_RULES"
            ),
            governor_limit_finding(finding_id="bp-002", limit="150 SOQL queries / txn"),
            ParityFinding(
                finding_id="bp-003",
                category=ParityCategory.CUSTOMER_REQUESTED_IMPROVEMENT,
                salesforce_behavior="Routes by zip code lookup",
                runtime_behavior="Routes by ML model recommendation",
                rationale="Customer asked us to upgrade routing logic during translation",
                customer_disposition="accepted",
                severity="info",
            ),
        ],
    )


def test_render_json_groups_by_category_and_severity() -> None:
    payload = render_json(_basic_report())
    assert payload["schema_version"] == "1.0"
    assert payload["summary"]["total"] == 3
    bc = payload["summary"]["by_category"]
    assert bc[ParityCategory.DELIBERATE_SIMPLIFICATION.value] == 1
    assert bc[ParityCategory.PLATFORM_IMPOSED_DEVIATION.value] == 1
    assert bc[ParityCategory.CUSTOMER_REQUESTED_IMPROVEMENT.value] == 1


def test_render_html_includes_finding_text() -> None:
    html = render_html(_basic_report())
    assert "Behavioral Parity Report" in html
    assert "lead_routing" in html
    assert "ENTITLEMENT_RULES" in html
    assert "ML model recommendation" in html


def test_write_produces_two_files(tmp_path: Path) -> None:
    j, h = write(_basic_report(), tmp_path)
    assert j.exists() and h.exists()
    payload = json.loads(j.read_text())
    assert payload["org_alias"] == "fisher_prod"


@pytest.mark.asyncio
async def test_anchor_findings_assigns_engram_anchors() -> None:
    engram = InMemoryEngramClient()
    report = _basic_report()
    await anchor_findings(report, engram=engram)
    for f in report.findings:
        assert f.engram_anchor is not None
        assert f.engram_anchor.startswith("engram:")
