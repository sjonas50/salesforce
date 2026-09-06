"""``offramp extract`` — pull, normalize, build the graph, write artifacts."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from offramp.cli._org import add_source_args, connect, write_result
from offramp.core.logging import get_logger
from offramp.engram.client import open_client

log = get_logger(__name__)


def add_extract_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("extract", help="Extract an org's automation surface + data model.")
    add_source_args(p)
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    async with open_client() as engram:
        src = await connect(args, engram)
        if src is None:
            return 1
        try:
            result = await src.orchestrator.run()
        finally:
            await src.close()
    if not result.components:
        log.error(
            "extract.cli.nothing_extracted",
            gaps=result.coverage.suspected_gaps if result.coverage else [],
            hint="check org auth / --via; see coverage.json suspected_gaps",
        )
        write_result(result, args.out)
        return 4
    write_result(result, args.out, library=args.library)
    graph = result.build_graph()
    log.info(
        "extract.cli.done",
        out=str(args.out),
        components=len(result.components),
        failures=len(result.failures),
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        unresolved=len(graph.unresolved),
    )
    return 0
