"""Pull-layer contracts.

Three real Phase 1 sources (Salto, sf CLI, Tooling API) plus a
fixture-backed client used by tests. All return ``RawMetadataRecord``
instances; the per-category extractors consume those.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from offramp.core.models import CategoryName


@dataclass(frozen=True)
class RawMetadataRecord:
    """One untyped metadata record straight from a pull source.

    The :attr:`payload` shape varies by source — Salto emits NaCl-as-dict,
    sf CLI emits XML-as-dict, Tooling API emits SObject rows. The reconciler
    is responsible for collapsing them into a single canonical Component.
    """

    source: str  # 'salto' | 'sf_cli' | 'tooling_api' | 'fixture'
    source_version: str
    api_version: str
    category: CategoryName
    api_name: str
    namespace: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    pulled_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PullClient(Protocol):
    """Source-agnostic metadata pull contract.

    Implementations stream records lazily so a 5,000-component org doesn't
    materialize the whole corpus in memory.
    """

    source_name: str
    source_version: str
    api_version: str

    async def list_categories(self) -> set[CategoryName]:
        """Categories this client can produce records for."""
        ...

    async def pull(
        self, *, categories: Iterable[CategoryName] | None = None
    ) -> Iterable[RawMetadataRecord]:
        """Yield raw records for the requested categories (or all if ``None``)."""
        ...


@dataclass(frozen=True)
class PullDisagreement:
    """Logged when two sources produce inconsistent records for the same artifact.

    The reconciler still applies its precedence rule, but the disagreement is
    surfaced in the coverage report as a data-quality observation.
    """

    api_name: str
    category: CategoryName
    sources_in_disagreement: tuple[str, ...]
    field_path: str
    values_by_source: dict[str, Any]
