"""Resolve string-valued CMT fields against an Apex class corpus.

Confidence scores follow the architecture spec:

* exact match  → 1.0
* case-insensitive match → 0.9
* common-prefix partial match → 0.6

Anything below 0.6 is dropped — the orphan resolver (Phase 2) will pick it up
via a different channel.
"""

from __future__ import annotations

from dataclasses import dataclass

from offramp.extract.dispatch.cmt_reader import CMTRecord


@dataclass(frozen=True)
class DispatchEdge:
    """One resolved dispatcher → handler edge."""

    dispatcher_cmt: str  # CMT row developer_name
    handler_class: str
    field_name: str  # which CMT field surfaced the reference
    confidence: float


def resolve(
    records: list[CMTRecord],
    apex_class_names: set[str],
) -> list[DispatchEdge]:
    """Scan CMT field values for matches against the known Apex class corpus.

    Each record contributes at most one edge per scanned field — the highest-
    confidence interpretation wins. Apex class names are matched
    case-insensitively because Salesforce-side admin tooling often shifts
    case during edits.
    """
    apex_lower = {name.lower(): name for name in apex_class_names}
    apex_prefixed = {name.split("_")[0].lower(): name for name in apex_class_names}

    edges: list[DispatchEdge] = []
    for rec in records:
        for field_name, value in rec.fields.items():
            if not value:
                continue
            best: tuple[float, str] | None = None
            if value in apex_class_names:
                best = (1.0, value)
            elif value.lower() in apex_lower:
                best = (0.9, apex_lower[value.lower()])
            else:
                head = value.lower().split("_", 1)[0]
                if head in apex_prefixed:
                    best = (0.6, apex_prefixed[head])
            if best is None:
                continue
            edges.append(
                DispatchEdge(
                    dispatcher_cmt=rec.developer_name,
                    handler_class=best[1],
                    field_name=field_name,
                    confidence=best[0],
                )
            )
    return edges
