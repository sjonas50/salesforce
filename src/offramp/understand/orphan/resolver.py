"""6-channel orphan resolver (architecture §C6).

An "orphan" is an Apex class with no resolved caller in the extracted corpus.
Naively-treated as dead code, orphans are usually externally-invoked entry
points that we missed via static analysis. The resolver tries six channels in
descending confidence order and promotes the orphan to a classified entry
point on the first match.

Channels (highest confidence first):

1. **runtime_log** — customer-provided EventLogFile shows the class invoked
2. **lwc_import** — an LWC bundle imports the class via @salesforce/apex/
3. **named_credential** — a Named Credential URL pattern points at the class
4. **connected_app_scope** — a Connected App grants OAuth access to the class
5. **cron_trigger** — System.schedule() registered the class at runtime
6. **integration_doc** — vendor-supplied integration docs (MuleSoft, etc.)

For Phase 2 the LWC channel is the only one we have real data for (from
Phase 1's extractor). The other channels accept a typed input but no-op when
the data isn't provided — the architecture keeps the interface complete so
adding the data later is a localized change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component

log = get_logger(__name__)


@dataclass(frozen=True)
class OrphanResolution:
    """One resolved orphan with the channel + confidence that found it."""

    component_id: str
    apex_class_name: str
    channel: str  # one of the six channel names
    confidence: float
    evidence: str  # human-readable detail for the report


@dataclass
class ResolutionInputs:
    """All optional channel-data the resolver can consume.

    Each field has a sensible default so callers can populate only what they
    have. Missing inputs simply don't contribute resolutions.
    """

    components: list[Component]
    runtime_log_class_invocations: set[str] = field(default_factory=set)
    named_credential_endpoints: dict[str, str] = field(default_factory=dict)  # class -> endpoint
    connected_app_scopes: dict[str, list[str]] = field(default_factory=dict)  # app -> classes
    cron_trigger_classes: set[str] = field(default_factory=set)
    integration_doc_classes: dict[str, str] = field(default_factory=dict)  # class -> doc src


@dataclass
class ResolutionReport:
    """Aggregate output: per-orphan resolution (or marked dead)."""

    resolved: list[OrphanResolution] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # apex class names

    @property
    def total_orphans(self) -> int:
        return len(self.resolved) + len(self.unresolved)

    @property
    def resolved_ratio(self) -> float:
        if self.total_orphans == 0:
            return 1.0
        return len(self.resolved) / self.total_orphans


def _called_apex_classes(components: list[Component]) -> set[str]:
    """Collect Apex class names referenced from OTHER APEX in the corpus.

    "Orphan" means "no Apex caller" — the six resolution channels then look
    for non-Apex entry points (LWC, Named Credentials, runtime logs, etc.).
    Counting LWC imports here would prevent the LWC channel from ever firing.

    Sources of Apex-to-Apex references:
    * Apex trigger bodies (string-search for class name)
    * (Phase 3) Apex AST callee resolution from summit-ast
    """
    referenced: set[str] = set()
    apex_class_names = {
        c.api_name for c in components if c.category is CategoryName.APEX_CLASS and c.api_name
    }
    for c in components:
        if c.category is CategoryName.APEX_TRIGGER:
            body = c.raw.get("body", "") if isinstance(c.raw, dict) else ""
            if isinstance(body, str) and body:
                for name in apex_class_names:
                    if name and name in body:
                        referenced.add(name)
    return referenced


def resolve_orphans(inputs: ResolutionInputs) -> ResolutionReport:
    """Run all six channels and return a per-orphan resolution map."""
    apex_classes = [
        c for c in inputs.components if c.category is CategoryName.APEX_CLASS and c.api_name
    ]
    referenced = _called_apex_classes(inputs.components)
    by_name = {c.api_name: c for c in apex_classes if c.api_name}

    orphans = [c for c in apex_classes if c.api_name and c.api_name not in referenced]

    report = ResolutionReport()
    for orphan in orphans:
        name = orphan.api_name or ""
        # Channel order = descending confidence. First match wins.
        if name in inputs.runtime_log_class_invocations:
            report.resolved.append(
                OrphanResolution(
                    component_id=str(orphan.id),
                    apex_class_name=name,
                    channel="runtime_log",
                    confidence=0.99,
                    evidence="Observed in EventLogFile invocation log.",
                )
            )
            continue
        # LWC was already collapsed into ``referenced``, but keep the channel
        # explicit so the report reflects the discovery path.
        lwc_match = _lwc_imports_class(inputs.components, name)
        if lwc_match:
            report.resolved.append(
                OrphanResolution(
                    component_id=str(orphan.id),
                    apex_class_name=name,
                    channel="lwc_import",
                    confidence=0.9,
                    evidence=f"Imported by LWC bundle {lwc_match}",
                )
            )
            continue
        if name in inputs.named_credential_endpoints:
            report.resolved.append(
                OrphanResolution(
                    component_id=str(orphan.id),
                    apex_class_name=name,
                    channel="named_credential",
                    confidence=0.8,
                    evidence=f"Named Credential endpoint: {inputs.named_credential_endpoints[name]}",
                )
            )
            continue
        ca_apps = [a for a, classes in inputs.connected_app_scopes.items() if name in classes]
        if ca_apps:
            report.resolved.append(
                OrphanResolution(
                    component_id=str(orphan.id),
                    apex_class_name=name,
                    channel="connected_app_scope",
                    confidence=0.7,
                    evidence=f"Connected App(s) granting access: {', '.join(ca_apps)}",
                )
            )
            continue
        if name in inputs.cron_trigger_classes:
            report.resolved.append(
                OrphanResolution(
                    component_id=str(orphan.id),
                    apex_class_name=name,
                    channel="cron_trigger",
                    confidence=0.85,
                    evidence="Registered via System.schedule() (CronTrigger row).",
                )
            )
            continue
        if name in inputs.integration_doc_classes:
            report.resolved.append(
                OrphanResolution(
                    component_id=str(orphan.id),
                    apex_class_name=name,
                    channel="integration_doc",
                    confidence=0.75,
                    evidence=f"Documented in {inputs.integration_doc_classes[name]}",
                )
            )
            continue
        report.unresolved.append(name)

    log.info(
        "understand.orphan.resolved",
        total=report.total_orphans,
        resolved=len(report.resolved),
        unresolved=len(report.unresolved),
    )
    # Acknowledge the unused mapping to keep mypy happy without leaking it.
    _ = by_name
    return report


def _lwc_imports_class(components: list[Component], class_name: str) -> str | None:
    for c in components:
        if c.category is not CategoryName.LWC_BUNDLE:
            continue
        imports = c.raw.get("apex_imports", []) if isinstance(c.raw, dict) else []
        for imp in imports:
            if isinstance(imp, str) and imp.split(".", 1)[0] == class_name:
                return c.name
    return None
