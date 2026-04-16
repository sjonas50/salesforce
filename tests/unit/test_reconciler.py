"""Reconciler precedence and disagreement-logging contract."""

from __future__ import annotations

from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord
from offramp.extract.pull.reconciler import reconcile


def _r(source: str, payload: dict[str, object]) -> RawMetadataRecord:
    return RawMetadataRecord(
        source=source,
        source_version="0",
        api_version="66.0",
        category=CategoryName.VALIDATION_RULE,
        api_name="Industry_Required",
        payload=payload,
    )


def test_single_source_passes_through() -> None:
    res = reconcile([_r("sf_cli", {"errorMessage": "X"})])
    assert len(res.records) == 1
    assert res.records[0].contributing_sources == ["sf_cli"]
    assert res.records[0].payload == {"errorMessage": "X"}
    assert res.disagreements == []


def test_salto_wins_over_sf_cli_on_conflict() -> None:
    res = reconcile(
        [
            _r("sf_cli", {"errorMessage": "From sf_cli"}),
            _r("salto", {"errorMessage": "From salto"}),
        ]
    )
    assert res.records[0].payload["errorMessage"] == "From salto"
    assert len(res.disagreements) == 1
    dis = res.disagreements[0]
    assert dis.field_path == "errorMessage"
    assert dis.values_by_source == {"sf_cli": "From sf_cli", "salto": "From salto"}


def test_non_conflicting_fields_merge() -> None:
    res = reconcile(
        [
            _r("sf_cli", {"errorMessage": "X", "active": "true"}),
            _r("salto", {"errorMessage": "X", "description": "D"}),
        ]
    )
    assert res.records[0].payload == {
        "errorMessage": "X",
        "active": "true",
        "description": "D",
    }
    assert res.disagreements == []
