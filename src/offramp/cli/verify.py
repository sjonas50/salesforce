"""``offramp verify``: run flows in the org under a debug trace and compare with the model.

Offline: ``--log file.log`` parses a saved debug log instead of running anything,
so a trace captured any other way (Developer Console, Setup) verifies the model too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger
from offramp.core.process import ProcessDefinition
from offramp.verify.compare import (
    Check,
    VerificationResult,
    compare_roundtrip,
    find_trace,
    verify_process,
)
from offramp.verify.runner import Recipe, deploy_roundtrip_copy, run_with_trace
from offramp.verify.trace import parse_debug_log

log = get_logger(__name__)

_FLOW_KINDS = {
    "record_triggered_flow",
    "autolaunched_flow",
    "schedule_triggered_flow",
    "platform_event_triggered_flow",
    "screen_flow",
}


def add_verify_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("verify", help="Verify process definitions against flow execution traces.")
    p.add_argument("--from", dest="extract_dir", type=Path, required=True, help="Extract dir.")
    p.add_argument("--flow", action="append", default=[], help="Flow API name (repeatable).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--log", type=Path, help="Offline: a saved Apex debug log to compare.")
    src.add_argument("--org", help="Run the flows in this org (needs --recipes for record flows).")
    p.add_argument("--auth", choices=["jwt", "sf-cli"], default=None)
    p.add_argument("--username", help="User to trace (defaults to SF_USERNAME / settings).")
    p.add_argument(
        "--recipes", type=Path, help='JSON: {"FlowName": {"object","create","update","inputs"}}'
    )
    p.add_argument(
        "--roundtrip",
        action="store_true",
        help="Also render each definition back to Flow XML, deploy it as <Name>_rt, and require "
        "the copy to take the same path with the same DML as the original.",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_run)


def load_processes(extract_dir: Path, names: list[str]) -> list[ProcessDefinition]:
    raw = json.loads((extract_dir / "processes.json").read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("processes", [])
    out = []
    for item in items:
        if item.get("kind") not in _FLOW_KINDS:
            continue
        if names and item.get("name") not in names:
            continue
        out.append(ProcessDefinition.model_validate(item))
    return out


def _print(results: list[VerificationResult], as_json: bool) -> None:
    if as_json:
        print(json.dumps([r.to_jsonable() for r in results], indent=2))
        return
    for r in results:
        print(f"{r.status:<15} {r.process}")
        for c in r.checks:
            mark = "ok " if c.ok else "XX "
            print(f"    {mark} {c.name:<20} {c.detail}")
        if r.path:
            print(f"    path: {' > '.join(r.path)}")
    counts = {
        s: sum(1 for r in results if r.status == s) for s in ("pass", "mismatch", "not_verifiable")
    }
    print(
        f"verified: {counts['pass']} pass, {counts['mismatch']} mismatch, {counts['not_verifiable']} not verifiable"
    )


def _run(args: argparse.Namespace) -> int:
    processes = load_processes(args.extract_dir, args.flow)
    if not processes:
        log.error("cli.verify.no_flows", extract_dir=str(args.extract_dir))
        return 2
    if args.log is not None:
        traces = parse_debug_log(args.log.read_text(encoding="utf-8"))
        results = [verify_process(p, find_trace(p, traces)) for p in processes]
    else:
        results = asyncio.run(_run_org(args, processes))
    _print(results, args.json)
    return 0 if all(r.status != "mismatch" for r in results) else 1


async def _run_org(
    args: argparse.Namespace, processes: list[ProcessDefinition]
) -> list[VerificationResult]:
    from offramp.core.config import get_settings
    from offramp.engram.client import InMemoryEngramClient
    from offramp.mcp.server import MCPGateway
    from offramp.mcp.sf_backend import SimpleSalesforceBackend

    settings = get_settings()
    updates: dict[str, Any] = {"org_alias": args.org}
    if args.auth:
        updates["auth_mode"] = args.auth
    backend = SimpleSalesforceBackend(
        settings=settings.salesforce.model_copy(update=updates), process_id="verify", quota=None
    )
    gateway = MCPGateway(backend=backend, engram=InMemoryEngramClient())
    recipes = json.loads(args.recipes.read_text(encoding="utf-8")) if args.recipes else {}
    try:
        username = args.username or getattr(settings.salesforce, "username", None)
        if not username:
            log.error("cli.verify.no_username", hint="pass --username")
            return [VerificationResult(p.name, "not_verifiable") for p in processes]
        rows = (await gateway.sf_query(f"SELECT Id FROM User WHERE Username = '{username}'")).get(
            "records", []
        )
        if not rows:
            log.error("cli.verify.user_not_found", username=username)
            return [VerificationResult(p.name, "not_verifiable") for p in processes]
        user_id = str(rows[0]["Id"])
        results = []
        for p in processes:
            recipe = Recipe.from_dict(recipes.get(p.name, {}))
            deployed_copy: str | None = None
            if args.roundtrip:
                try:
                    dep = await deploy_roundtrip_copy(gateway, p)
                    deployed_copy = dep["copy"] if dep.get("status") == "Succeeded" else None
                    if deployed_copy is None:
                        log.error("cli.verify.roundtrip_deploy_failed", process=p.name, result=dep)
                except Exception as exc:
                    log.error(
                        "cli.verify.roundtrip_render_failed", process=p.name, error=str(exc)[:200]
                    )
            outcome = await run_with_trace(gateway, p, recipe, user_id=user_id)
            if not outcome.log_text:
                results.append(
                    VerificationResult(
                        p.name,
                        "not_verifiable",
                        [Check("trace_found", False, outcome.note or "no log produced")],
                    )
                )
                continue
            traces = parse_debug_log(outcome.log_text)
            original = find_trace(p, traces)
            result = verify_process(p, original)
            if args.roundtrip:
                if deployed_copy is None:
                    result.checks.append(Check("roundtrip_copy_ran", False, "copy not deployed"))
                elif original is not None:
                    copy_label = f"{p.label or p.name} (rt)".lower()
                    copy = next((t for t in traces if t.flow_label.lower() == copy_label), None)
                    result.checks.append(compare_roundtrip(original, copy))
                if any(not c.ok for c in result.checks) and result.status == "pass":
                    result.status = "mismatch"
            results.append(result)
        return results
    finally:
        await backend.aclose()
