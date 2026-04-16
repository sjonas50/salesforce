"""Extraction Coverage Audit (architecture §C9 / build-plan 1.9).

Per-category coverage: how many components attempted, how many succeeded,
how many failed and why. Includes per-component provenance and unresolved-
reference logging. The output is the foundation of the X-Ray report's
credibility section.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from offramp.core.models import CategoryName, Component
from offramp.extract.categories.base import ExtractionFailure
from offramp.extract.pull.base import PullDisagreement


@dataclass
class CategoryCoverage:
    """One row of the coverage report — coverage for one category."""

    category: CategoryName
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    failure_reasons: list[str] = field(default_factory=list)

    @property
    def coverage_ratio(self) -> float:
        if self.attempted == 0:
            return 1.0
        return self.succeeded / self.attempted


@dataclass
class CoverageReport:
    """Aggregate coverage report."""

    org_alias: str
    total_attempted: int = 0
    total_succeeded: int = 0
    by_category: dict[CategoryName, CategoryCoverage] = field(default_factory=dict)
    failures: list[ExtractionFailure] = field(default_factory=list)
    disagreements: list[PullDisagreement] = field(default_factory=list)
    unresolved_references: list[str] = field(default_factory=list)
    suspected_gaps: list[str] = field(default_factory=list)

    @property
    def overall_coverage(self) -> float:
        if self.total_attempted == 0:
            return 1.0
        return self.total_succeeded / self.total_attempted


def build_report(
    *,
    org_alias: str,
    attempted: dict[CategoryName, int],
    components: list[Component],
    failures: list[ExtractionFailure],
    disagreements: list[PullDisagreement],
    unresolved_references: list[str] | None = None,
    suspected_gaps: list[str] | None = None,
) -> CoverageReport:
    by_cat: dict[CategoryName, CategoryCoverage] = {
        cat: CategoryCoverage(category=cat) for cat in CategoryName
    }
    for cat, count in attempted.items():
        by_cat[cat].attempted = count
    for c in components:
        by_cat[c.category].succeeded += 1
    for f in failures:
        by_cat[f.category].failed += 1
        by_cat[f.category].failure_reasons.append(f"{f.api_name}: {f.reason}")

    return CoverageReport(
        org_alias=org_alias,
        total_attempted=sum(c.attempted for c in by_cat.values()),
        total_succeeded=sum(c.succeeded for c in by_cat.values()),
        by_category=by_cat,
        failures=failures,
        disagreements=disagreements,
        unresolved_references=list(unresolved_references or []),
        suspected_gaps=list(suspected_gaps or []),
    )
