"""sf CLI pull client (Phase 1.2).

Shells out to ``sf project retrieve start`` with a dynamically-generated
``package.xml`` covering all 21 categories. Phase 0/1 stubs the surface so
the orchestrator can plug it in once a real org is connected.
"""

from __future__ import annotations

from collections.abc import Iterable

from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord


class SfCliPullClient:
    """Wraps ``sf project retrieve start``."""

    source_name = "sf_cli"

    def __init__(self, *, org_alias: str, output_dir: str, sf_binary: str = "sf") -> None:
        self.org_alias = org_alias
        self.output_dir = output_dir
        self.sf_binary = sf_binary
        self.source_version = "TBD"
        self.api_version = "66.0"

    async def list_categories(self) -> set[CategoryName]:
        raise NotImplementedError(
            "SfCliPullClient is wired for Phase 1.2 but requires sf CLI + org auth."
        )

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        raise NotImplementedError(
            "SfCliPullClient.pull lands once sf CLI is configured — see Phase 1.2."
        )
