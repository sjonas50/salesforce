"""``offramp review``: sample annotations for human review, score verdicts, apply calibration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from offramp.core.logging import get_logger
from offramp.understand.calibration import (
    Calibration,
    apply,
    read_sheet,
    sample,
    score,
    sheet_markdown,
    write_sheet,
)

log = get_logger(__name__)


def add_review_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("review", help="Calibrate annotation confidence with a human review sample.")
    ops = p.add_subparsers(dest="op", required=True)

    s = ops.add_parser("sample", help="Pick a stratified review set and write the sheet.")
    s.add_argument("--annotations", type=Path, required=True, help="annotations.json to sample.")
    s.add_argument("--out", type=Path, required=True, help="review sheet (JSON) to write.")
    s.add_argument("--size", type=int, default=20)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--markdown", type=Path, help="Also write a readable Markdown copy.")
    s.set_defaults(func=_sample)

    sc = ops.add_parser("score", help="Score a filled-in sheet; write calibration.json.")
    sc.add_argument("--sheet", type=Path, required=True)
    sc.add_argument("--annotations", type=Path, help="Apply the calibration to this file in place.")
    sc.add_argument("--out", type=Path, help="calibration.json (default: next to the sheet).")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=_score)

    ap = ops.add_parser("apply", help="Apply an existing calibration.json to an annotations file.")
    ap.add_argument("--calibration", type=Path, required=True)
    ap.add_argument("--annotations", type=Path, required=True)
    ap.set_defaults(func=_apply)


def _sample(args: argparse.Namespace) -> int:
    rows = json.loads(args.annotations.read_text(encoding="utf-8"))
    items = sample(rows, size=args.size, seed=args.seed)
    write_sheet(items, args.out)
    if args.markdown:
        args.markdown.write_text(sheet_markdown(items), encoding="utf-8")
    print(f"review sheet: {len(items)} items -> {args.out}")
    for it in items:
        print(f"  {it.confidence:.2f} {it.category:<28} {it.name:<36} {'; '.join(it.reasons)}")
    return 0


def _print_calibration(cal: Calibration) -> None:
    print(f"reviewed: {cal.reviewed}   brier: {cal.brier}")
    print("band        n  accuracy  tier_acc  mean_conf  -> calibrated")
    for b, st in cal.bands.items():
        print(
            f"{b:<9} {st['n']:>4}  {st['accuracy']:>8.2f}  "
            f"{'-' if st['tier_accuracy'] is None else format(st['tier_accuracy'], '.2f'):>8}  "
            f"{st['mean_confidence']:>9.2f}  -> {cal.mapping.get(b, '-')}"
        )
    if cal.overconfident:
        print("overconfident (>= 0.8, rejected):")
        for c in cal.overconfident:
            print(f"  {c['confidence']:.2f} {c['name']} [{c['category']}] {c['notes']}")
    if cal.underconfident:
        print("underconfident (< 0.6, accepted):")
        for c in cal.underconfident:
            print(f"  {c['confidence']:.2f} {c['name']} [{c['category']}] {c['notes']}")


def _score(args: argparse.Namespace) -> int:
    items = read_sheet(args.sheet)
    cal = score(items)
    out = args.out or (args.sheet.parent / "calibration.json")
    out.write_text(json.dumps(cal.to_jsonable(), indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(cal.to_jsonable(), indent=2))
    else:
        _print_calibration(cal)
    print(f"calibration -> {out}")
    if args.annotations:
        rows = json.loads(args.annotations.read_text(encoding="utf-8"))
        args.annotations.write_text(json.dumps(apply(rows, cal), indent=2, sort_keys=True))
        print(f"applied to {len(rows)} annotations in {args.annotations}")
    return 0


def _apply(args: argparse.Namespace) -> int:
    data = json.loads(args.calibration.read_text(encoding="utf-8"))
    cal = Calibration(**{k: v for k, v in data.items() if k in Calibration.__dataclass_fields__})
    rows = json.loads(args.annotations.read_text(encoding="utf-8"))
    args.annotations.write_text(json.dumps(apply(rows, cal), indent=2, sort_keys=True))
    print(f"applied calibration ({cal.reviewed} reviews) to {len(rows)} annotations")
    return 0
