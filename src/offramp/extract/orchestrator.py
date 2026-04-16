"""End-to-end extract orchestration.

Drives the pull layer → reconciler → per-category extractors → audits →
output writing. The orchestrator is async because real pull clients are
network-bound; the fixture client is synchronous internally but presents the
same interface.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component, Provenance
from offramp.engram.client import EngramClient
from offramp.extract import categories as _categories  # noqa: F401 — register extractors
from offramp.extract.audit import CoverageReport, build_report
from offramp.extract.categories.base import (
    ExtractionFailure,
    get_extractor,
)
from offramp.extract.dispatch.class_resolver import DispatchEdge
from offramp.extract.dispatch.class_resolver import resolve as resolve_dispatch
from offramp.extract.dispatch.cmt_reader import read_cmt_records_from_fixture
from offramp.extract.dispatch.framework_detectors import (
    FrameworkSignal,
)
from offramp.extract.dispatch.framework_detectors import (
    detect as detect_frameworks,
)
from offramp.extract.ooe_audit.audit import SurfaceAuditReport
from offramp.extract.ooe_audit.audit import audit as audit_ooe
from offramp.extract.pull.base import PullClient
from offramp.extract.pull.reconciler import reconcile

log = get_logger(__name__)


class ExtractOrchestrator:
    """Run the extract pipeline end-to-end against one ``PullClient``."""

    def __init__(
        self,
        *,
        org_alias: str,
        client: PullClient,
        engram: EngramClient,
        fixture_root: Path | None = None,
    ) -> None:
        self.org_alias = org_alias
        self.client = client
        self.engram = engram
        # ``fixture_root`` is only honored when the client is the FixturePullClient
        # — used to find the optional CMT records dump.
        self.fixture_root = fixture_root

    async def run(self) -> ExtractRunResult:
        log.info("extract.run.start", org=self.org_alias, source=self.client.source_name)
        raw_records = list(await self.client.pull())
        log.info("extract.pulled", count=len(raw_records))

        attempted: dict[CategoryName, int] = defaultdict(int)
        for r in raw_records:
            attempted[r.category] += 1

        recon = reconcile(raw_records)

        components: list[Component] = []
        failures: list[ExtractionFailure] = []
        for rec in recon.records:
            try:
                extractor = get_extractor(rec.category)
            except KeyError:
                failures.append(
                    ExtractionFailure(
                        api_name=rec.api_name,
                        category=rec.category,
                        reason=f"No extractor registered for {rec.category}",
                    )
                )
                continue

            provenance = Provenance(
                source_tool=rec.contributing_sources[0],
                source_version=self.client.source_version,
                api_version=self.client.api_version,
            )
            try:
                component = extractor.to_component(rec, self.org_alias, provenance)
            except (ValueError, KeyError) as exc:
                failures.append(
                    ExtractionFailure(
                        api_name=rec.api_name,
                        category=rec.category,
                        reason=str(exc),
                    )
                )
                continue

            await self.engram.anchor(
                component="extract.orchestrator",
                payload={
                    "component_id": str(component.id),
                    "category": component.category.value,
                    "api_name": component.api_name,
                    "content_hash": component.content_hash,
                },
            )
            components.append(component)

        # Dispatch resolution is opt-in — only runs when CMT records are present.
        dispatch_edges: list[DispatchEdge] = []
        framework_signals: list[FrameworkSignal] = []
        if self.fixture_root is not None:
            cmt_records = read_cmt_records_from_fixture(self.fixture_root)
            apex_class_names = {
                c.api_name
                for c in components
                if c.category is CategoryName.APEX_CLASS and c.api_name is not None
            }
            cmt_types_present = {r.cmt_type for r in cmt_records}
            framework_signals = detect_frameworks(apex_class_names, cmt_types_present)
            dispatch_edges = resolve_dispatch(cmt_records, apex_class_names)

        ooe_report = audit_ooe(components, self.org_alias)
        coverage = build_report(
            org_alias=self.org_alias,
            attempted=dict(attempted),
            components=components,
            failures=failures,
            disagreements=recon.disagreements,
            unresolved_references=[],
            suspected_gaps=_detect_suspected_gaps(attempted),
        )

        log.info(
            "extract.run.done",
            extracted=len(components),
            failed=len(failures),
            categories_with_data=sum(1 for v in attempted.values() if v),
        )
        return ExtractRunResult(
            org_alias=self.org_alias,
            components=components,
            failures=failures,
            coverage=coverage,
            ooe=ooe_report,
            dispatch_edges=dispatch_edges,
            framework_signals=framework_signals,
        )


def _detect_suspected_gaps(attempted: dict[CategoryName, int]) -> list[str]:
    """Flag categories with zero records as a possible scope or fixture gap."""
    return [f"no records found for {cat.value}" for cat, n in attempted.items() if n == 0]


from dataclasses import dataclass, field  # noqa: E402 — placed after imports for clarity


@dataclass
class ExtractRunResult:
    """Aggregate output of one extract run."""

    org_alias: str
    components: list[Component] = field(default_factory=list)
    failures: list[ExtractionFailure] = field(default_factory=list)
    coverage: CoverageReport | None = None
    ooe: SurfaceAuditReport | None = None
    dispatch_edges: list[DispatchEdge] = field(default_factory=list)
    framework_signals: list[FrameworkSignal] = field(default_factory=list)

    def write(self, out_dir: Path) -> None:
        """Persist a JSON dump of every artifact under ``out_dir``."""
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "components.json").write_text(
            json.dumps(
                [json.loads(c.model_dump_json()) for c in self.components],
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (out_dir / "failures.json").write_text(
            json.dumps([asdict(f) for f in self.failures], indent=2, default=str),
            encoding="utf-8",
        )
        if self.coverage is not None:
            (out_dir / "coverage.json").write_text(
                json.dumps(_coverage_to_jsonable(self.coverage), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        if self.ooe is not None:
            (out_dir / "ooe_surface_audit.json").write_text(
                json.dumps(_ooe_to_jsonable(self.ooe), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        (out_dir / "dispatch_edges.json").write_text(
            json.dumps([asdict(e) for e in self.dispatch_edges], indent=2),
            encoding="utf-8",
        )
        (out_dir / "framework_signals.json").write_text(
            json.dumps([asdict(s) for s in self.framework_signals], indent=2),
            encoding="utf-8",
        )


def _coverage_to_jsonable(c: CoverageReport) -> dict[str, Any]:
    return {
        "org_alias": c.org_alias,
        "overall_coverage": c.overall_coverage,
        "total_attempted": c.total_attempted,
        "total_succeeded": c.total_succeeded,
        "by_category": [
            {
                "category": cat.value,
                "attempted": cov.attempted,
                "succeeded": cov.succeeded,
                "failed": cov.failed,
                "coverage_ratio": cov.coverage_ratio,
                "failure_reasons": cov.failure_reasons,
            }
            for cat, cov in c.by_category.items()
        ],
        "failures": [asdict(f) for f in c.failures],
        "disagreements": [asdict(d) for d in c.disagreements],
        "unresolved_references": c.unresolved_references,
        "suspected_gaps": c.suspected_gaps,
    }


def _ooe_to_jsonable(r: SurfaceAuditReport) -> dict[str, Any]:
    return {
        "org_alias": r.org_alias,
        "total_components": r.total_components,
        "observations": [
            {
                "step": int(o.step),
                "step_name": o.step.name,
                "structural_count": o.structural_count,
                "observed_frequency": o.observed_frequency,
                "in_scope": o.in_scope,
                "priority": o.priority,
                "contributing_categories": [c.value for c in o.contributing_categories],
            }
            for o in r.observations
        ],
    }
