"""``offramp extract`` subcommand.

Phase 1: drives a single ``PullClient`` end-to-end against a fixture org
dump or (eventually) a real Salesforce org. Output is a directory of JSON
artifacts the X-Ray report (Phase 2) renders.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from offramp.core.logging import get_logger
from offramp.engram.client import open_client
from offramp.extract.orchestrator import ExtractOrchestrator
from offramp.extract.pull.fixture import FixturePullClient
from offramp.extract.pull.sf_cli import SfCliError, SfCliPullClient

log = get_logger(__name__)


def add_extract_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("extract", help="Run the extract pipeline.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--fixture",
        type=Path,
        help="Path to a fixture org dump (FixturePullClient).",
    )
    src.add_argument(
        "--org",
        help="Authenticated Salesforce org alias to retrieve from via the sf CLI.",
    )
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.add_argument("--org-alias", default=None, help="Override org alias label.")
    p.add_argument(
        "--sf-binary",
        default="sf",
        help="Path to the Salesforce CLI binary (default: sf on PATH).",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    if args.fixture is not None:
        return asyncio.run(_run_fixture(args))
    return asyncio.run(_run_org(args))


async def _run_fixture(args: argparse.Namespace) -> int:
    fixture: Path = args.fixture
    # Filesystem checks here are intentional CLI guards before async work begins;
    # converting to anyio.Path would add a dep just for one check.
    if not fixture.is_dir():  # noqa: ASYNC240
        log.error("extract.fixture_not_found", path=str(fixture))
        return 1
    org_alias = args.org_alias or fixture.name
    client = FixturePullClient(fixture)
    async with open_client() as engram:
        orch = ExtractOrchestrator(
            org_alias=org_alias, client=client, engram=engram, fixture_root=fixture
        )
        result = await orch.run()
    result.write(args.out)
    log.info(
        "extract.cli.done",
        out=str(args.out),
        components=len(result.components),
        failures=len(result.failures),
    )
    return 0


async def _run_org(args: argparse.Namespace) -> int:
    org_alias = args.org_alias or args.org
    client = SfCliPullClient(org_alias=args.org, sf_binary=args.sf_binary)
    try:
        async with open_client() as engram:
            orch = ExtractOrchestrator(org_alias=org_alias, client=client, engram=engram)
            result = await orch.run()
    except SfCliError as exc:
        log.error("extract.sf_cli.failed", org=args.org, error=str(exc))
        return 1
    result.write(args.out)
    log.info(
        "extract.cli.done",
        out=str(args.out),
        org=args.org,
        components=len(result.components),
        failures=len(result.failures),
    )
    return 0
