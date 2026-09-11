"""Review sampling, scoring and calibration of annotation confidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from offramp.understand.calibration import (
    ReviewItem,
    apply,
    band_of,
    read_sheet,
    sample,
    score,
    sheet_markdown,
    write_sheet,
)


def _ann(i: int, conf: float, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "component_id": f"c{i}",
        "name": f"Comp{i}",
        "category": kw.pop("category", "apex_class"),
        "confidence": conf,
        "recommended_tier": kw.pop("tier", "tier1_rules"),
        "tier_hint": "tier1_rules",
        "summary": f"summary {i}",
        "narrative": "how it works",
        "evidence": ["fact"],
        "unknowns": [],
        "source_available": True,
        "deterministic": False,
        "needs_review": False,
    }
    row.update(kw)
    return row


def test_sample_is_stratified_and_prefers_flagged_cases() -> None:
    rows = [_ann(i, 0.1 + 0.045 * i) for i in range(20)]
    rows.append(_ann(90, 0.6, needs_review=True))
    rows.append(_ann(91, 0.4, source_available=False))
    rows.append(_ann(92, 1.0, deterministic=True, category="sharing_rule"))
    items = sample(rows, size=10)
    assert len(items) == 10
    names = {it.name for it in items}
    assert {"Comp90", "Comp91", "Comp92"} <= names
    bands = {band_of(it.confidence) for it in items if not it.deterministic}
    assert len(bands) >= 5  # every populated band is represented
    assert all(it.summary_verdict == "" for it in items)


def test_score_maps_bands_to_observed_accuracy_and_flags_outliers() -> None:
    items = [
        ReviewItem("a", "A", "apex_class", 0.85, "tier1_rules", "tier1_rules", "s", "", [], [], []),
        ReviewItem("b", "B", "apex_class", 0.82, "tier1_rules", "tier1_rules", "s", "", [], [], []),
        ReviewItem("c", "C", "lwc_bundle", 0.45, "tier1_rules", "tier1_rules", "s", "", [], [], []),
        ReviewItem(
            "d", "D", "lwc_bundle", 0.95, "tier2_temporal", "tier1_rules", "s", "", [], [], []
        ),
        ReviewItem("e", "E", "flow", 0.7, "tier1_rules", "tier1_rules", "s", "", [], [], []),
    ]
    items[0].summary_verdict, items[0].tier_verdict = "yes", "yes"
    items[1].summary_verdict, items[1].tier_verdict = "no", "yes"  # overconfident
    items[2].summary_verdict, items[2].tier_verdict = "yes", "yes"  # underconfident
    items[3].summary_verdict, items[3].tier_verdict = "partly", "no"
    items[3].correct_tier = "tier1_rules"
    # item e left unanswered: ignored
    cal = score(items)
    assert cal.reviewed == 4
    assert cal.bands["0.8-0.9"]["n"] == 2 and cal.bands["0.8-0.9"]["accuracy"] == 0.5
    assert cal.bands["0.0-0.5"]["accuracy"] == 1.0
    assert cal.bands["0.9-1.0"]["tier_accuracy"] == 0.0
    assert [c["name"] for c in cal.overconfident] == ["B"]
    assert [c["name"] for c in cal.underconfident] == ["C"]
    # mapping blends observed accuracy with mean confidence by sample size: n/(n+2)
    assert cal.mapping["0.8-0.9"] == round(0.5 * 0.5 + 0.5 * 0.835, 2)
    assert cal.mapping["0.0-0.5"] == round((1 / 3) * 1.0 + (2 / 3) * 0.45, 2)
    assert "0.7-0.8" not in cal.mapping  # no reviews there
    assert cal.brier is not None and 0 < cal.brier < 1


def test_apply_adds_calibrated_confidence_and_keeps_deterministic() -> None:
    items = [
        ReviewItem("a", "A", "apex_class", 0.85, "tier1_rules", "tier1_rules", "s", "", [], [], [])
    ]
    items[0].summary_verdict = "no"
    cal = score(items)
    rows = apply([_ann(1, 0.88), _ann(2, 0.72), _ann(3, 1.0, deterministic=True)], cal)
    assert rows[0]["calibrated_confidence"] == cal.mapping["0.8-0.9"] < 0.88
    assert rows[1]["calibrated_confidence"] == 0.72  # band without reviews keeps the rule value
    assert rows[2]["calibrated_confidence"] == 1.0
    # accepted hidden-source items raise their band, but hidden source keeps the 0.5 ceiling
    hidden = ReviewItem(
        "h", "H", "apex_class", 0.4, "tier1_rules", "tier1_rules", "s", "", [], [], []
    )
    hidden.summary_verdict = "yes"
    cal2 = score([hidden] * 5)
    assert cal2.mapping["0.0-0.5"] > 0.5
    assert apply([_ann(4, 0.4, source_available=False)], cal2)[0]["calibrated_confidence"] == 0.5
    assert apply([_ann(5, 0.4)], cal2)[0]["calibrated_confidence"] == cal2.mapping["0.0-0.5"]


def test_sheet_roundtrip_and_markdown(tmp_path: Path) -> None:
    items = sample([_ann(i, 0.5 + i * 0.05) for i in range(8)], size=4)
    path = tmp_path / "sheet.json"
    write_sheet(items, path)
    back = read_sheet(path)
    assert [b.name for b in back] == [i.name for i in items]
    md = sheet_markdown(items)
    assert md.startswith("# Annotation review sheet") and "summary_verdict: yes | partly | no" in md
