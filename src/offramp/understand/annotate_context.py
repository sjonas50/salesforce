"""Dossiers: everything X-Ray knows about one component, written for the annotator.

The first annotation pass sent the model a 4,000-character slice of raw
metadata and asked for a confidence number. The dossier replaces that with the
reverse-engineered picture, in priority order:

1. identity (category, package, status, object);
2. facts established without a model — entry points, callouts, async work,
   DML/SOQL, fields written, complexity drivers, health findings, live
   verification results, and the deterministic tier hint;
3. the process model (``ProcessDefinition`` rendered as Markdown);
4. the dependency neighbourhood (who calls it, what it calls, data touched);
5. the source itself (Apex body, bundle files, normalised flow JSON), which is
   the only section that gets truncated to fit the budget — and the model is
   told when that happened.

Sharing rules with no rules, and managed code Salesforce hides, are handled
deterministically: the dossier says exactly what is knowable, and the
annotation is capped accordingly instead of asking the model to guess.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from offramp.core.models import CategoryName, Component
from offramp.core.process import ProcessDefinition
from offramp.knowledge.render import to_markdown
from offramp.understand.complexity import ComplexityScore
from offramp.understand.dependencies import DependencyGraph
from offramp.understand.health import HealthFinding
from offramp.understand.process_ir import build_process
from offramp.understand.tier_rules import TierHint, class_facts, tier_hint

DEFAULT_BUDGET = 30_000
_EDGE_CAP = 40
_SHARING_RULE_LISTS = ("criteria_rules", "owner_rules", "guest_rules", "territory_rules")


@dataclass
class Dossier:
    component_id: str
    name: str
    category: str
    text: str
    chars: int
    truncated: bool
    source_available: bool
    hint: TierHint
    definitions: list[ProcessDefinition] = field(default_factory=list)
    deterministic: dict[str, Any] | None = None  # a complete annotation needing no model call
    notes: list[str] = field(default_factory=list)


@dataclass
class DossierInputs:
    components: list[Component]
    graph: DependencyGraph | None = None
    health: list[HealthFinding] = field(default_factory=list)
    complexity: dict[str, ComplexityScore] = field(default_factory=dict)
    packages: list[dict[str, Any]] = field(default_factory=list)
    verify_results: list[dict[str, Any]] = field(default_factory=list)  # `offramp verify --json`
    org_alias: str = "org"
    budget: int = DEFAULT_BUDGET


def build_dossiers(inputs: DossierInputs) -> dict[str, Dossier]:
    """One dossier per component, keyed by component id."""
    by_name_health: dict[str, list[HealthFinding]] = defaultdict(list)
    for f in inputs.health:
        by_name_health[f.component.lower()].append(f)
    verify_by_name = {str(r.get("process", "")).lower(): r for r in inputs.verify_results}
    packages = {str(p.get("namespace") or "").lower(): p for p in inputs.packages}
    classes = class_facts(inputs.components)
    out: dict[str, Dossier] = {}
    for c in inputs.components:
        out[str(c.id)] = build_dossier(
            c,
            inputs,
            health=by_name_health,
            verify=verify_by_name,
            packages=packages,
            classes=classes,
        )
    return out


def build_dossier(
    c: Component,
    inputs: DossierInputs,
    *,
    health: dict[str, list[HealthFinding]],
    verify: dict[str, dict[str, Any]],
    packages: dict[str, dict[str, Any]],
    classes: dict[str, dict[str, list[str]]] | None = None,
) -> Dossier:
    raw = c.raw if isinstance(c.raw, dict) else {}
    definitions = build_process(c, org_alias=inputs.org_alias)
    hint = tier_hint(c, definitions, classes)
    notes: list[str] = []
    sections: list[tuple[str, str]] = []

    # ---- 1. identity ---------------------------------------------------------
    ident = [f"category: {c.category.value}", f"name: {c.name}"]
    if c.api_name and c.api_name != c.name:
        ident.append(f"api_name: {c.api_name}")
    if c.namespace:
        pkg = packages.get(c.namespace.lower())
        if pkg:
            ident.append(
                f"managed package: {pkg.get('name')} {pkg.get('version') or ''} "
                f"(namespace {c.namespace})".strip()
            )
        else:
            ident.append(f"namespace: {c.namespace} (installed package)")
    status = raw.get("status")
    if status:
        ident.append(f"status: {status}")
    obj = raw.get("object") or raw.get("sobject")
    if obj:
        ident.append(f"object: {obj}")
    sections.append(("IDENTITY", "\n".join(ident)))

    # ---- deterministic cases -------------------------------------------------
    source_available = True
    if c.category is CategoryName.SHARING_RULE:
        n_rules = sum(len(raw.get(k) or []) for k in _SHARING_RULE_LISTS)
        if n_rules == 0:
            det = {
                "summary": (
                    f"No sharing rules are defined for {obj or c.name}; record access follows the "
                    "org-wide default and the role hierarchy."
                ),
                "narrative": "The sharing-rules file for this object is empty: no criteria-based, "
                "owner-based, guest or territory rules extend access beyond the org-wide default.",
                "domain": "compliance",
                "complexity_band": "low",
                "recommended_tier": "tier1_rules",
                "tier_reasoning": "Access configuration, not process logic.",
                "evidence": ["sharing rules file has zero criteria/owner/guest/territory rules"],
                "unknowns": [],
                "confidence": 1.0,
            }
            text = _join(sections)
            return Dossier(
                component_id=str(c.id),
                name=c.name,
                category=c.category.value,
                text=text,
                chars=len(text),
                truncated=False,
                source_available=True,
                hint=hint,
                definitions=definitions,
                deterministic=det,
                notes=["empty sharing rules: annotated without a model call"],
            )
    if c.category in {CategoryName.APEX_CLASS, CategoryName.APEX_TRIGGER} and not raw.get(
        "has_body"
    ):
        source_available = False
        notes.append("source hidden (managed package)")
    if c.category in {CategoryName.LWC_BUNDLE, CategoryName.AURA_BUNDLE} and _bundle_hidden(raw):
        source_available = False
        notes.append("bundle sources hidden (managed package)")

    # ---- 2. facts ------------------------------------------------------------
    facts: list[str] = []
    if not source_available:
        facts.append(
            "SOURCE NOT AVAILABLE: Salesforce hides the source of managed-package code and "
            "bundles. Only the name, package, size, validity, and what org code references it "
            "are knowable."
        )
        if raw.get("length_without_comments"):
            facts.append(f"body length without comments: {raw['length_without_comments']} chars")
        if raw.get("is_valid") is not None:
            facts.append(f"compiles (IsValid): {raw['is_valid']}")
    analysis = raw.get("analysis") or {}
    for key, label in (
        ("entry_points", "entry points"),
        ("callouts", "callout types used"),
        ("named_credentials", "named credentials"),
        ("custom_settings", "custom settings read"),
        ("custom_labels", "custom labels"),
        ("dynamic_access", "dynamic access patterns"),
    ):
        vals = raw.get(key) if key in {"entry_points", "dynamic_access"} else analysis.get(key)
        if vals:
            facts.append(f"{label}: {', '.join(str(v) for v in vals)}")
    if analysis.get("async_calls"):
        facts.append(
            "async work: "
            + ", ".join(
                f"{a.get('mechanism')} {a.get('target_class') or ''}".strip()
                for a in analysis["async_calls"]
            )
        )
    if analysis.get("dml"):
        facts.append(
            "DML: "
            + ", ".join(
                f"{d.get('op')} {d.get('sobject') or d.get('target')}" for d in analysis["dml"][:12]
            )
        )
    if analysis.get("soql"):
        objs = sorted({q.get("sobject") for q in analysis["soql"] if q.get("sobject")})
        facts.append(f"SOQL objects: {', '.join(objs)}")
    refs = raw.get("references") or {}
    if refs.get("fields_written"):
        facts.append(f"fields written: {', '.join(refs['fields_written'][:25])}")
    if refs.get("fields") and not refs.get("fields_written"):
        facts.append(f"fields read: {', '.join(refs['fields'][:25])}")
    if c.category is CategoryName.APEX_TRIGGER:
        facts.append(f"trigger events: {', '.join(raw.get('events') or [])} ({raw.get('timing')})")
    score = inputs.complexity.get(str(c.id))
    if score is not None:
        facts.append(
            f"deterministic scores: translation difficulty {score.translation_difficulty}/100, "
            f"migration risk {score.migration_risk}/100; drivers: {', '.join(score.drivers) or 'none'}"
        )
    names = {c.name.lower(), (c.api_name or "").lower(), *(d.name.lower() for d in definitions)}
    for n in names:
        for f in health.get(n, []):
            facts.append(f"health {f.severity}: {f.code}: {f.message}")
    for n in names:
        v = verify.get(n)
        if v:
            checks = ", ".join(
                f"{ch.get('name')}={'ok' if ch.get('ok') else 'FAIL'}" for ch in v.get("checks", [])
            )
            facts.append(
                f"live verification ({v.get('status')}): path {' > '.join(v.get('path') or [])}; "
                f"{checks}"
            )
    facts.append(f"tier hint from static rules: {hint.tier} because " + "; ".join(hint.reasons))
    sections.append(("FACTS (established by static analysis, not by you)", "\n".join(facts)))

    # ---- 3. process model ----------------------------------------------------
    if definitions:
        model = "\n\n".join(to_markdown(d) for d in definitions[:6])
        if len(definitions) > 6:
            model += f"\n\n[{len(definitions) - 6} more definitions omitted]"
        sections.append(("PROCESS MODEL (reverse-engineered)", model))

    # ---- 4. neighbourhood ----------------------------------------------------
    g = inputs.graph
    if g is not None and g.node(str(c.id)) is not None:
        inbound = []
        for e in g.inbound(str(c.id))[:_EDGE_CAP]:
            s = g.node(str(e.source_id))
            if s is not None:
                inbound.append(f"{s.category} {s.api_name} --{e.kind.value}-->")
        calls: list[str] = []
        data: list[str] = []
        for e in g.outbound(str(c.id)):
            t = g.node(str(e.target_id))
            if t is None:
                continue
            line = f"--{e.kind.value}--> {t.category} {t.api_name}" + (
                f" ({e.notes})" if e.notes else ""
            )
            (data if t.kind in {"object", "field", "record_type"} else calls).append(line)
        neigh = []
        if inbound:
            neigh.append("used by:\n  " + "\n  ".join(inbound))
        if calls:
            neigh.append("uses:\n  " + "\n  ".join(calls[:_EDGE_CAP]))
        if data:
            neigh.append("data touched:\n  " + "\n  ".join(data[:_EDGE_CAP]))
        if not inbound and c.category in {CategoryName.APEX_CLASS, CategoryName.LWC_BUNDLE}:
            neigh.append("used by: nothing in the org references this component")
        if neigh:
            sections.append(("DEPENDENCY NEIGHBOURHOOD", "\n".join(neigh)))

    # ---- 5. source -----------------------------------------------------------
    source = _source_text(c, raw)
    head = _join(sections)
    truncated = False
    if source:
        room = inputs.budget - len(head) - 200
        if room < 2000:
            room = 2000
        if len(source) > room:
            source = source[:room] + f"\n[... truncated {len(source) - room} chars]"
            truncated = True
            notes.append("source truncated to fit the budget")
        sections.append(("SOURCE", source))
    text = _join(sections)
    return Dossier(
        component_id=str(c.id),
        name=c.name,
        category=c.category.value,
        text=text,
        chars=len(text),
        truncated=truncated,
        source_available=source_available,
        hint=hint,
        definitions=definitions,
        notes=notes,
    )


def _bundle_hidden(raw: dict[str, Any]) -> bool:
    """Managed bundles come back as ``(hidden)`` per file (or with no sources captured)."""
    sources = raw.get("sources")
    if not isinstance(sources, dict) or not sources:
        return False
    return all(len(v.strip()) <= 12 for v in sources.values())


def _source_text(c: Component, raw: dict[str, Any]) -> str:
    if c.category in {CategoryName.APEX_CLASS, CategoryName.APEX_TRIGGER}:
        return str(raw.get("body") or "")
    if c.category in {CategoryName.LWC_BUNDLE, CategoryName.AURA_BUNDLE}:
        sources = raw.get("sources") or {}
        if _bundle_hidden(raw):
            return "(hidden by Salesforce: managed package bundle)"
        if isinstance(sources, dict) and sources:
            return "\n\n".join(f"--- {name} ---\n{text}" for name, text in sources.items())
        return (
            "bundle sources were not captured by this scan (files: "
            + ", ".join(raw.get("files") or [])
            + ")"
        )
    # Flows, rules, layouts …: the normalised metadata, minus what the model already saw above.
    skip = {"references", "analysis", "body", "sources", "raw_root_keys", "element_counts"}
    slim = {k: v for k, v in raw.items() if k not in skip}
    return json.dumps(slim, indent=1, sort_keys=True, default=str)


def _join(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"## {title}\n{body}" for title, body in sections)


# ---- process (cluster) dossiers ---------------------------------------------


def build_process_dossier(
    process_id: str,
    label: str,
    members: list[Component],
    summaries: dict[str, dict[str, Any]],
    graph: DependencyGraph | None,
    object_names: list[str],
    *,
    budget: int = DEFAULT_BUDGET,
) -> str:
    """Text for a business-process (cluster) annotation: members, their annotations, shared data."""
    lines = [f"## PROCESS CLUSTER {process_id}: {label}", f"objects: {', '.join(object_names)}"]
    lines.append("\n## MEMBERS (with their component annotations)")
    for m in members:
        a = summaries.get(str(m.id)) or {}
        tier = a.get("recommended_tier", "")
        lines.append(
            f"- [{m.category.value}] {m.name}"
            + (f" — {a.get('summary')}" if a.get("summary") else "")
            + (f" (tier: {tier}, domain: {a.get('domain')})" if tier else "")
        )
    if graph is not None:
        ids = {str(m.id) for m in members}
        edges: list[str] = []
        for m in members:
            for e in graph.outbound(str(m.id)):
                if str(e.target_id) in ids:
                    t = graph.node(str(e.target_id))
                    if t is not None:
                        edges.append(f"{m.name} --{e.kind.value}--> {t.api_name}")
        if edges:
            lines.append("\n## EDGES BETWEEN MEMBERS")
            lines.extend(edges[:80])
    text = "\n".join(lines)
    return text[:budget]
