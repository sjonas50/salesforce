"""``offramp shadow`` subcommand: start / status / report / replay-log.

* ``offramp shadow start`` — drive the shadow executor against a synthetic
  CDC stream loaded from a JSON file (or, when --pubsub is set, the real
  Salesforce Pub/Sub API). Useful for both local iteration and live runs.
* ``offramp shadow status`` — print readiness + lag for a process.
* ``offramp shadow report`` — render the divergence dashboard + compliance
  export for a process.
* ``offramp shadow replay-log`` — Compare Mode: parse a debug log, replay
  through the OoE runtime, write findings to the same shadow store.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from offramp.core.config import get_settings
from offramp.core.logging import get_logger
from offramp.engram.client import open_client
from offramp.runtime.ooe.state_machine import OoERuntime
from offramp.runtime.rules.engine import load_artifact
from offramp.validate.compare_mode.log_parser import parse as parse_log
from offramp.validate.compare_mode.replay_harness import ReplayHarness
from offramp.validate.compare_mode.state_reconstructor import StateReconstructor
from offramp.validate.reconcile.lag_monitor import LagMonitor
from offramp.validate.shadow.cdc_event import CDCEvent, ChangeEventHeader, ChangeType, now_utc
from offramp.validate.shadow.compliance import export_compliance_report
from offramp.validate.shadow.dashboard import render_dashboard
from offramp.validate.shadow.data_env import ForkedDataEnv
from offramp.validate.shadow.executor import ShadowExecutor
from offramp.validate.shadow.readiness import ReadinessScorer
from offramp.validate.shadow.store import open_store
from offramp.validate.shadow.synthetic import SyntheticSource

log = get_logger(__name__)


def add_shadow_subparser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("shadow", help="Shadow Mode + Compare Mode controls.")
    sp = p.add_subparsers(dest="shadow_cmd", required=True)

    start = sp.add_parser("start", help="Run the shadow executor.")
    start.add_argument("--process-id", required=True)
    start.add_argument("--artifact", type=Path, required=True, help="Generated tier1/ dir.")
    start.add_argument(
        "--events",
        type=Path,
        help="JSON file of synthetic events (see docs/runbooks/shadow_events.md).",
    )
    start.add_argument(
        "--reset-store",
        action="store_true",
        help="Truncate shadow tables before starting (test-friendly).",
    )
    start.set_defaults(func=_run_start)

    status = sp.add_parser("status", help="Print readiness + lag.")
    status.add_argument("--process-id", required=True)
    status.set_defaults(func=_run_status)

    report = sp.add_parser("report", help="Render dashboard + compliance export.")
    report.add_argument("--process-id", required=True)
    report.add_argument("--out", type=Path, required=True)
    report.set_defaults(func=_run_report)

    replay = sp.add_parser("replay-log", help="Compare Mode: replay a SF debug log.")
    replay.add_argument("--process-id", required=True)
    replay.add_argument("--artifact", type=Path, required=True)
    replay.add_argument("--log-file", type=Path, required=True)
    replay.set_defaults(func=_run_replay)


def _run_start(args: argparse.Namespace) -> int:
    return asyncio.run(_async_start(args))


def _run_status(args: argparse.Namespace) -> int:
    return asyncio.run(_async_status(args))


def _run_report(args: argparse.Namespace) -> int:
    return asyncio.run(_async_report(args))


def _run_replay(args: argparse.Namespace) -> int:
    return asyncio.run(_async_replay(args))


async def _async_start(args: argparse.Namespace) -> int:
    settings = get_settings()
    artifact_dir: Path = args.artifact
    if not artifact_dir.is_dir():  # noqa: ASYNC240
        log.error("shadow.start.artifact_missing", path=str(artifact_dir))
        return 1
    init = artifact_dir / "__init__.py"
    if not init.is_file():
        log.error("shadow.start.no_init", path=str(init))
        return 1

    rules_engine = load_artifact(init)
    runtime = OoERuntime(rules=rules_engine)

    async with open_store(settings.infra.postgres_shadow_dsn) as store, open_client() as engram:
        if args.reset_store:
            await store.reset()

        # Synthesize events from the JSON file (real Pub/Sub mode lands when
        # we have org credentials wired through the MCP gateway).
        source = SyntheticSource()
        if args.events is not None and args.events.is_file():
            payload: list[dict[str, Any]] = json.loads(args.events.read_text())
            for entry in payload:
                source.register_entity(
                    entity_name=entry["entity_name"],
                    fields=entry.get("schema_fields", {}),
                )
                for ev_spec in entry.get("events", []):
                    op = ev_spec["op"]
                    if op == "create":
                        source.add_create(
                            entry["entity_name"], ev_spec["record_id"], ev_spec["fields"]
                        )
                    elif op == "update":
                        source.add_update(
                            entry["entity_name"], ev_spec["record_id"], ev_spec["fields"]
                        )
                    elif op == "delete":
                        source.add_delete(entry["entity_name"], ev_spec["record_id"])
                    elif op == "gap":
                        source.add_gap(entry["entity_name"], ev_spec["record_id"])

        async def _no_prod_read(_sobject: str, _record_id: str) -> dict[str, Any] | None:
            return None

        def _data_env_factory() -> ForkedDataEnv:
            return ForkedDataEnv(
                store=store,
                production_read=_no_prod_read,
                process_id=args.process_id,
            )

        executor = ShadowExecutor(
            process_id=args.process_id,
            runtime=runtime,
            store=store,
            engram=engram,
            data_env_factory=_data_env_factory,
        )

        processed = 0
        async for ev in source.stream(topics=[]):
            await executor.execute_event(ev)
            processed += 1

        log.info("shadow.start.done", processed=processed, process=args.process_id)
        # Print a one-line summary the test gate can grep for.
        print(f"shadow start ok: process={args.process_id} processed={processed}")
    return 0


async def _async_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    async with open_store(settings.infra.postgres_shadow_dsn) as store:
        scorer = ReadinessScorer(store=store)
        lag = LagMonitor(store=store)
        score = await scorer.score(args.process_id)
        lag_snap = await lag.snapshot(args.process_id)
    print(
        json.dumps(
            {
                "process_id": args.process_id,
                "score": score.score,
                "total_events": score.total_events,
                "diverged_events": score.diverged_events,
                "cutover_eligible": score.cutover_eligible,
                "reason": score.reason,
                "lag_status": lag_snap.status,
                "lag_hours": lag_snap.lag_hours,
            },
            indent=2,
        )
    )
    return 0


async def _async_report(args: argparse.Namespace) -> int:
    settings = get_settings()
    out_dir: Path = args.out
    # Pre-async filesystem prep — sync mkdir is intentional.
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    async with open_store(settings.infra.postgres_shadow_dsn) as store, open_client() as engram:
        scorer = ReadinessScorer(store=store)
        lag = LagMonitor(store=store)
        await render_dashboard(
            process_id=args.process_id,
            store=store,
            scorer=scorer,
            lag=lag,
            out_path=out_dir / "shadow_dashboard.html",
        )
        result = await export_compliance_report(
            process_id=args.process_id,
            store=store,
            scorer=scorer,
            lag=lag,
            engram=engram,
            out_path=out_dir / "compliance_report.json",
        )
    print(
        f"report written: {result.out_path} ({result.divergences_exported} divergences, "
        f"f44_anchored={result.f44_anchored_count})"
    )
    return 0


async def _async_replay(args: argparse.Namespace) -> int:
    settings = get_settings()
    log_text = args.log_file.read_text(encoding="utf-8")
    transactions, stats = parse_log(log_text)
    log.info("shadow.replay.parsed", transactions=stats.transactions)

    rules_engine = load_artifact(args.artifact / "__init__.py")
    runtime = OoERuntime(rules=rules_engine)

    async with open_store(settings.infra.postgres_shadow_dsn) as store, open_client() as engram:
        reconstructor = StateReconstructor(store=store)
        harness = ReplayHarness(
            runtime=runtime,
            reconstructor=reconstructor,
            store=store,
            engram=engram,
            process_id=args.process_id,
        )
        diverged_total = 0
        for txn in transactions:
            outcomes = await harness.replay(txn)
            diverged_total += sum(1 for o in outcomes if o.diverged)
    print(f"replay-log done: transactions={stats.transactions} diverged={diverged_total}")
    return 0


# Unused import suppression for runtime types referenced only for typing.
_ = (CDCEvent, ChangeEventHeader, ChangeType, now_utc)
