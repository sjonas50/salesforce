"""``offramp changes``: read the per-org change log; ``offramp sync``: rescan on an interval."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.understand.changes import ChangeLog, ChangeSet, ComponentChange

log = get_logger(__name__)


def add_changes_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "changes", help="What changed in an org between scans (from the library's change log)."
    )
    p.add_argument("--library", type=Path, required=True)
    p.add_argument("--org", required=True, help="Org alias the scans were recorded under.")
    p.add_argument("--last", type=int, default=None, help="Only the last N scans.")
    p.add_argument("--component", help="History of one component: <category>:<api_name>.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_changes)

    s = sub.add_parser(
        "sync", help="Scan an org into a library now, or every N seconds, recording changes."
    )
    from offramp.cli._org import add_source_args

    add_source_args(s)
    s.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Extract dir (default: <library>/extracts/<org>/<time>).",
    )
    s.add_argument("--interval", type=int, default=0, help="Seconds between scans; 0 = once.")
    s.add_argument(
        "--max-runs", type=int, default=0, help="Stop after N scans (0 = until interrupted)."
    )
    s.set_defaults(func=_sync)


def _as_changeset(row: dict[str, Any]) -> ChangeSet:
    def items(kind: str) -> list[ComponentChange]:
        return [
            ComponentChange(
                str(c["category"]),
                str(c["api_name"]),
                str(c["change"]),
                c.get("before_hash"),
                c.get("after_hash"),
                dict(c.get("details") or {}),
            )
            for c in row.get(kind, [])
        ]

    from_scan = row.get("from_scan")
    return ChangeSet(
        org_alias=str(row["org_alias"]),
        from_scan=str(from_scan) if from_scan else None,
        to_scan=str(row["to_scan"]),
        at=datetime.fromisoformat(str(row["at"])),
        added=items("added"),
        removed=items("removed"),
        modified=items("modified"),
    )


def _changes(args: argparse.Namespace) -> int:
    cl = ChangeLog(args.library)
    if args.component:
        cat, _, name = args.component.partition(":")
        rows = cl.history(args.org, cat, name)
        print(
            json.dumps(rows, indent=2)
            if args.json
            else "\n".join(
                f"{r['at']}  {r['scan']}  {r['change']}  {r.get('details') or ''}" for r in rows
            )
            or "no history"
        )
        return 0
    rows = cl.read(args.org, last=args.last)
    if args.json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print(f"no scans recorded for {args.org} in {args.library}")
    else:
        print("\n\n".join(_as_changeset(r).to_text() for r in rows))
    return 0


def _sync(args: argparse.Namespace) -> int:
    runs = 0
    while True:
        rc = asyncio.run(_sync_once(args))
        runs += 1
        if args.interval <= 0 or (args.max_runs and runs >= args.max_runs) or rc not in (0, 4):
            return rc
        log.info("sync.sleeping", seconds=args.interval)
        time.sleep(args.interval)


async def _sync_once(args: argparse.Namespace) -> int:
    from offramp.cli._org import connect, write_result
    from offramp.engram.client import open_client

    if args.library is None:
        log.error("sync.library_required", hint="pass --library DIR")
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    alias = args.org_alias or args.org or (args.source_dir or args.fixture).name
    out = args.out or (args.library / "extracts" / alias / stamp)
    async with open_client() as engram:
        src = await connect(args, engram)
        if src is None:
            return 1
        try:
            result = await src.orchestrator.run()
        finally:
            await src.close()
    if not result.components:
        log.error("sync.nothing_extracted", org=alias)
        return 4
    write_result(result, out, library=args.library)
    return 0
