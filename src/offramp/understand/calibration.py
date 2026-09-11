"""Calibrating annotation confidence against human review.

``earned confidence`` (:mod:`annotate`) is a rule-based number. This module
closes the loop with people:

1. :func:`sample` picks a stratified review set — every confidence band, the
   flagged and hidden-source cases, a couple of deterministic ones — so the
   verdicts cover the range rather than the easy middle.
2. A reviewer answers, per item: was the summary right (``yes`` / ``partly`` /
   ``no``), was the tier right (``yes`` / ``no`` + the correct tier), notes.
3. :func:`score` turns verdicts into a calibration table: observed accuracy per
   confidence band, plus per-category and per-tier agreement, Brier score, and
   the cases where the rules were wrong (high confidence, rejected) or too
   harsh (low confidence, accepted).
4. :func:`apply` maps each annotation's confidence through the table
   (``calibrated_confidence``), so the number reported is the accuracy people
   measured at that band, not the one the rules assumed.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 0.5),
    (0.5, 0.6),
    (0.6, 0.7),
    (0.7, 0.8),
    (0.8, 0.9),
    (0.9, 1.01),
)
SUMMARY_VERDICTS = {"yes": 1.0, "partly": 0.5, "no": 0.0}


def band_of(conf: float) -> str:
    for lo, hi in BANDS:
        if lo <= conf < hi:
            return f"{lo:.1f}-{min(hi, 1.0):.1f}"
    return "0.9-1.0"


@dataclass
class ReviewItem:
    """One annotation prepared for a reviewer, with empty verdict fields."""

    component_id: str
    name: str
    category: str
    confidence: float
    recommended_tier: str
    tier_hint: str
    summary: str
    narrative: str
    evidence: list[str]
    unknowns: list[str]
    reasons: list[str]  # why it was sampled
    deterministic: bool = False
    source_available: bool = True
    needs_review: bool = False
    # reviewer fills these
    summary_verdict: str = ""  # yes | partly | no
    tier_verdict: str = ""  # yes | no
    correct_tier: str = ""  # when tier_verdict == no
    notes: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return self.__dict__.copy()


def sample(
    annotations: list[dict[str, Any]], *, size: int = 20, seed: int = 42
) -> list[ReviewItem]:
    """Stratified pick: flagged + hidden first, then every band, then fill by category spread."""
    rng = random.Random(seed)
    rows = [a for a in annotations if a.get("summary")]
    chosen: dict[str, ReviewItem] = {}

    def add(a: dict[str, Any], reason: str) -> None:
        cid = str(a["component_id"])
        if cid in chosen:
            chosen[cid].reasons.append(reason)
            return
        chosen[cid] = ReviewItem(
            component_id=cid,
            name=str(a.get("name") or cid),
            category=str(a.get("category") or ""),
            confidence=float(a.get("confidence", 0.0)),
            recommended_tier=str(a.get("recommended_tier", "")),
            tier_hint=str(a.get("tier_hint", "")),
            summary=str(a.get("summary", "")),
            narrative=str(a.get("narrative", "")),
            evidence=list(a.get("evidence") or []),
            unknowns=list(a.get("unknowns") or []),
            reasons=[reason],
            deterministic=bool(a.get("deterministic")),
            source_available=bool(a.get("source_available", True)),
            needs_review=bool(a.get("needs_review")),
        )

    for a in rows:
        if a.get("needs_review") and len(chosen) < size:
            add(a, "tier contradicted the static hint")
    hidden = [a for a in rows if not a.get("source_available", True)]
    rng.shuffle(hidden)
    for a in hidden[:2]:
        add(a, "source hidden (managed package)")
    det = [a for a in rows if a.get("deterministic")]
    rng.shuffle(det)
    for a in det[:2]:
        add(a, "deterministic (rule-answered) annotation")
    model_rows = [a for a in rows if not a.get("deterministic")]
    by_band: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in model_rows:
        by_band[band_of(float(a.get("confidence", 0.0)))].append(a)
    # one per band first, then round-robin over bands, spreading categories
    order = [f"{lo:.1f}-{min(hi, 1.0):.1f}" for lo, hi in BANDS]
    for b in order:
        pool = by_band.get(b, [])
        rng.shuffle(pool)
        if pool and len(chosen) < size:
            add(pool[0], f"confidence band {b}")
    seen_cats: dict[str, int] = defaultdict(int)
    for it in chosen.values():
        seen_cats[it.category] += 1
    while len(chosen) < size:
        progressed = False
        for b in order:
            pool = [a for a in by_band.get(b, []) if str(a["component_id"]) not in chosen]
            if not pool:
                continue
            pool.sort(key=lambda a: (seen_cats[str(a.get("category") or "")], rng.random()))
            a = pool[0]
            add(a, f"confidence band {b}")
            seen_cats[str(a.get("category") or "")] += 1
            progressed = True
            if len(chosen) >= size:
                break
        if not progressed:
            break
    return list(chosen.values())[:size]


@dataclass
class Calibration:
    """What review measured, and how to map rule confidence to measured accuracy."""

    reviewed: int
    bands: dict[str, dict[str, Any]] = field(default_factory=dict)  # band -> n, accuracy, tier_acc
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_tier: dict[str, dict[str, Any]] = field(default_factory=dict)
    brier: float | None = None
    overconfident: list[dict[str, Any]] = field(default_factory=list)  # conf >= 0.8, rejected
    underconfident: list[dict[str, Any]] = field(default_factory=list)  # conf < 0.6, accepted
    mapping: dict[str, float] = field(default_factory=dict)  # band -> calibrated confidence
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def to_jsonable(self) -> dict[str, Any]:
        return self.__dict__.copy()


def score(items: list[ReviewItem]) -> Calibration:
    """Calibration from the reviewed items (unanswered ones are ignored)."""
    done = [it for it in items if it.summary_verdict in SUMMARY_VERDICTS]
    cal = Calibration(reviewed=len(done))
    if not done:
        return cal
    bands: dict[str, list[ReviewItem]] = defaultdict(list)
    cats: dict[str, list[ReviewItem]] = defaultdict(list)
    tiers: dict[str, list[ReviewItem]] = defaultdict(list)
    brier_sum = 0.0
    for it in done:
        bands[band_of(it.confidence)].append(it)
        cats[it.category].append(it)
        tiers[it.recommended_tier].append(it)
        outcome = SUMMARY_VERDICTS[it.summary_verdict]
        brier_sum += (it.confidence - outcome) ** 2
        if it.confidence >= 0.8 and it.summary_verdict == "no":
            cal.overconfident.append(_case(it))
        if it.confidence < 0.6 and it.summary_verdict == "yes":
            cal.underconfident.append(_case(it))
    cal.brier = round(brier_sum / len(done), 3)

    def stats(group: list[ReviewItem]) -> dict[str, Any]:
        acc = sum(SUMMARY_VERDICTS[i.summary_verdict] for i in group) / len(group)
        tier_answers = [i for i in group if i.tier_verdict in {"yes", "no"}]
        tier_acc = (
            sum(1 for i in tier_answers if i.tier_verdict == "yes") / len(tier_answers)
            if tier_answers
            else None
        )
        return {
            "n": len(group),
            "accuracy": round(acc, 2),
            "tier_accuracy": None if tier_acc is None else round(tier_acc, 2),
            "mean_confidence": round(sum(i.confidence for i in group) / len(group), 2),
        }

    cal.bands = {b: stats(g) for b, g in sorted(bands.items())}
    cal.by_category = {c: stats(g) for c, g in sorted(cats.items())}
    cal.by_tier = {t: stats(g) for t, g in sorted(tiers.items())}
    # Mapping: a band with reviews maps to its observed accuracy blended with its mean
    # confidence by sample size (n/(n+2)), so two reviews do not swing a band to 0 or 1;
    # bands without reviews keep the rule confidence.
    for lo, hi in BANDS:
        key = f"{lo:.1f}-{min(hi, 1.0):.1f}"
        st = cal.bands.get(key)
        if st is None:
            continue
        w = st["n"] / (st["n"] + 2)
        cal.mapping[key] = round(w * st["accuracy"] + (1 - w) * st["mean_confidence"], 2)
    return cal


def _case(it: ReviewItem) -> dict[str, Any]:
    return {
        "name": it.name,
        "category": it.category,
        "confidence": it.confidence,
        "verdict": it.summary_verdict,
        "notes": it.notes,
    }


HIDDEN_SOURCE_CEILING = 0.5


def apply(annotations: list[dict[str, Any]], cal: Calibration) -> list[dict[str, Any]]:
    """Add ``calibrated_confidence`` to every annotation row.

    Deterministic rows keep their value. Rows whose source Salesforce hides keep the
    0.5 ceiling even when reviewers accept them: a reviewer confirms that an honest
    "cannot be inspected, likely X" is *accurate*, not that the component is *known*,
    and confidence measures the second thing.
    """
    out = []
    for a in annotations:
        row = dict(a)
        conf = float(a.get("confidence", 0.0))
        if a.get("deterministic"):
            row["calibrated_confidence"] = conf
        else:
            mapped = cal.mapping.get(band_of(conf), conf)
            if not a.get("source_available", True):
                mapped = min(mapped, HIDDEN_SOURCE_CEILING)
            row["calibrated_confidence"] = mapped
        out.append(row)
    return out


# ---- files ---------------------------------------------------------------------


def write_sheet(items: list[ReviewItem], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "_how": (
            "For each item set summary_verdict to yes | partly | no, tier_verdict to yes | no "
            "(and correct_tier when no), add notes if useful. Then: "
            "offramp review score --sheet <this file> --annotations <annotations.json>"
        ),
        "items": [it.to_jsonable() for it in items],
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def read_sheet(path: Path) -> list[ReviewItem]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc["items"] if isinstance(doc, dict) else doc
    return [
        ReviewItem(**{k: v for k, v in r.items() if k in ReviewItem.__dataclass_fields__})
        for r in rows
    ]


def sheet_markdown(items: list[ReviewItem]) -> str:
    out = ["# Annotation review sheet", ""]
    for i, it in enumerate(items, 1):
        out.append(
            f"## {i}. {it.name}  `{it.category}`  confidence {it.confidence:.2f}  "
            f"tier {it.recommended_tier} (hint {it.tier_hint})"
        )
        out.append(f"_sampled because: {'; '.join(it.reasons)}_")
        out.append("")
        out.append(f"**Summary.** {it.summary}")
        if it.narrative:
            out.append("")
            out.append(f"**Narrative.** {it.narrative}")
        if it.evidence:
            out.append("")
            out.append("**Evidence.** " + " · ".join(it.evidence))
        if it.unknowns:
            out.append("")
            out.append("**Unknowns.** " + " · ".join(it.unknowns))
        out.append("")
        out.append("- summary_verdict: yes | partly | no")
        out.append(
            "- tier_verdict: yes | no  (correct_tier: tier1_rules | tier2_temporal | tier3_langgraph)"
        )
        out.append("")
    return "\n".join(out)
