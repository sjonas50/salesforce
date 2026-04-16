"""``offramp xray`` subcommand.

Drives Phase 2 end-to-end: extract → graph load → cluster → annotate → score
→ orphan-resolve → render HTML + JSON. Real FalkorDB (no mock) and real
Claude Sonnet 4.6 (no stub).
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from offramp.core.config import get_settings
from offramp.core.logging import get_logger
from offramp.engram.client import open_client
from offramp.extract.orchestrator import ExtractOrchestrator
from offramp.extract.pull.fixture import FixturePullClient
from offramp.understand.annotate import Annotation, Annotator
from offramp.understand.clustering import (
    build_networkx_graph,
    detect_processes,
    write_processes_to_graph,
)
from offramp.understand.complexity import score_all
from offramp.understand.graph_loader import (
    load_components,
    load_dispatch_edges,
    load_lwc_apex_edges,
    open_graph,
)
from offramp.understand.orphan.resolver import ResolutionInputs, resolve_orphans
from offramp.understand.xray.render import XRayInputs, write_xray

log = get_logger(__name__)


def add_xray_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("xray", help="Run extract + understand + render the X-Ray report.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture", type=Path, help="Fixture org dump.")
    src.add_argument("--org", help="Real Salesforce org alias (not yet wired).")
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.add_argument(
        "--graph-name",
        default=None,
        help="FalkorDB graph name (default = org alias).",
    )
    p.add_argument(
        "--cluster-resolution",
        type=float,
        default=1.0,
        help="Louvain resolution parameter (higher = more clusters).",
    )
    p.add_argument(
        "--annotation-concurrency",
        type=int,
        default=4,
        help="Max in-flight LLM calls.",
    )
    p.add_argument(
        "--skip-annotations",
        action="store_true",
        help="Skip the LLM annotation pass (useful for offline iteration).",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    if args.fixture is None:
        log.error("xray.real_org_not_wired", org=args.org)
        return 2
    return asyncio.run(_run_fixture(args))


async def _run_fixture(args: argparse.Namespace) -> int:
    fixture: Path = args.fixture
    if not fixture.is_dir():  # noqa: ASYNC240 — pre-async CLI guard
        log.error("xray.fixture_not_found", path=str(fixture))
        return 1
    settings = get_settings()
    org_alias = fixture.name
    graph_name = args.graph_name or org_alias.replace("/", "_")

    async with open_client() as engram:
        # === Phase 1 reuse: extract pipeline against fixture org ===
        client = FixturePullClient(fixture)
        orch = ExtractOrchestrator(
            org_alias=org_alias, client=client, engram=engram, fixture_root=fixture
        )
        result = await orch.run()
        log.info("xray.extract_done", components=len(result.components))

        # === Graph load ===
        handle = open_graph(url=settings.infra.falkordb_url, name=graph_name)
        load_components(handle, result.components)
        components_by_name = {
            c.api_name: str(c.id) for c in result.components if c.api_name is not None
        }
        load_dispatch_edges(handle, result.dispatch_edges, components_by_name=components_by_name)
        load_lwc_apex_edges(handle, result.components, components_by_name=components_by_name)

        # === Clustering ===
        nx_graph = build_networkx_graph(
            result.components, result.dispatch_edges, components_by_name=components_by_name
        )
        processes = detect_processes(nx_graph, resolution=args.cluster_resolution)
        write_processes_to_graph(handle, processes)

        # === Complexity scoring (deterministic, no LLM) ===
        complexity = score_all(result.components)

        # === LLM annotation ===
        annotations: list[Annotation] = []
        if not args.skip_annotations:
            if not settings.llm.api_key.get_secret_value():
                log.error("xray.llm_key_missing")
                return 3
            annotator = Annotator.from_settings(settings.llm, engram=engram)
            log.info(
                "xray.annotating",
                count=len(result.components),
                model=settings.llm.model,
                concurrency=args.annotation_concurrency,
            )
            annotations = await annotator.annotate_many(
                result.components, concurrency=args.annotation_concurrency
            )

        # === Orphan resolution ===
        orphans = resolve_orphans(ResolutionInputs(components=result.components))

        # === Assert coverage + OoE present (Phase 1 wired them) ===
        assert result.coverage is not None and result.ooe is not None

        # === Render X-Ray report ===
        write_xray(
            XRayInputs(
                org_alias=org_alias,
                components=result.components,
                coverage=result.coverage,
                ooe=result.ooe,
                dispatch_edges=result.dispatch_edges,
                annotations=annotations,
                complexity=complexity,
                processes=processes,
                orphans=orphans,
            ),
            args.out,
        )
    log.info("xray.cli.done", out=str(args.out))
    return 0
