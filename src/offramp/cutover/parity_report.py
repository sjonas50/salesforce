"""Behavioral Parity Report (architecture §11.6).

Catalogs every known behavioral difference between the deployed runtime
and the source Salesforce org, organized by category:

* deliberate_simplifications — chose not to replicate; document why
* discovered_undocumented   — SF behavior not in docs; how we handle it
* platform_imposed_deviations — governor limits, system context diffs
* customer_requested_improvements — explicit asks to diverge from SF

Inputs are configurable but the typical sources are:
* the v2.1 §10.4 divergence categories observed during shadow execution
* hand-curated entries the engagement team adds during pre-cutover review
* the OoE Surface Audit's exclude list (§7.7)

Output is HTML + JSON, both Engram-anchored. Required deliverable for every
Agent Factory engagement before cutover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from offramp.core.logging import get_logger
from offramp.engram.client import EngramClient

log = get_logger(__name__)

# parity_report.py is at src/offramp/cutover/parity_report.py — parents:
# cutover (0), offramp (1), src (2), repo root (3). Templates live at
# <repo_root>/templates.
_TEMPLATES = Path(__file__).resolve().parents[3] / "templates"


class ParityCategory(StrEnum):
    DELIBERATE_SIMPLIFICATION = "deliberate_simplification"
    DISCOVERED_UNDOCUMENTED = "discovered_undocumented"
    PLATFORM_IMPOSED_DEVIATION = "platform_imposed_deviation"
    CUSTOMER_REQUESTED_IMPROVEMENT = "customer_requested_improvement"


@dataclass
class ParityFinding:
    """One catalogued behavioral difference."""

    finding_id: str
    category: ParityCategory
    salesforce_behavior: str
    runtime_behavior: str
    rationale: str
    customer_disposition: str = "pending"  # 'accepted' | 'rejected' | 'pending'
    references: list[str] = field(default_factory=list)
    severity: str = "info"  # 'info' | 'minor' | 'major' | 'blocking'
    engram_anchor: str | None = None


@dataclass
class ParityReport:
    """Aggregate report — JSON + HTML are both rendered from this."""

    process_id: str
    org_alias: str
    findings: list[ParityFinding] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def by_category(self) -> dict[ParityCategory, list[ParityFinding]]:
        out: dict[ParityCategory, list[ParityFinding]] = {c: [] for c in ParityCategory}
        for f in self.findings:
            out[f.category].append(f)
        return out


async def anchor_findings(report: ParityReport, *, engram: EngramClient) -> ParityReport:
    """Engram-anchor every finding so the customer's disposition record is verifiable."""
    for f in report.findings:
        rec = await engram.anchor(
            "cutover.parity_report.finding",
            {
                "process_id": report.process_id,
                "finding_id": f.finding_id,
                "category": f.category.value,
                "disposition": f.customer_disposition,
                "severity": f.severity,
            },
        )
        f.engram_anchor = rec.anchor_id
    return report


def render_json(report: ParityReport) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
        "process_id": report.process_id,
        "org_alias": report.org_alias,
        "summary": {
            "total": len(report.findings),
            "by_category": {c.value: len(items) for c, items in report.by_category().items()},
            "by_disposition": _count_by(report.findings, lambda f: f.customer_disposition),
            "by_severity": _count_by(report.findings, lambda f: f.severity),
        },
        "findings": [_finding_to_jsonable(f) for f in report.findings],
    }


def render_html(report: ParityReport) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("parity_report.html.j2")
    return template.render(
        report=report,
        by_category=report.by_category(),
        generated_at=report.generated_at.isoformat(timespec="seconds"),
    )


def write(report: ParityReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "parity_report.json"
    html_path = out_dir / "parity_report.html"
    json_path.write_text(
        json.dumps(render_json(report), indent=2, sort_keys=True), encoding="utf-8"
    )
    html_path.write_text(render_html(report), encoding="utf-8")
    log.info(
        "cutover.parity_report.written",
        process=report.process_id,
        findings=len(report.findings),
        out_dir=str(out_dir),
    )
    return json_path, html_path


def _finding_to_jsonable(f: ParityFinding) -> dict[str, Any]:
    return {
        "finding_id": f.finding_id,
        "category": f.category.value,
        "salesforce_behavior": f.salesforce_behavior,
        "runtime_behavior": f.runtime_behavior,
        "rationale": f.rationale,
        "customer_disposition": f.customer_disposition,
        "severity": f.severity,
        "references": list(f.references),
        "engram_anchor": f.engram_anchor,
    }


def _count_by(items: list[ParityFinding], key) -> dict[str, int]:  # type: ignore[no-untyped-def]
    out: dict[str, int] = {}
    for it in items:
        k = key(it)
        out[k] = out.get(k, 0) + 1
    return out


# Convenience constructors for the typical findings the engagement team
# pre-populates before shadow execution refines them.


def excluded_ooe_step_finding(
    *, finding_id: str, step_number: int, step_name: str
) -> ParityFinding:
    """Surface Audit: an OoE step explicitly excluded from the runtime."""
    return ParityFinding(
        finding_id=finding_id,
        category=ParityCategory.DELIBERATE_SIMPLIFICATION,
        salesforce_behavior=(
            f"OoE step {step_number} ({step_name}) executes for matching transactions"
        ),
        runtime_behavior=(
            "Runtime explicitly refuses transactions exercising this step (StepNotInScopeError)"
        ),
        rationale=(
            "The OoE Surface Audit (§7.7) measured zero exercising components "
            "in this customer's org for this step. Excluding it converts an "
            "unbounded compatibility problem into a bounded one and surfaces "
            "any future drift loudly."
        ),
        severity="info",
        references=["docs/architecture.md §7.7", f"OoE step #{step_number}"],
    )


def governor_limit_finding(*, finding_id: str, limit: str) -> ParityFinding:
    return ParityFinding(
        finding_id=finding_id,
        category=ParityCategory.PLATFORM_IMPOSED_DEVIATION,
        salesforce_behavior=f"Salesforce enforces governor limit: {limit}",
        runtime_behavior=(
            "Runtime does not enforce this limit; transactions that would "
            "exceed it on Salesforce execute to completion externally"
        ),
        rationale="Removing governor limits is part of the value of migration.",
        severity="minor",
    )


def divergence_observation_finding(
    *,
    finding_id: str,
    category: str,
    sample_field_diff: dict[str, Any],
) -> ParityFinding:
    """Findings sourced from Phase 4 shadow divergence rows."""
    sf = ", ".join(f"{k}={v[0]}" for k, v in sample_field_diff.items())
    rt = ", ".join(f"{k}={v[1]}" for k, v in sample_field_diff.items())
    return ParityFinding(
        finding_id=finding_id,
        category=ParityCategory.DISCOVERED_UNDOCUMENTED,
        salesforce_behavior=f"Production produced: {sf}",
        runtime_behavior=f"Runtime produced: {rt}",
        rationale=f"Observed during shadow execution; categorized as {category}.",
        severity="major" if category in {"translation_error", "ooe_ordering_mismatch"} else "minor",
    )
