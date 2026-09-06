"""``offramp compare``: diff two extract outputs (repo vs org, org vs org, before vs after)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from offramp.core.logging import get_logger
from offramp.understand.compare import compare_extracts

log = get_logger(__name__)


def add_compare_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "compare",
        help="Diff two extract output directories: components, parser edges, process definitions.",
    )
    p.add_argument(
        "--a", type=Path, required=True, help="First extract dir (e.g. a source-tree scan)."
    )
    p.add_argument("--b", type=Path, required=True, help="Second extract dir (e.g. the org scan).")
    p.add_argument(
        "--a-is-subset",
        action="store_true",
        help="A holds part of B (a repo vs the whole org): components only in B are not differences.",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p.add_argument(
        "--fail-on-diff", action="store_true", help="Exit 1 when the views differ (CI gate)."
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    for d in (args.a, args.b):
        if not (d / "graph.json").is_file():
            log.error("cli.compare.not_an_extract", path=str(d))
            return 2
    rep = compare_extracts(args.a, args.b, a_is_subset=args.a_is_subset)
    print(json.dumps(rep.to_jsonable(), indent=2) if args.json else rep.to_text())
    return 1 if args.fail_on_diff and not rep.clean else 0
