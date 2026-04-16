"""``offramp`` CLI entry point (Phase 0 skeleton).

Subcommands land per phase:

* Phase 1: ``offramp extract``
* Phase 2: ``offramp xray``
* Phase 3: ``offramp generate``, ``offramp deploy``
* Phase 4: ``offramp shadow``
* Phase 5: ``offramp cutover``
"""

from __future__ import annotations

import argparse
import sys

from offramp import __version__
from offramp.cli.extract import add_extract_subparser
from offramp.cli.generate import add_generate_subparser
from offramp.cli.shadow import add_shadow_subparser
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
    add_generate_subparser(sub)
    add_shadow_subparser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "info":
        log.info("offramp.info", version=__version__, status="phase-1-extract-engine")
        return 0
    if hasattr(args, "func"):
        rc = args.func(args)
        return int(rc) if isinstance(rc, int) else 0
    log.warning("offramp.unknown_command", command=args.command)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
