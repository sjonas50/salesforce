"""``offramp generate`` subcommand."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from offramp.core.logging import get_logger
from offramp.engram.client import open_client
from offramp.extract.orchestrator import ExtractOrchestrator
from offramp.extract.pull.fixture import FixturePullClient
from offramp.generate.orchestrator import GenerateOrchestrator, write_skipped_report

log = get_logger(__name__)


def add_generate_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("generate", help="Generate runtime artifacts from a fixture/org.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture", type=Path, help="Fixture org dump.")
    src.add_argument("--org", help="Real Salesforce org alias (not yet wired).")
    p.add_argument("--out", type=Path, required=True, help="Output artifact directory.")
    p.add_argument(
        "--process-id",
        default=None,
        help="Process identifier baked into the manifest (default = fixture dir name).",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    if args.fixture is None:
        log.error("generate.real_org_not_wired", org=args.org)
        return 2
    return asyncio.run(_run_fixture(args))


async def _run_fixture(args: argparse.Namespace) -> int:
    fixture: Path = args.fixture
    if not fixture.is_dir():  # noqa: ASYNC240 — pre-async CLI guard
        log.error("generate.fixture_not_found", path=str(fixture))
        return 1
    org_alias = fixture.name
    process_id = args.process_id or org_alias

    async with open_client() as engram:
        client = FixturePullClient(fixture)
        ext = ExtractOrchestrator(
            org_alias=org_alias, client=client, engram=engram, fixture_root=fixture
        )
        ext_result = await ext.run()

        gen = GenerateOrchestrator(process_id=process_id, out_dir=args.out, engram=engram)
        gen_result = await gen.run(ext_result.components)

    print(write_skipped_report(gen_result))
    return 0
