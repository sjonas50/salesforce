"""6-channel orphan resolver (architecture §C6).

An "orphan" is an Apex class with no caller in the extracted corpus. Naively
treated as dead code, orphans are usually externally-invoked entry points
that static analysis cannot see. The resolver tries six channels in
descending confidence order and promotes the orphan to a classified entry
point on the first match.

Channels (highest confidence first):

1. **runtime_log** — customer-provided EventLogFile shows the class invoked
2. **lwc_import** — an LWC bundle imports the class via ``@salesforce/apex/``
3. **cron_trigger** — a CronTrigger row (System.schedule) names the class
4. **named_credential** — a Named Credential / External Service targets the class
5. **connected_app_scope** — a Connected App grants OAuth access to the class
6. **integration_doc** — vendor-supplied integration docs (MuleSoft, etc.)

Before the channels run, a class is *not* an orphan if the dependency graph
shows an Apex caller: a static call, ``new``, ``Type.forName``, an async
target, a CMT dispatch row, a Flow action call, or a trigger reference. The
class's own entry-point annotations (``@AuraEnabled``, ``@InvocableMethod``,
``@RestResource``, Batchable/Schedulable/Queueable) are reported as the
``entry_point`` channel so admins see *why* the class exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component, DependencyKind
from offramp.understand.dependencies import DependencyGraph

log = get_logger(__name__)

_ENTRY_POINT_LABELS = {
    "aura_enabled": "Exposed to Lightning (@AuraEnabled)",
    "invocable": "Exposed to Flow (@InvocableMethod)",
    "rest_resource": "REST endpoint (@RestResource)",
    "soap_webservice": "SOAP web service (global webservice)",
    "batchable": "Batch job (Database.Batchable)",
    "schedulable": "Scheduled job (Schedulable)",
    "queueable": "Queueable job",
    "future": "@future method",
    "inbound_email": "Inbound email handler",
    "trigger_handler": "Trigger handler interface",
    "flow_plugin": "Flow plugin (Process.Plugin)",
    "auth_handler": "Auth registration handler",
    "site_rewriter": "Site URL rewriter",
    "test": "Test class",
}


@dataclass(frozen=True)
class OrphanResolution:
    """One resolved orphan with the channel + confidence that found it."""

    component_id: str
    apex_class_name: str
    channel: str
    confidence: float
    evidence: str


@dataclass
class ResolutionInputs:
    """All optional channel-data the resolver can consume."""

    components: list[Component]
    graph: DependencyGraph | None = None
    runtime_log_class_invocations: set[str] = field(default_factory=set)
    named_credential_endpoints: dict[str, str] = field(default_factory=dict)
    connected_app_scopes: dict[str, list[str]] = field(default_factory=dict)
    cron_trigger_classes: set[str] = field(default_factory=set)
    integration_doc_classes: dict[str, str] = field(default_factory=dict)


@dataclass
class ResolutionReport:
    """Aggregate output: per-orphan resolution (or marked dead)."""

    resolved: list[OrphanResolution] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    referenced: int = 0  # classes with an Apex/Flow/trigger caller (never orphans)

    @property
    def total_orphans(self) -> int:
        return len(self.resolved) + len(self.unresolved)

    @property
    def resolved_ratio(self) -> float:
        if self.total_orphans == 0:
            return 1.0
        return len(self.resolved) / self.total_orphans


def _called_apex_classes(components: list[Component], graph: DependencyGraph | None) -> set[str]:
    """Class names with a caller inside the corpus."""
    referenced: set[str] = set()
    if graph is not None:
        for c in components:
            if c.category is not CategoryName.APEX_CLASS:
                continue
            for e in graph.inbound(str(c.id)):
                if e.kind in {
                    DependencyKind.CALLS,
                    DependencyKind.DISPATCHES,
                    DependencyKind.TRIGGERS,
                }:
                    src = graph.node(str(e.source_id))
                    if (
                        src is not None
                        and src.category not in {"lwc_bundle", "CronTrigger"}
                        and not src.meta.get("is_test")
                    ):
                        referenced.add(c.api_name or c.name)
                        break
        return referenced
    # Graph-less fallback: analyzer references on Apex components.
    names = {c.api_name for c in components if c.category is CategoryName.APEX_CLASS and c.api_name}
    for c in components:
        if c.category not in {CategoryName.APEX_CLASS, CategoryName.APEX_TRIGGER}:
            continue
        refs = c.raw.get("references", {}) if isinstance(c.raw, dict) else {}
        for key in ("apex_classes", "async_targets", "type_forname"):
            for cls in refs.get(key, []):
                if cls in names:
                    referenced.add(cls)
    return referenced


def resolve_orphans(inputs: ResolutionInputs) -> ResolutionReport:
    """Run all channels and return a per-orphan resolution map."""
    apex_classes = [
        c for c in inputs.components if c.category is CategoryName.APEX_CLASS and c.api_name
    ]
    referenced = _called_apex_classes(inputs.components, inputs.graph)
    report = ResolutionReport(referenced=len(referenced))
    orphans = [c for c in apex_classes if c.api_name and c.api_name not in referenced]

    for orphan in orphans:
        name = orphan.api_name or ""
        raw = orphan.raw if isinstance(orphan.raw, dict) else {}
        entry_points = [e for e in raw.get("entry_points", []) if e != "test"]
        if name in inputs.runtime_log_class_invocations:
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id),
                    name,
                    "runtime_log",
                    0.99,
                    "Observed in EventLogFile invocation log.",
                )
            )
            continue
        lwc_match = _lwc_imports_class(inputs.components, name)
        if lwc_match:
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id), name, "lwc_import", 0.9, f"Imported by LWC bundle {lwc_match}"
                )
            )
            continue
        if name in inputs.cron_trigger_classes or _cron_in_graph(inputs.graph, orphan):
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id),
                    name,
                    "cron_trigger",
                    0.85,
                    "Registered via System.schedule() (CronTrigger row).",
                )
            )
            continue
        if name in inputs.named_credential_endpoints:
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id),
                    name,
                    "named_credential",
                    0.8,
                    f"Named Credential endpoint: {inputs.named_credential_endpoints[name]}",
                )
            )
            continue
        ca_apps = [a for a, classes in inputs.connected_app_scopes.items() if name in classes]
        if ca_apps:
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id),
                    name,
                    "connected_app_scope",
                    0.7,
                    f"Connected App(s) granting access: {', '.join(ca_apps)}",
                )
            )
            continue
        if name in inputs.integration_doc_classes:
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id),
                    name,
                    "integration_doc",
                    0.75,
                    f"Documented in {inputs.integration_doc_classes[name]}",
                )
            )
            continue
        if entry_points:
            labels = ", ".join(_ENTRY_POINT_LABELS.get(e, e) for e in entry_points)
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id),
                    name,
                    "entry_point",
                    0.6,
                    f"Declares an external entry point: {labels}. Invocation not observed.",
                )
            )
            continue
        analysis = raw.get("analysis", {}) if isinstance(raw.get("analysis"), dict) else {}
        if analysis.get("is_test") or "test" in raw.get("entry_points", []):
            report.resolved.append(
                OrphanResolution(str(orphan.id), name, "test", 0.5, "Test class.")
            )
            continue
        if analysis.get("kind") == "interface":
            report.resolved.append(
                OrphanResolution(
                    str(orphan.id),
                    name,
                    "interface",
                    0.5,
                    "Interface; implemented by other classes.",
                )
            )
            continue
        report.unresolved.append(name)

    log.info(
        "understand.orphan.resolved",
        total=report.total_orphans,
        resolved=len(report.resolved),
        unresolved=len(report.unresolved),
        referenced=report.referenced,
    )
    return report


def _cron_in_graph(graph: DependencyGraph | None, orphan: Component) -> bool:
    if graph is None:
        return False
    for e in graph.inbound(str(orphan.id)):
        src = graph.node(str(e.source_id))
        if src is not None and src.category == "CronTrigger":
            return True
    return False


def _lwc_imports_class(components: list[Component], class_name: str) -> str | None:
    for c in components:
        if c.category is not CategoryName.LWC_BUNDLE:
            continue
        imports = c.raw.get("apex_imports", []) if isinstance(c.raw, dict) else []
        for imp in imports:
            if isinstance(imp, str) and imp.split(".", 1)[0] == class_name:
                return c.name
    return None
