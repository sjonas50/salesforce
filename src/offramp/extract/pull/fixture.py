"""Fixture-backed pull client: a thin wrapper over :class:`SourceTree` (C19).

Used by tests, ``make smoke``, and ``offramp extract --fixture``. Also the
client behind ``--source-dir`` for customer-supplied SFDX projects — the
reader is identical; only the ``source`` label differs.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from offramp.core.models import CategoryName
from offramp.extract.pull.base import RawMetadataRecord
from offramp.extract.pull.source_tree import SourceTree


class FixturePullClient:
    """Reads an org dump from disk."""

    source_name = "fixture"

    def __init__(
        self,
        root: Path,
        *,
        version: str = "0.1.0",
        api_version: str = "66.0",
        source_name: str | None = None,
    ) -> None:
        self.root = root
        self.tree = SourceTree(root)
        self.source_version = version
        self.api_version = api_version
        if source_name:
            self.source_name = source_name

    async def list_categories(self) -> set[CategoryName]:
        return self.tree.present_categories()

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        wanted = set(categories) if categories else None
        return self.tree.records(
            source=self.source_name,
            source_version=self.source_version,
            api_version=self.api_version,
            categories=wanted,
        )


class SourceDirPullClient(FixturePullClient):
    """Customer-supplied SFDX project directory (``offramp extract --source-dir``)."""

    source_name = "sfdx_project"
