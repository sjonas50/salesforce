"""Detect managed-package dependencies in the component corpus.

Heuristic — Phase 1 doesn't surface package metadata cleanly yet, so this
classifies based on namespace prefixes (the most reliable Salesforce
signal) plus name patterns.

Each detected dependency is either ``auto-adaptable`` (we generate an MCP
tool definition for it) or ``hand_tuned_required`` (route to the
:mod:`offramp.generate.adapters.hand_tuned` library).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from offramp.core.models import Component

# Namespaces of the top-5 packages the architecture (§9.8) flags for
# hand-tuned adapters. Mapped to the canonical adapter module name.
HAND_TUNED_NAMESPACES: dict[str, str] = {
    "sbqq": "cpq",
    "apxt": "conga",  # Conga Composer / Composer Pro
    "dsfs": "docusign",
    "et4ae5": "marketing_cloud_connect",
    "pi": "pardot",
}


@dataclass(frozen=True)
class PackageDependency:
    """One detected ISV / managed-package dependency."""

    namespace: str
    package_name: str
    adapter_kind: Literal["hand_tuned", "auto"]
    contributing_components: tuple[str, ...]


def detect(components: list[Component]) -> list[PackageDependency]:
    """Group components by namespace and classify each group's adapter strategy."""
    by_ns: dict[str, list[Component]] = {}
    for c in components:
        if c.namespace:
            by_ns.setdefault(c.namespace.lower(), []).append(c)

    deps: list[PackageDependency] = []
    for ns, members in by_ns.items():
        canonical = HAND_TUNED_NAMESPACES.get(ns)
        if canonical:
            deps.append(
                PackageDependency(
                    namespace=ns,
                    package_name=canonical,
                    adapter_kind="hand_tuned",
                    contributing_components=tuple(c.name for c in members),
                )
            )
        else:
            deps.append(
                PackageDependency(
                    namespace=ns,
                    package_name=ns,
                    adapter_kind="auto",
                    contributing_components=tuple(c.name for c in members),
                )
            )
    return deps
