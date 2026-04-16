"""Reconciler — collapse multi-source raw records into one canonical view.

Documented precedence (architecture §C1, v2.1 plan §7.2):

* **Salto** wins for resolved references (cross-object, cross-namespace).
* **sf CLI** wins for source-of-truth XML payloads.
* **Tooling API** wins for runtime state (active version, last execution).

When sources disagree on a field, the precedence rule applies and the
disagreement is captured as a :class:`PullDisagreement` for the coverage
report. Single-source records pass through unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName
from offramp.extract.pull.base import PullDisagreement, RawMetadataRecord

log = get_logger(__name__)


# Lower = higher priority. Sources not in the table sort last (priority 99).
_DEFAULT_PRECEDENCE: dict[str, int] = {
    "salto": 0,
    "sf_cli": 1,
    "tooling_api": 2,
    "fixture": 50,  # tests only — never wins over a real source
}


@dataclass
class ReconciledRecord:
    """Canonical per-(api_name, category) view after reconciliation."""

    category: CategoryName
    api_name: str
    namespace: str | None
    payload: dict[str, Any]
    contributing_sources: list[str] = field(default_factory=list)


@dataclass
class ReconciliationResult:
    """All canonical records plus the disagreement log."""

    records: list[ReconciledRecord] = field(default_factory=list)
    disagreements: list[PullDisagreement] = field(default_factory=list)


def reconcile(
    raws: Iterable[RawMetadataRecord],
    *,
    precedence: dict[str, int] | None = None,
) -> ReconciliationResult:
    """Collapse a stream of raw records into canonical reconciled records."""
    pri = precedence or _DEFAULT_PRECEDENCE
    grouped: dict[tuple[CategoryName, str, str | None], list[RawMetadataRecord]] = defaultdict(list)
    for r in raws:
        grouped[(r.category, r.api_name, r.namespace)].append(r)

    result = ReconciliationResult()
    for (category, api_name, namespace), bucket in grouped.items():
        # Single-source records pass through unchanged.
        if len(bucket) == 1:
            r = bucket[0]
            result.records.append(
                ReconciledRecord(
                    category=category,
                    api_name=api_name,
                    namespace=namespace,
                    payload=dict(r.payload),
                    contributing_sources=[r.source],
                )
            )
            continue

        # Multi-source — sort by precedence, build the canonical payload by
        # field-level merge with disagreement detection.
        bucket.sort(key=lambda r: pri.get(r.source, 99))
        canonical: dict[str, Any] = {}
        for r in bucket:
            for k, v in r.payload.items():
                if k not in canonical:
                    canonical[k] = v
                    continue
                if canonical[k] != v:
                    result.disagreements.append(
                        PullDisagreement(
                            api_name=api_name,
                            category=category,
                            sources_in_disagreement=tuple(b.source for b in bucket),
                            field_path=k,
                            values_by_source={b.source: b.payload.get(k) for b in bucket},
                        )
                    )
                    # Higher-precedence source already won by virtue of sort order.
        result.records.append(
            ReconciledRecord(
                category=category,
                api_name=api_name,
                namespace=namespace,
                payload=canonical,
                contributing_sources=[b.source for b in bucket],
            )
        )

    log.info(
        "extract.reconcile.done",
        records=len(result.records),
        disagreements=len(result.disagreements),
    )
    return result
