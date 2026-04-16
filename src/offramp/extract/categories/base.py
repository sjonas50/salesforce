"""Per-category extractor base class and registry.

Each of the 21 Salesforce automation categories has an extractor that turns
:class:`ReconciledRecord` instances into canonical
:class:`offramp.core.models.Component` records with content hashes.

Implementations register themselves via the :func:`register` decorator so
the orchestrator can dispatch by ``CategoryName`` without an explicit table.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, ClassVar

from offramp.core.hashing import content_hash
from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component, Provenance
from offramp.extract.pull.reconciler import ReconciledRecord

log = get_logger(__name__)


@dataclass
class ExtractionFailure:
    """One unrecoverable extraction error, surfaced in the coverage report."""

    api_name: str
    category: CategoryName
    reason: str


class CategoryExtractor(abc.ABC):
    """Base class for per-category extractors."""

    category: ClassVar[CategoryName]

    @abc.abstractmethod
    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        """Parse the source-specific payload into a category-canonical dict.

        Implementations may raise :class:`ValueError` to signal a parse failure
        — the orchestrator will record an :class:`ExtractionFailure` and skip
        the component without aborting the whole run.
        """

    def to_component(
        self,
        record: ReconciledRecord,
        org_alias: str,
        provenance: Provenance,
    ) -> Component:
        parsed = self.parse_payload(record)
        return Component(
            org_alias=org_alias,
            category=self.category,
            name=record.api_name,
            api_name=record.api_name,
            namespace=record.namespace,
            raw=parsed,
            content_hash=content_hash(parsed),
            provenance=provenance,
        )


_REGISTRY: dict[CategoryName, type[CategoryExtractor]] = {}


def register(cls: type[CategoryExtractor]) -> type[CategoryExtractor]:
    """Class decorator: register an extractor class against its ``category``."""
    if not hasattr(cls, "category"):
        raise TypeError(f"{cls.__name__} missing required ClassVar 'category'")
    _REGISTRY[cls.category] = cls
    return cls


def get_extractor(category: CategoryName) -> CategoryExtractor:
    cls = _REGISTRY.get(category)
    if cls is None:
        raise KeyError(f"No extractor registered for {category}")
    return cls()


def registered_categories() -> set[CategoryName]:
    return set(_REGISTRY.keys())
