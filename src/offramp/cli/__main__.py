"""``offramp`` CLI entry point.

X-Ray (build plan v0.2): ``extract``, ``xray``, ``impact``, ``kg``.
Year-two (kept, not extended): ``generate``, ``shadow``, ``cutover``.
"""

from __future__ import annotations

import argparse
import sys

from offramp import __version__
from offramp.cli.changes import add_changes_subparser
from offramp.cli.compare import add_compare_subparser
from offramp.cli.cutover import add_cutover_subparser
from offramp.cli.extract import add_extract_subparser
from offramp.cli.generate import add_generate_subparser
from offramp.cli.impact import add_impact_subparser
from offramp.cli.kg import add_kg_subparser
from offramp.cli.shadow import add_shadow_subparser
from offramp.cli.verify import add_verify_subparser
from offramp.cli.xray import add_xray_subparser
from offramp.core.logging import get_logger

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offramp", description="Salesforce Off-Ramp CLI")
    parser.add_argument("--version", action="version", version=f"offramp {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="Print platform info and exit.")
    add_extract_subparser(sub)
    add_xray_subparser(sub)
    add_impact_subparser(sub)
    add_kg_subparser(sub)
    add_compare_subparser(sub)
    add_changes_subparser(sub)
    add_verify_subparser(sub)
    add_generate_subparser(sub)
    add_shadow_subparser(sub)
    add_cutover_subparser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "info":
        log.info("offramp.info", version=__version__, status="xray-v0.2")
        return 0
    if hasattr(args, "func"):
        rc = args.func(args)
        return int(rc) if isinstance(rc, int) else 0
    log.warning("offramp.unknown_command", command=args.command)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
