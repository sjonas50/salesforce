"""Salto pull client (real-org backend, Phase 1.1).

Wraps ``salto fetch`` and parses the resulting NaCl tree. Phase 0/1 ships the
class skeleton with a clear NotImplementedError so callers see the wiring
point. Real implementation lands when a customer org is connected.
"""

from __future__ import annotations

from collections.abc import Iterable

from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord


class SaltoPullClient:
    """Wraps ``salto fetch`` against a customer org."""

    source_name = "salto"

    def __init__(self, *, workspace_dir: str, salto_binary: str = "salto") -> None:
        self.workspace_dir = workspace_dir
        self.salto_binary = salto_binary
        self.source_version = "TBD"
        self.api_version = "66.0"

    async def list_categories(self) -> set[CategoryName]:
        raise NotImplementedError(
            "SaltoPullClient is wired for Phase 1.1 but requires a real Salto "
            "workspace + customer org credentials. Use FixturePullClient until "
            "scratch-org provisioning is complete."
        )

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        raise NotImplementedError(
            "SaltoPullClient.pull lands once a real org is connected — see Phase 1.1."
        )
