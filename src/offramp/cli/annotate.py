"""``offramp annotate``: LLM-annotate an existing extract, no org calls.

Builds the same context the X-Ray run uses — dependency graph, health findings,
complexity scores, installed packages, optional live verification results —
so every component is annotated from a dossier, then (``--processes``) writes
a narrative per detected business process. Outputs live next to the extract:
``annotations.json`` and ``process_annotations.json``; ``xray --annotations``
reuses them by content hash.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from offramp.cli.health import load_extract
from offramp.core.config import get_settings
from offramp.core.logging import get_logger
from offramp.core.models import AUTOMATION_CATEGORIES, Component, DataProfile, SchemaSnapshot
from offramp.core.process import ProcessDefinition
from offramp.engram.client import open_client
from offramp.extract.dispatch.class_resolver import DispatchEdge
from offramp.understand.annotate import (
    Annotation,
    Annotator,
    save_annotations,
    save_process_annotations,
)
from offramp.understand.annotate_context import DossierInputs, build_dossiers
from offramp.understand.clustering import BusinessProcess, build_networkx_graph, detect_processes
from offramp.understand.complexity import score_all
from offramp.understand.dependencies import DependencyGraph, build_graph
from offramp.understand.health import run_health_checks

log = get_logger(__name__)


@dataclass
class ExtractContext:
    """Everything an annotation pass needs, rebuilt from an extract directory."""

    org_alias: str
    components: list[Component]
    definitions: list[ProcessDefinition]
    schema: SchemaSnapshot | None
    graph: DependencyGraph
    processes: list[BusinessProcess]
    packages: list[dict[str, Any]]


def load_context(extract_dir: Path, *, org_alias: str | None = None) -> ExtractContext:
    comps, defs, schema = load_extract(extract_dir)
    alias = org_alias or (comps[0].org_alias if comps else "org")
    dispatch: list[DispatchEdge] = []
    p = extract_dir / "dispatch_edges.json"
    if p.exists():
        dispatch = [DispatchEdge(**row) for row in json.loads(p.read_text(encoding="utf-8"))]
    profile = None
    p = extract_dir / "data_profile.json"
    if p.exists():
        profile = DataProfile.model_validate_json(p.read_text(encoding="utf-8"))
    packages: list[dict[str, Any]] = []
    p = extract_dir / "packages.json"
    if p.exists():
        packages = [r for r in json.loads(p.read_text(encoding="utf-8")) if isinstance(r, dict)]
    graph = build_graph(
        org_alias=alias,
        components=comps,
        schema=schema,
        dispatch_edges=dispatch,
        data_profile=profile,
    )
    processes = detect_processes(build_networkx_graph(graph))
    return ExtractContext(alias, comps, defs, schema, graph, processes, packages)


def dossier_inputs(
    ctx: ExtractContext, *, verify_results: list[dict[str, Any]] | None = None, budget: int
) -> DossierInputs:
    return DossierInputs(
        components=ctx.components,
        graph=ctx.graph,
        health=run_health_checks(ctx.components, ctx.definitions, ctx.schema),
        complexity=score_all(ctx.components),
        packages=ctx.packages,
        verify_results=verify_results or [],
        org_alias=ctx.org_alias,
        budget=budget,
    )


def add_annotate_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "annotate",
        help="Run the LLM annotation pass over an extract dir; reuse with `xray --annotations`.",
    )
    p.add_argument("--from", dest="extract_dir", type=Path, required=True, help="Extract dir.")
    p.add_argument(
        "--out",
        type=Path,
        help="annotations.json to write (default: <extract dir>/annotations.json).",
    )
    p.add_argument("--concurrency", type=int, default=4, help="Max in-flight LLM calls.")
    p.add_argument("--annotate-surfaces", action="store_true", help="Include surface categories.")
    p.add_argument("--category", action="append", default=[], help="Only these categories.")
    p.add_argument("--name", action="append", default=[], help="Only these component names.")
    p.add_argument("--limit", type=int, default=0, help="Annotate at most N components (a trial).")
    p.add_argument(
        "--processes",
        action="store_true",
        help="Also write a narrative per business process (cluster) with 2+ annotated members.",
    )
    p.add_argument(
        "--verify-results", type=Path, help="Output of `offramp verify --json` to cite as facts."
    )
    p.add_argument(
        "--budget", type=int, default=30_000, help="Max characters of dossier per component."
    )
    p.add_argument(
        "--dump-context", type=Path, help="Write each component's dossier text into this dir."
    )
    p.set_defaults(func=_run)


def _merge_existing(
    out: Path, fresh: list[Annotation], components: list[Component]
) -> list[Annotation]:
    """Keep earlier annotations for components not re-annotated in this run."""
    if not out.exists():
        return fresh
    from offramp.understand.annotate import load_annotations

    done = {a.component_id for a in fresh}
    kept = [a for a in load_annotations(out, components) if a.component_id not in done]
    return fresh + kept


def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.llm.api_key.get_secret_value():
        log.error("annotate.llm_key_missing", hint="set LLM_API_KEY in .env")
        return 2
    ctx = load_context(args.extract_dir)
    wanted = {c.lower() for c in args.category}
    names = {n.lower() for n in args.name}
    todo = [
        c
        for c in ctx.components
        if (
            c.category.value in wanted
            if wanted
            else (args.annotate_surfaces or c.category in AUTOMATION_CATEGORIES)
        )
        and (not names or c.name.lower() in names)
    ]
    if args.limit:
        todo = todo[: args.limit]
    verify_rows: list[dict[str, Any]] = []
    if args.verify_results:
        verify_rows = json.loads(args.verify_results.read_text(encoding="utf-8"))
    inputs = dossier_inputs(ctx, verify_results=verify_rows, budget=args.budget)
    dossiers = build_dossiers(inputs)
    if args.dump_context:
        args.dump_context.mkdir(parents=True, exist_ok=True)
        for c in todo:
            d = dossiers[str(c.id)]
            (args.dump_context / f"{c.category.value}__{c.name}.md").write_text(
                d.text, encoding="utf-8"
            )
    out = args.out or (args.extract_dir / "annotations.json")

    async def _go() -> int:
        async with open_client() as engram:
            annotator = Annotator.from_settings(settings.llm, engram=engram)
            log.info("annotate.start", count=len(todo), model=settings.llm.model)
            anns: list[Annotation] = await annotator.annotate_many(
                todo, concurrency=args.concurrency, dossiers=dossiers
            )
            merged = _merge_existing(out, anns, ctx.components)
            save_annotations(merged, ctx.components, out)
            print(
                f"annotated {len(anns)} of {len(todo)} components -> {out}"
                + (
                    f" ({len(merged) - len(anns)} kept from the previous file)"
                    if len(merged) > len(anns)
                    else ""
                )
            )
            if args.processes and anns:
                pas = await annotator.annotate_processes(
                    ctx.processes,
                    ctx.components,
                    anns,
                    graph=ctx.graph,
                    concurrency=args.concurrency,
                )
                pout = out.parent / "process_annotations.json"
                save_process_annotations(pas, pout)
                print(f"annotated {len(pas)} business processes -> {pout}")
        return 0 if anns or not todo else 1

    return asyncio.run(_go())
