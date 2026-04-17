"""``offramp cutover`` subcommand: begin / advance / rollback / status / parity / monitor."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from offramp.core.config import get_settings
from offramp.core.logging import get_logger
from offramp.cutover.orchestrator import CutoverOrchestrator, TransitionKind
from offramp.cutover.parity_report import (
    ParityCategory,
    ParityFinding,
    ParityReport,
    anchor_findings,
    write,
)
from offramp.cutover.post_cutover_monitor import PostCutoverMonitor
from offramp.cutover.provenance import CutoverProvenance
from offramp.engram.client import open_client
from offramp.mcp.routing import RoutingTable
from offramp.validate.shadow.readiness import ReadinessScorer
from offramp.validate.shadow.store import open_store

log = get_logger(__name__)


def add_cutover_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("cutover", help="Cutover orchestration commands.")
    sp = p.add_subparsers(dest="cutover_cmd", required=True)

    begin = sp.add_parser("begin", help="Initialize routing for a process at 1%.")
    begin.add_argument("--process-id", required=True)
    begin.add_argument("--hash-seed", default=None)
    begin.set_defaults(func=_run_begin)

    advance = sp.add_parser("advance", help="Evaluate + (if eligible) advance one stage.")
    advance.add_argument("--process-id", required=True)
    advance.add_argument("--dry-run", action="store_true", help="Print decision; do not apply.")
    advance.add_argument(
        "--confirm", action="store_true", help="Confirm immediate-rollback if needed."
    )
    advance.set_defaults(func=_run_advance)

    rollback = sp.add_parser("rollback", help="Force an instant rollback to 0%.")
    rollback.add_argument("--process-id", required=True)
    rollback.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="Required: rollback to 0% is destructive and human-confirmed.",
    )
    rollback.set_defaults(func=_run_rollback)

    status = sp.add_parser("status", help="Print routing config + readiness for a process.")
    status.add_argument("--process-id", required=True)
    status.set_defaults(func=_run_status)

    parity = sp.add_parser("parity-report", help="Render the Behavioral Parity Report.")
    parity.add_argument("--process-id", required=True)
    parity.add_argument("--org-alias", required=True)
    parity.add_argument("--out", type=Path, required=True)
    parity.add_argument(
        "--findings-file",
        type=Path,
        help="Optional JSON file with hand-curated findings to seed the report.",
    )
    parity.set_defaults(func=_run_parity)

    monitor = sp.add_parser("monitor", help="Post-cutover regression check (one-shot poll).")
    monitor.add_argument("--process-id", required=True)
    monitor.add_argument(
        "--auto-rollback",
        action="store_true",
        help="If a regression is detected, instantly roll back to 0%.",
    )
    monitor.set_defaults(func=_run_monitor)


def _run_begin(args: argparse.Namespace) -> int:
    return asyncio.run(_async_begin(args))


def _run_advance(args: argparse.Namespace) -> int:
    return asyncio.run(_async_advance(args))


def _run_rollback(args: argparse.Namespace) -> int:
    return asyncio.run(_async_rollback(args))


def _run_status(args: argparse.Namespace) -> int:
    return asyncio.run(_async_status(args))


def _run_parity(args: argparse.Namespace) -> int:
    return asyncio.run(_async_parity(args))


def _run_monitor(args: argparse.Namespace) -> int:
    return asyncio.run(_async_monitor(args))


# Each command opens its own store + engram + routing table so the CLI
# remains a thin layer on the library APIs.


def _routing_dsn() -> str:
    s = get_settings()
    return s.infra.postgres_dsn


def _shadow_dsn() -> str:
    s = get_settings()
    return s.infra.postgres_shadow_dsn


async def _async_begin(args: argparse.Namespace) -> int:
    routing = RoutingTable(dsn=_routing_dsn())
    await routing.connect()
    await routing.reload()
    async with open_store(_shadow_dsn()) as store, open_client() as engram:
        scorer = ReadinessScorer(store=store)
        provenance = CutoverProvenance(engram=engram)
        orch = CutoverOrchestrator(routing=routing, scorer=scorer, provenance=provenance)
        result = await orch.begin(process_id=args.process_id, hash_seed=args.hash_seed)
    await routing.close()
    print(json.dumps(_jsonable(result), indent=2))
    return 0


async def _async_advance(args: argparse.Namespace) -> int:
    routing = RoutingTable(dsn=_routing_dsn())
    await routing.connect()
    await routing.reload()
    async with open_store(_shadow_dsn()) as store, open_client() as engram:
        scorer = ReadinessScorer(store=store)
        provenance = CutoverProvenance(engram=engram)
        orch = CutoverOrchestrator(routing=routing, scorer=scorer, provenance=provenance)
        decision = await orch.evaluate(args.process_id)
        if args.dry_run:
            print(json.dumps(_jsonable_decision(decision), indent=2))
            await routing.close()
            return 0
        outcome = await orch.apply(decision, confirmed=args.confirm)
    await routing.close()
    print(
        json.dumps(
            _jsonable({"decision": _jsonable_decision(decision), "outcome": outcome}), indent=2
        )
    )
    return 0 if (decision.kind is TransitionKind.HOLD or outcome.get("applied")) else 1


async def _async_rollback(args: argparse.Namespace) -> int:
    routing = RoutingTable(dsn=_routing_dsn())
    await routing.connect()
    await routing.reload()
    async with open_client() as engram:
        provenance = CutoverProvenance(engram=engram)
        existing = await routing.get_config(args.process_id)
        if existing is None:
            log.error("cutover.rollback.no_config", process=args.process_id)
            await routing.close()
            return 1
        new_cfg = await routing.instant_rollback(args.process_id)
        await provenance.anchor_stage_transition(
            process_id=args.process_id,
            from_percent=existing.stage_percent,
            to_percent=0,
            readiness_score=0,
            kind="instant_rollback",
            reason="manual rollback via CLI",
        )
    await routing.close()
    print(
        json.dumps(
            {
                "rolled_back": True,
                "process_id": args.process_id,
                "from_percent": existing.stage_percent,
                "to_percent": 0,
                "new_config_entered_at": new_cfg.entered_stage_at.isoformat() if new_cfg else None,
            },
            indent=2,
        )
    )
    return 0


async def _async_status(args: argparse.Namespace) -> int:
    routing = RoutingTable(dsn=_routing_dsn())
    await routing.connect()
    await routing.reload()
    async with open_store(_shadow_dsn()) as store:
        scorer = ReadinessScorer(store=store)
        cfg = await routing.get_config(args.process_id)
        score = await scorer.score(args.process_id)
    await routing.close()
    print(
        json.dumps(
            {
                "process_id": args.process_id,
                "stage_percent": cfg.stage_percent if cfg else None,
                "entered_stage_at": cfg.entered_stage_at.isoformat() if cfg else None,
                "dwell_remaining_s": cfg.dwell_remaining().total_seconds() if cfg else None,
                "readiness_score": score.score,
                "cutover_eligible": score.cutover_eligible,
                "reason": score.reason,
            },
            indent=2,
        )
    )
    return 0


async def _async_parity(args: argparse.Namespace) -> int:
    findings: list[ParityFinding] = []
    if args.findings_file is not None and args.findings_file.is_file():
        spec = json.loads(args.findings_file.read_text())
        for entry in spec:
            findings.append(
                ParityFinding(
                    finding_id=entry["finding_id"],
                    category=ParityCategory(entry["category"]),
                    salesforce_behavior=entry["salesforce_behavior"],
                    runtime_behavior=entry["runtime_behavior"],
                    rationale=entry["rationale"],
                    customer_disposition=entry.get("customer_disposition", "pending"),
                    severity=entry.get("severity", "info"),
                    references=entry.get("references", []),
                )
            )
    report = ParityReport(
        process_id=args.process_id,
        org_alias=args.org_alias,
        findings=findings,
    )
    async with open_client() as engram:
        await anchor_findings(report, engram=engram)
    json_path, html_path = write(report, args.out)
    print(
        json.dumps(
            {
                "process_id": args.process_id,
                "findings": len(report.findings),
                "json_path": str(json_path),
                "html_path": str(html_path),
            },
            indent=2,
        )
    )
    return 0


async def _async_monitor(args: argparse.Namespace) -> int:
    routing = RoutingTable(dsn=_routing_dsn())
    await routing.connect()
    await routing.reload()
    async with open_store(_shadow_dsn()) as store, open_client() as engram:
        scorer = ReadinessScorer(store=store)
        provenance = CutoverProvenance(engram=engram)
        orch = CutoverOrchestrator(routing=routing, scorer=scorer, provenance=provenance)
        monitor = PostCutoverMonitor(
            routing=routing,
            scorer=scorer,
            provenance=provenance,
            orchestrator=orch,
            auto_rollback=args.auto_rollback,
        )
        alert = await monitor.check(args.process_id)
    await routing.close()
    if alert is None:
        print(json.dumps({"process_id": args.process_id, "regression": False}, indent=2))
        return 0
    print(
        json.dumps(
            {
                "process_id": alert.process_id,
                "regression": True,
                "score": alert.score,
                "threshold": alert.threshold,
                "auto_rollback_triggered": alert.auto_rollback_triggered,
                "explanation": alert.explanation,
            },
            indent=2,
        )
    )
    return 2  # exit code signals regression detected


def _jsonable(payload: Any) -> Any:
    """Best-effort dataclass / Pydantic / datetime renderer."""
    from dataclasses import asdict, is_dataclass
    from datetime import datetime as _dt

    if is_dataclass(payload) and not isinstance(payload, type):
        return {k: _jsonable(v) for k, v in asdict(payload).items()}
    if isinstance(payload, dict):
        return {k: _jsonable(v) for k, v in payload.items()}
    if isinstance(payload, list | tuple):
        return [_jsonable(v) for v in payload]
    if isinstance(payload, _dt):
        return payload.isoformat()
    return payload


def _jsonable_decision(decision: Any) -> dict[str, Any]:
    return {
        "process_id": decision.process_id,
        "kind": decision.kind.value,
        "from_percent": decision.from_percent,
        "to_percent": decision.to_percent,
        "readiness_score": decision.readiness_score,
        "reason": decision.reason,
        "requires_human_signoff": decision.requires_human_signoff,
        "decided_at": decision.decided_at.isoformat(),
    }
