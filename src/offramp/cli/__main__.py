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
from offramp.core.logging import get_logger

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offramp", description="Salesforce Off-Ramp CLI")
    parser.add_argument("--version", action="version", version=f"offramp {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="Print platform info and exit.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "info":
        log.info("offramp.info", version=__version__, status="phase-0-scaffold")
        return 0
    log.warning("offramp.unknown_command", command=args.command)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
