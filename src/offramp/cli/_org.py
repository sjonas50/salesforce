"""Shared org-connection helpers for CLI subcommands (AD-29).

Three ways to get metadata into the pipeline:

* ``--fixture DIR`` / ``--source-dir DIR`` — read an sf-retrieve-shaped tree
* ``--org ALIAS`` — REST/Tooling through the MCP gateway (JWT settings from env)
* ``--org ALIAS --via sf-cli`` — ``sf project retrieve start`` into a temp dir,
  then read the tree; Tooling supplement still comes from REST
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from offramp.core.config import get_settings
from offramp.core.logging import get_logger
from offramp.engram.client import EngramClient
from offramp.extract.orchestrator import ExtractOrchestrator, ExtractRunResult, ToolingSupplement
from offramp.extract.pull.fixture import FixturePullClient, SourceDirPullClient
from offramp.extract.pull.source_tree import SourceTree

log = get_logger(__name__)


def add_source_args(p: argparse.ArgumentParser) -> None:
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture", type=Path, help="Path to a fixture org dump (tests/smoke).")
    src.add_argument(
        "--source-dir", type=Path, help="Customer-supplied SFDX project or sf retrieve output."
    )
    src.add_argument("--org", help="Salesforce org alias; credentials from SF_* env (JWT bearer).")
    p.add_argument(
        "--via",
        choices=["rest", "sf-cli"],
        default="rest",
        help="With --org: REST/Tooling (default) or sf CLI retrieve.",
    )
    p.add_argument("--org-alias", default=None, help="Override the org label used in outputs.")
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
        client = FixturePullClient(root) if args.fixture is not None else SourceDirPullClient(root)
        orch = ExtractOrchestrator(org_alias=alias, client=client, engram=engram, fixture_root=root)

        async def _noop() -> None:
            return None

        return ConnectedSource(alias, orch, _noop)

    # ---- real org ----
    settings = get_settings()
    alias = args.org_alias or args.org
    from offramp.mcp.quota import QuotaAllocator
    from offramp.mcp.server import MCPGateway
    from offramp.mcp.sf_backend import SimpleSalesforceBackend

    sf_settings = settings.salesforce.model_copy(update={"org_alias": args.org})
    backend = SimpleSalesforceBackend(settings=sf_settings, process_id="xray", quota=None)
    gateway = MCPGateway(backend=backend, engram=engram)
    _ = QuotaAllocator  # quota is wired in the hosted service; CLI scans run unmetered

    from offramp.extract.pull.tooling_api import ToolingApiPullClient

    tooling = ToolingApiPullClient(
        gateway=gateway, org_alias=alias, api_version=sf_settings.api_version
    )

    supplement = ToolingSupplement()
    try:
        supplement.cmt_records = await tooling.cmt_records()
        if not args.no_dependency_api:
            supplement.dependency_rows = await tooling.dependency_rows()
        supplement.cron_rows = await tooling.cron_rows()
        if not args.no_schema:
            supplement.schema = await tooling.schema()
    except Exception as exc:
        log.error("cli.org_supplement_failed", org=args.org, error=str(exc))
        await backend.aclose()
        return None

    if args.via == "sf-cli":
        import tempfile

        from offramp.extract.pull.sf_cli import SfCliPullClient

        out = Path(tempfile.mkdtemp(prefix=f"offramp-{alias}-"))
        cli_client = SfCliPullClient(
            org_alias=args.org, output_dir=out, api_version=sf_settings.api_version
        )
        # Source-tree schema is richer for custom objects; merge with describe.
        if supplement.schema is not None:
            from offramp.extract.schema import from_source_tree, merge

            tree_schema = from_source_tree(SourceTree(out), org_alias=alias)
            supplement.schema = merge(tree_schema, supplement.schema)
        orch = ExtractOrchestrator(
            org_alias=alias, client=cli_client, engram=engram, supplement=supplement
        )
    else:
        orch = ExtractOrchestrator(
            org_alias=alias, client=tooling, engram=engram, supplement=supplement
        )

    return ConnectedSource(alias, orch, backend.aclose)


def write_result(result: ExtractRunResult, out: Path) -> None:
    result.write(out)
