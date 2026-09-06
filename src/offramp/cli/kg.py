"""``offramp kg`` — the reusable process library.

offramp kg ingest --from out/acme/extract --library ~/.offramp/library
offramp kg list   --library … [--kind record_triggered_flow] [--object Lead] [--org acme]
offramp kg search --library … "lead routing"
offramp kg show   --library … LeadRouting [--format md|mermaid|json|code]
offramp kg families --library …
offramp kg scans  --library …
offramp kg diff   --library … <scan_a> <scan_b>
offramp kg export --library … --out library.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.models import Component
from offramp.knowledge.render import to_markdown, to_mermaid
from offramp.knowledge.store import KnowledgeStore
from offramp.understand.process_ir import build_processes

log = get_logger(__name__)

DEFAULT_LIBRARY = Path(os.environ.get("OFFRAMP_LIBRARY", str(Path.home() / ".offramp" / "library")))


def add_kg_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("kg", help="Persistent, reusable process library (knowledge graph).")
    p.add_argument(
        "--library",
        type=Path,
        default=DEFAULT_LIBRARY,
        help=f"Library directory (default {DEFAULT_LIBRARY}).",
    )
    ops = p.add_subparsers(dest="kg_command", required=True)

    ing = ops.add_parser(
        "ingest", help="Add an extract output directory's processes to the library."
    )
    ing.add_argument("--from", dest="extract_dir", type=Path, required=True)
    ing.add_argument("--org-alias", default=None)
    ing.add_argument("--scan-id", default=None)
    ing.add_argument(
        "--falkordb", action="store_true", help="Also persist into FalkorDB (FALKORDB_URL)."
    )
    ing.set_defaults(func=_ingest)

    ls = ops.add_parser("list")
    ls.add_argument("--kind")
    ls.add_argument("--object")
    ls.add_argument("--org")
    ls.set_defaults(func=_list)

    se = ops.add_parser("search")
    se.add_argument("query")
    se.set_defaults(func=_search)

    sh = ops.add_parser("show")
    sh.add_argument("key", help="Process id, id prefix, or name")
    sh.add_argument("--format", choices=["md", "mermaid", "json", "code", "flowxml"], default="md")
    sh.set_defaults(func=_show)

    ops.add_parser("families").set_defaults(func=_families)
    ops.add_parser("scans").set_defaults(func=_scans)

    df = ops.add_parser("diff")
    df.add_argument("scan_a")
    df.add_argument("scan_b")
    df.set_defaults(func=_diff)

    ex = ops.add_parser("export")
    ex.add_argument("--out", type=Path, required=True)
    ex.set_defaults(func=_export)


def ingest_extract_dir(
    store: KnowledgeStore,
    extract_dir: Path,
    *,
    org_alias: str | None = None,
    scan_id: str | None = None,
) -> Any:
    comps_path = extract_dir / "components.json"
    if not comps_path.is_file():
        raise FileNotFoundError(comps_path)
    components = [
        Component.model_validate(c) for c in json.loads(comps_path.read_text(encoding="utf-8"))
    ]
    alias = org_alias or (components[0].org_alias if components else extract_dir.name)
    processes = build_processes(components, org_alias=alias, scan_id=scan_id)
    graph = None
    gp = extract_dir / "graph.json"
    if gp.is_file():
        graph = json.loads(gp.read_text(encoding="utf-8"))
    return store.ingest(
        processes,
        org_alias=alias,
        scan_id=scan_id,
        graph=graph,
        component_count=len(components),
        source=str(extract_dir),
    ), processes


def _ingest(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    try:
        rec, processes = ingest_extract_dir(
            store, args.extract_dir, org_alias=args.org_alias, scan_id=args.scan_id
        )
    except FileNotFoundError as exc:
        log.error("kg.ingest.missing", path=str(exc))
        return 1
    if args.falkordb:
        from offramp.core.config import get_settings
        from offramp.knowledge.falkor import persist
        from offramp.understand.graph_loader import open_graph

        try:
            handle = open_graph(url=get_settings().infra.falkordb_url, name="offramp_knowledge")
            persist(handle, processes, rec)
        except Exception as exc:
            log.warning("kg.falkordb_unavailable", error=str(exc))
    print(
        f"scan {rec.scan_id}: {len(rec.process_ids)} processes, {len(rec.new_process_ids)} new; library now {len(store.index())} processes at {store.root}"
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    rows = store.list_processes(kind=args.kind, obj=args.object, org=args.org)
    print(f"{len(rows)} processes")
    for r in rows:
        print(
            f"  {r.id[:12]}  {r.kind:30} {r.name:40} {r.fidelity:16} objs={','.join(r.objects)} orgs={','.join(r.orgs)}"
        )
    return 0


def _search(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    hits = store.search(args.query)
    print(f"{len(hits)} hits for {args.query!r}")
    for score, r in hits:
        print(f"  {score:3}  {r.id[:12]}  {r.kind:30} {r.name:40} {' · '.join(r.objects)}")
    return 0


def _show(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    p = store.resolve(args.key)
    if p is None:
        log.error("kg.show.not_found", key=args.key)
        return 2
    if args.format == "json":
        print(p.model_dump_json(indent=2))
    if args.format == "flowxml":
        from offramp.knowledge.flow_xml import to_flow_xml

        print(to_flow_xml(p))
    elif args.format == "mermaid":
        print(to_mermaid(p))
    elif args.format == "code":
        code = store.code(p)
        print(code if code else f"# no source body stored for {p.name}")
    else:
        print(to_markdown(p))
    return 0


def _families(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    fams = store.families()
    print(f"{len(fams)} families (processes sharing a shape)")
    for f in fams:
        print(
            f"  {f.fingerprint[:12]}  x{len(f.members)}  {f.members[0].kind}: "
            + ", ".join(m.name for m in f.members[:6])
            + (" …" if len(f.members) > 6 else "")
        )
    return 0


def _scans(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    for s in store.scans():
        print(
            f"  {s.scan_id:40} {s.org_alias:16} {s.scanned_at.isoformat(timespec='seconds')}  processes={len(s.process_ids)} new={len(s.new_process_ids)}"
        )
    return 0


def _diff(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    try:
        d = store.diff(args.scan_a, args.scan_b)
    except KeyError as exc:
        log.error("kg.diff.unknown_scan", scan=str(exc))
        return 2
    for key in ("added", "removed"):
        print(f"{key}: {len(d[key])}")
        for r in d[key]:
            print(f"  {r.kind:30} {r.name}")
    print(f"unchanged: {len(d['unchanged'])}")
    return 0


def _export(args: argparse.Namespace) -> int:
    store = KnowledgeStore(args.library)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(store.export(), indent=1, sort_keys=True, default=str), encoding="utf-8"
    )
    print(f"wrote {args.out} ({len(store.index())} processes)")
    return 0
