"""Tooling API pull client (Phase 1.3).

Issues parallel queries against the Salesforce Tooling API for metadata not
exposed via the Metadata API: ``MetadataComponentDependency``,
``FlowVersionView``, ``CronTrigger``, and Custom Metadata Type records used
by the dynamic dispatch resolver (C2).

Phase 0/1 ships the skeleton; the real client is built on simple-salesforce's
Tooling API support and lands when scratch org auth is wired.
"""

from __future__ import annotations

from collections.abc import Iterable

from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord


class ToolingApiPullClient:
    """Salesforce Tooling API client."""

    source_name = "tooling_api"

    def __init__(self, *, mcp_gateway: object) -> None:
        # Real impl will use the MCP gateway so all API calls flow through
        # one quota-managed entry point. Typed as ``object`` to avoid a
        # circular import; concrete type is offramp.mcp.server.MCPGateway.
        self.mcp_gateway = mcp_gateway
        self.source_version = "TBD"
        self.api_version = "66.0"

    async def list_categories(self) -> set[CategoryName]:
        # Tooling API surfaces a subset: dependencies + Flow versions + scheduled jobs + CMT.
        # Returning the empty set is the honest answer until impl lands.
        raise NotImplementedError("ToolingApiPullClient is wired for Phase 1.3.")

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        raise NotImplementedError(
            "ToolingApiPullClient.pull lands once MCP gateway has real SF backend."
        )
