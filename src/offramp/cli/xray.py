"""``offramp xray`` — extract → graph → cluster → score → annotate → orphans → report.

Runs against a fixture, an SFDX directory, or a live org (see ``cli/_org``).
FalkorDB is optional (``--no-graph-db``); the LLM annotation pass is
optional (``--skip-annotations``) and needs ``LLM_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from offramp.cli._org import add_source_args, connect, write_result
from offramp.core.config import get_settings
from offramp.core.logging import get_logger
from offramp.core.models import AUTOMATION_CATEGORIES
from offramp.engram.client import open_client
from offramp.understand.annotate import (
    Annotation,
    Annotator,
    ProcessAnnotation,
    load_annotations,
    load_process_annotations,
    save_annotations,
    save_process_annotations,
)
from offramp.understand.annotate_context import DossierInputs, build_dossiers
from offramp.understand.clustering import build_networkx_graph, detect_processes
from offramp.understand.complexity import score_all
from offramp.understand.health import run_health_checks
from offramp.understand.orphan.resolver import ResolutionInputs, resolve_orphans
from offramp.understand.process_ir import build_processes
from offramp.understand.xray.render import XRayInputs, write_xray

log = get_logger(__name__)


def add_xray_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("xray", help="Produce the X-Ray report for an org.")
    add_source_args(p)
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.add_argument("--graph-name", default=None, help="FalkorDB graph name (default = org alias).")
    p.add_argument(
        "--no-graph-db", action="store_true", help="Do not load FalkorDB; analysis runs in memory."
    )
    p.add_argument(
        "--cluster-resolution",
        type=float,
        default=1.0,
        help="Community-detection resolution (higher = more clusters).",
    )
    p.add_argument("--cluster-algorithm", choices=["louvain", "leiden"], default="louvain")
    p.add_argument("--annotation-concurrency", type=int, default=4, help="Max in-flight LLM calls.")
    p.add_argument("--skip-annotations", action="store_true", help="Skip the LLM annotation pass.")
    p.add_argument(
        "--annotations",
        type=Path,
        help="Reuse a saved annotations.json (matched by content hash) instead of calling the LLM.",
    )
    p.add_argument(
        "--annotate-surfaces",
        action="store_true",
        help="Also annotate layouts, pages, profiles, permission sets, reports, tabs, apps and "
        "paths (default: automation categories only).",
    )
    p.add_argument(
        "--save-impact",
        action="append",
        default=[],
        metavar="OBJECT",
        help="Object(s) to render save-impact tables for (default: busiest 8).",
    )
    p.set_defaults(func=_run)


def _run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_async(args))


async def _run_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    if (
        not args.skip_annotations
        and args.annotations is None
        and not settings.llm.api_key.get_secret_value()
    ):
        log.error("xray.llm_key_missing", hint="set LLM_API_KEY or pass --skip-annotations")
        return 3
    async with open_client() as engram:
        src = await connect(args, engram)
        if src is None:
            return 1
        try:
            result = await src.orchestrator.run()
        finally:
            await src.close()
        org_alias = src.org_alias
        write_result(result, args.out / "extract", library=args.library)

        graph = result.build_graph()
        log.info(
            "xray.graph",
            nodes=len(graph.nodes),
            edges=len(graph.edges),
            unresolved=len(graph.unresolved),
        )

        # ---- clustering ----
        nx_graph = build_networkx_graph(graph)
        processes = detect_processes(
            nx_graph, resolution=args.cluster_resolution, algorithm=args.cluster_algorithm
        )

        # ---- optional FalkorDB ----
        if not args.no_graph_db:
            from offramp.understand.clustering import write_processes_to_graph
            from offramp.understand.graph_loader import load_dependency_graph, open_graph

            try:
                handle = open_graph(
                    url=settings.infra.falkordb_url,
                    name=args.graph_name or org_alias.replace("/", "_"),
                )
                load_dependency_graph(handle, graph)
                write_processes_to_graph(handle, processes)
            except Exception as exc:
                log.warning(
                    "xray.graph_db_unavailable", error=str(exc), hint="use --no-graph-db to silence"
                )

        # ---- deterministic scoring ----
        complexity = score_all(result.components)

        # ---- LLM annotation ----
        annotations: list[Annotation] = []
        process_annotations: list[ProcessAnnotation] = []
        if args.annotations is not None:
            annotations = load_annotations(args.annotations, result.components)
            log.info("xray.annotations_reused", count=len(annotations), path=str(args.annotations))
            sibling = args.annotations.parent / "process_annotations.json"
            if sibling.exists():
                process_annotations = load_process_annotations(sibling)
        elif not args.skip_annotations:
            annotator = Annotator.from_settings(settings.llm, engram=engram)
            to_annotate = [
                c
                for c in result.components
                if args.annotate_surfaces or c.category in AUTOMATION_CATEGORIES
            ]
            definitions = build_processes(result.components, org_alias=org_alias)
            dossiers = build_dossiers(
                DossierInputs(
                    components=result.components,
                    graph=graph,
                    health=run_health_checks(result.components, definitions, result.schema),
                    complexity=complexity,
                    packages=result.packages,
                    org_alias=org_alias,
                )
            )
            log.info("xray.annotating", count=len(to_annotate), model=settings.llm.model)
            annotations = await annotator.annotate_many(
                to_annotate, concurrency=args.annotation_concurrency, dossiers=dossiers
            )
            if annotations:
                process_annotations = await annotator.annotate_processes(
                    processes,
                    result.components,
                    annotations,
                    graph=graph,
                    concurrency=args.annotation_concurrency,
                )
        if annotations:
            save_annotations(annotations, result.components, args.out / "annotations.json")
        if process_annotations:
            save_process_annotations(process_annotations, args.out / "process_annotations.json")

        # ---- orphans ----
        orphans = resolve_orphans(ResolutionInputs(components=result.components, graph=graph))

        assert result.coverage is not None and result.ooe is not None
        partial = sorted(
            {
                c.category.value
                for c in result.components
                if isinstance(c.raw, dict) and c.raw.get("partial")
            }
        )
        write_xray(
            XRayInputs(
                org_alias=org_alias,
                components=result.components,
                coverage=result.coverage,
                ooe=result.ooe,
                graph=graph,
                processes=processes,
                orphans=orphans,
                complexity=complexity,
                annotations=annotations,
                process_annotations=process_annotations,
                schema=result.schema,
                save_impact_objects=list(args.save_impact),
                partial_categories=partial,
            ),
            args.out,
        )
    log.info(
        "xray.cli.done", out=str(args.out), processes=len(processes), orphans=orphans.total_orphans
    )
    return 0
