"""End-to-end extract orchestration.

Drives the pull layer → reconciler → per-category extractors → audits →
output writing. The orchestrator is async because real pull clients are
network-bound; the fixture client is synchronous internally but presents the
same interface.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component, DataProfile, Provenance, SchemaSnapshot
from offramp.engram.client import EngramClient
from offramp.extract import categories as _categories  # noqa: F401 — register extractors
from offramp.extract.audit import CoverageReport, build_report
from offramp.extract.categories.base import (
    ExtractionFailure,
    get_extractor,
)
from offramp.extract.dispatch.class_resolver import DispatchEdge
from offramp.extract.dispatch.class_resolver import resolve as resolve_dispatch
from offramp.extract.dispatch.cmt_reader import CMTRecord, read_cmt_records_from_fixture
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
from offramp.extract.pull.source_tree import SourceTree
from offramp.extract.schema import from_source_tree
from offramp.understand.dependencies import DependencyGraph, build_graph

log = get_logger(__name__)


@dataclass
class ToolingSupplement:
    """Data that does not come from the metadata files themselves.

    Filled from ``_tooling/*.json`` dumps on the fixture / SFDX path and from
    :class:`offramp.extract.pull.tooling_api.ToolingApiPullClient` on the
    REST path. Every field is optional; missing data narrows the graph, it
    never breaks the run.
    """

    cmt_records: list[CMTRecord] = field(default_factory=list)
    dependency_rows: list[dict[str, Any]] = field(default_factory=list)
    cron_rows: list[dict[str, Any]] = field(default_factory=list)
    schema: SchemaSnapshot | None = None
    data_profile: DataProfile | None = None

    @classmethod
    def from_source_tree(cls, tree: SourceTree, *, org_alias: str) -> ToolingSupplement:
        from offramp.extract.data_profile import profile_from_dump

        deps = tree.tooling_json("dependencies") or []
        cron = tree.tooling_json("cron_triggers") or []
        dump = tree.tooling_json("data_profile")
        return cls(
            cmt_records=read_cmt_records_from_fixture(tree.root),
            dependency_rows=[r for r in deps if isinstance(r, dict)],
            cron_rows=[r for r in cron if isinstance(r, dict)],
            schema=from_source_tree(tree, org_alias=org_alias),
            data_profile=profile_from_dump(dump, org_alias=org_alias) if dump else None,
        )


class ExtractOrchestrator:
    """Run the extract pipeline end-to-end against one ``PullClient``."""

    def __init__(
        self,
        *,
        org_alias: str,
        client: PullClient,
        engram: EngramClient,
        fixture_root: Path | None = None,
        supplement: ToolingSupplement | None = None,
    ) -> None:
        self.org_alias = org_alias
        self.client = client
        self.engram = engram
        # ``fixture_root`` (or any SFDX-shaped directory) supplies the tooling
        # dumps + schema when no explicit supplement is given.
        self.fixture_root = fixture_root
        if supplement is None and fixture_root is not None:
            supplement = ToolingSupplement.from_source_tree(
                SourceTree(fixture_root), org_alias=org_alias
            )
        self.supplement = supplement or ToolingSupplement()

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

        # Dispatch resolution + framework detection (CMT rows may be empty).
        apex_class_names = {
            c.api_name
            for c in components
            if c.category is CategoryName.APEX_CLASS and c.api_name is not None
        }
        cmt_records = self.supplement.cmt_records
        cmt_types_present = {r.cmt_type for r in cmt_records}
        framework_signals: list[FrameworkSignal] = detect_frameworks(
            apex_class_names, cmt_types_present
        )
        dispatch_edges: list[DispatchEdge] = (
            resolve_dispatch(cmt_records, apex_class_names) if cmt_records else []
        )

        ooe_report = audit_ooe(components, self.org_alias)
        coverage = build_report(
            org_alias=self.org_alias,
            attempted=dict(attempted),
            components=components,
            failures=failures,
            disagreements=recon.disagreements,
            unresolved_references=[],
            suspected_gaps=[
                *_detect_suspected_gaps(attempted),
                *(f"pull failure: {f}" for f in getattr(self.client, "failures", [])),
            ],
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
            schema=self.supplement.schema,
            dependency_rows=list(self.supplement.dependency_rows),
            cron_rows=list(self.supplement.cron_rows),
            data_profile=self.supplement.data_profile,
        )


def _detect_suspected_gaps(attempted: dict[CategoryName, int]) -> list[str]:
    """Flag categories with zero records as a possible scope or fixture gap."""
    return [f"no records found for {cat.value}" for cat, n in attempted.items() if n == 0]


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
    schema: SchemaSnapshot | None = None
    dependency_rows: list[dict[str, Any]] = field(default_factory=list)
    cron_rows: list[dict[str, Any]] = field(default_factory=list)
    data_profile: DataProfile | None = None
    _graph: DependencyGraph | None = field(default=None, repr=False)

    def build_graph(self) -> DependencyGraph:
        """The C22 graph for this run (built once, cached)."""
        if self._graph is None:
            self._graph = build_graph(
                org_alias=self.org_alias,
                components=self.components,
                schema=self.schema,
                dispatch_edges=self.dispatch_edges,
                api_rows=self.dependency_rows,
                cron_rows=self.cron_rows,
                data_profile=self.data_profile,
            )
        return self._graph

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
        if self.schema is not None:
            (out_dir / "schema.json").write_text(
                self.schema.model_dump_json(indent=2), encoding="utf-8"
            )
        if self.data_profile is not None:
            (out_dir / "data_profile.json").write_text(
                self.data_profile.model_dump_json(indent=2), encoding="utf-8"
            )
        graph = self.build_graph()
        (out_dir / "graph.json").write_text(
            json.dumps(graph.to_jsonable(), indent=2, sort_keys=True), encoding="utf-8"
        )
        from offramp.understand.impact import (
            summarize,  # local import keeps extract importable alone
        )

        (out_dir / "impact_summary.json").write_text(
            json.dumps(summarize(graph, self.schema, self.components), indent=2, sort_keys=True),
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
