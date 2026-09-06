"""Shared org-connection helpers for CLI subcommands (AD-29).

Three ways to get metadata into the pipeline:

* ``--fixture DIR`` / ``--source-dir DIR`` — read an sf-retrieve-shaped tree
* ``--org ALIAS`` — REST/Tooling through the MCP gateway (JWT settings from env),
  plus a Metadata API retrieve for the types Tooling cannot read in full
* ``--org ALIAS --via mdapi`` — Metadata API retrieve for everything
* ``--org ALIAS --via sf-cli`` — ``sf project retrieve start`` into a temp dir;
  the Tooling supplement (CMT rows, dependencies, cron, schema, data profile)
  still comes from REST
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from offramp.core.config import get_settings
from offramp.core.logging import get_logger
from offramp.core.models import CategoryName
from offramp.engram.client import EngramClient
from offramp.extract.orchestrator import ExtractOrchestrator, ExtractRunResult, ToolingSupplement
from offramp.extract.pull.fixture import FixturePullClient, SourceDirPullClient

log = get_logger(__name__)


def add_source_args(p: argparse.ArgumentParser) -> None:
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture", type=Path, help="Path to a fixture org dump (tests/smoke).")
    src.add_argument(
        "--source-dir", type=Path, help="Customer-supplied SFDX project or sf retrieve output."
    )
    src.add_argument("--org", help="Salesforce org alias; credentials from SF_* env (JWT bearer).")
    p.add_argument(
        "--ignore-api-budget",
        action="store_true",
        help="Scan even when /limits says fewer API requests remain than a scan needs.",
    )
    p.add_argument(
        "--via",
        choices=["rest", "mdapi", "sf-cli"],
        default="rest",
        help=(
            "With --org: REST/Tooling plus Metadata API for what Tooling cannot read (default), "
            "Metadata API for everything, or sf CLI retrieve."
        ),
    )
    p.add_argument("--org-alias", default=None, help="Override the org label used in outputs.")
    p.add_argument(
        "--auth",
        choices=["jwt", "sf-cli"],
        default=None,
        help="With --org: JWT bearer via Connected App (default, SF_AUTH_MODE) or the sf CLI's session.",
    )
    p.add_argument(
        "--no-schema",
        action="store_true",
        help="Skip describe-based schema extraction (faster; fewer edges).",
    )
    p.add_argument(
        "--no-dependency-api",
        action="store_true",
        help="Skip MetadataComponentDependency cross-check.",
    )
    p.add_argument(
        "--library",
        type=Path,
        default=None,
        help="Also ingest this scan into the process library at DIR (see `offramp kg`).",
    )
    p.add_argument(
        "--no-data-profile",
        action="store_true",
        help="Skip record counts and field fill rates.",
    )


@dataclass
class ConnectedSource:
    org_alias: str
    orchestrator: ExtractOrchestrator
    close: Any  # async callable


async def connect(args: argparse.Namespace, engram: EngramClient) -> ConnectedSource | None:
    """Build the orchestrator for whichever source the flags selected; ``None`` on user error."""
    if args.fixture is not None or args.source_dir is not None:
        root: Path = args.fixture if args.fixture is not None else args.source_dir
        if not root.is_dir():  # noqa: ASYNC240 — CLI guard before async work
            log.error("cli.source_not_found", path=str(root))
            return None
        alias = args.org_alias or root.name
        dir_client = (
            FixturePullClient(root) if args.fixture is not None else SourceDirPullClient(root)
        )
        orch = ExtractOrchestrator(
            org_alias=alias, client=dir_client, engram=engram, fixture_root=root
        )

        async def _noop() -> None:
            return None

        return ConnectedSource(alias, orch, _noop)

    # ---- real org ----
    settings = get_settings()
    alias = args.org_alias or args.org
    from offramp.mcp.server import MCPGateway
    from offramp.mcp.sf_backend import SimpleSalesforceBackend

    updates: dict[str, Any] = {"org_alias": args.org}
    if args.auth:
        updates["auth_mode"] = args.auth
    sf_settings = settings.salesforce.model_copy(update=updates)
    # CLI scans run unmetered; the hosted service attaches a QuotaAllocator here.
    backend = SimpleSalesforceBackend(settings=sf_settings, process_id="xray", quota=None)
    gateway = MCPGateway(backend=backend, engram=engram)

    from offramp.extract.pull.tooling_api import ToolingApiPullClient, api_budget

    # Org-wide daily request quota (pitfall 4): refuse a scan that cannot finish.
    try:
        remaining, ok = api_budget(await gateway.sf_restful("limits"))
    except Exception as exc:
        log.warning("cli.api_limits_unavailable", org=args.org, error=str(exc)[:200])
        remaining, ok = None, True
    if remaining is not None:
        log.info("cli.api_budget", org=args.org, daily_requests_remaining=remaining)
    if not ok and not getattr(args, "ignore_api_budget", False):
        log.error(
            "cli.api_budget_insufficient",
            org=args.org,
            daily_requests_remaining=remaining,
            hint="wait for the rolling 24h window or pass --ignore-api-budget",
        )
        await backend.aclose()
        return None

    via = getattr(args, "via", "rest")
    tooling = ToolingApiPullClient(
        gateway=gateway,
        org_alias=alias,
        api_version=sf_settings.api_version,
        # In the composite path the Metadata API retrieves these in full.
        skip_categories=(
            {
                CategoryName.PAGE_LAYOUT,
                CategoryName.FLEXIPAGE,
                CategoryName.PERMISSION_SET,
                CategoryName.PROFILE,
                CategoryName.REPORT,
                CategoryName.CUSTOM_TAB,
                CategoryName.CUSTOM_APPLICATION,
                CategoryName.PATH_ASSISTANT,
            }
            if via == "rest"
            else set()
        ),
    )

    supplement = ToolingSupplement()
    try:
        supplement.cmt_records = await tooling.cmt_records()
        if not args.no_dependency_api:
            supplement.dependency_rows = await tooling.dependency_rows()
        supplement.cron_rows = await tooling.cron_rows()
        if not args.no_schema:
            supplement.schema = await tooling.schema()
        if not args.no_data_profile and supplement.schema is not None:
            from offramp.extract.data_profile import profile_from_gateway

            supplement.data_profile = await profile_from_gateway(
                gateway, supplement.schema, org_alias=alias
            )
    except Exception as exc:
        log.error("cli.org_supplement_failed", org=args.org, error=str(exc))
        await backend.aclose()
        return None

    import tempfile

    from offramp.extract.pull.mdapi import PARTIAL_TYPES, CompositePullClient, MetadataApiPullClient

    workdir = Path(tempfile.mkdtemp(prefix=f"offramp-{alias}-"))
    client: Any
    if args.via == "sf-cli":
        from offramp.extract.pull.sf_cli import SfCliPullClient

        client = SfCliPullClient(
            org_alias=args.org, output_dir=workdir, api_version=sf_settings.api_version
        )
    elif args.via == "mdapi":
        client = MetadataApiPullClient(
            gateway=gateway, org_alias=alias, workdir=workdir, api_version=sf_settings.api_version
        )
    else:
        # REST for the bulk; Metadata API only for the types Tooling cannot read in full.
        fill_in = MetadataApiPullClient(
            gateway=gateway,
            org_alias=alias,
            workdir=workdir,
            api_version=sf_settings.api_version,
            types=PARTIAL_TYPES,
        )
        client = CompositePullClient(tooling, fill_in)
    orch = ExtractOrchestrator(org_alias=alias, client=client, engram=engram, supplement=supplement)

    return ConnectedSource(alias, orch, backend.aclose)


def write_result(result: ExtractRunResult, out: Path, *, library: Path | None = None) -> None:
    result.write(out)
    if library is not None:
        from offramp.cli.kg import ingest_extract_dir
        from offramp.knowledge.store import KnowledgeStore

        rec, _ = ingest_extract_dir(KnowledgeStore(library), out, org_alias=result.org_alias)
        log.info(
            "cli.library_ingested",
            library=str(library),
            scan=rec.scan_id,
            new=len(rec.new_process_ids),
        )
