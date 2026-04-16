"""Generate-engine orchestrator.

Walks an X-Ray output, dispatches each component through the translation
matrix to the right tier emitter, and writes a deployable artifact:

    artifact_dir/
    ├── manifest.json
    ├── tier1/
    │   ├── __init__.py            # exposes REGISTRY: list[Rule]
    │   └── <safe_id>.py           # one module per generated rule
    ├── tier2/
    │   └── <safe_id>.py           # one Temporal workflow per file
    ├── tier3/
    │   └── <safe_id>.py           # one LangGraph builder per file
    └── adapters/
        └── <pkg>.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from offramp.core.logging import get_logger
from offramp.core.models import CategoryName, Component, Tier
from offramp.engram.client import EngramClient
from offramp.generate import tier1, tier2, tier3
from offramp.generate.adapters.detector import detect as detect_packages
from offramp.generate.adapters.mcp_emitter import emit as emit_adapter
from offramp.generate.dual_target import emit as emit_dual_target
from offramp.generate.translation_matrix import classify, is_dual_target_candidate

log = get_logger(__name__)


@dataclass
class GenerateResult:
    """Aggregate output of a generate run."""

    process_id: str
    artifact_dir: Path
    tier1_count: int = 0
    tier2_count: int = 0
    tier3_count: int = 0
    dual_target_count: int = 0
    adapter_count: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (component_name, reason)


@dataclass
class GenerateOrchestrator:
    """Drive the full translation pass."""

    process_id: str
    out_dir: Path
    engram: EngramClient

    async def run(self, components: list[Component]) -> GenerateResult:
        result = GenerateResult(process_id=self.process_id, artifact_dir=self.out_dir)
        for sub in ("tier1", "tier2", "tier3", "adapters"):
            (self.out_dir / sub).mkdir(parents=True, exist_ok=True)

        registry_entries: list[dict[str, str]] = []  # for tier1/__init__.py
        manifest_components: list[dict[str, str]] = []

        for component in components:
            tier_assignment = classify(component)
            try:
                if tier_assignment.tier is Tier.TIER1_RULES:
                    self._emit_tier1(component, registry_entries, manifest_components, result)
                elif tier_assignment.tier is Tier.TIER2_TEMPORAL:
                    self._emit_tier2(component, manifest_components, result)
                elif tier_assignment.tier is Tier.TIER3_LANGGRAPH:
                    self._emit_tier3(component, manifest_components, result)
                if is_dual_target_candidate(component):
                    self._emit_dual_target(component, manifest_components, result)
            except (NotImplementedError, ValueError) as exc:
                result.skipped.append((component.name, str(exc)))
                log.warning(
                    "generate.skipped",
                    component=component.name,
                    category=component.category.value,
                    reason=str(exc),
                )
                continue

            await self.engram.anchor(
                component="generate.orchestrator",
                payload={
                    "component_id": str(component.id),
                    "tier": tier_assignment.tier.value,
                    "drivers": list(tier_assignment.drivers),
                },
            )

        # Generate adapter modules for any detected managed-package deps.
        for dep in detect_packages(components):
            adapter = emit_adapter(dep)
            (self.out_dir / "adapters" / f"{adapter.module_name}.py").write_text(
                adapter.code, encoding="utf-8"
            )
            result.adapter_count += 1

        # Write the Tier 1 registry shim.
        self._write_tier1_init(registry_entries)

        # Write the manifest last so it sees the final counts.
        manifest = {
            "schema_version": "1.0",
            "process_id": self.process_id,
            "tier1_count": result.tier1_count,
            "tier2_count": result.tier2_count,
            "tier3_count": result.tier3_count,
            "dual_target_count": result.dual_target_count,
            "adapter_count": result.adapter_count,
            "skipped": [{"name": n, "reason": r} for n, r in result.skipped],
            "components": manifest_components,
        }
        (self.out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        log.info(
            "generate.run.done",
            tier1=result.tier1_count,
            tier2=result.tier2_count,
            tier3=result.tier3_count,
            dual_target=result.dual_target_count,
            adapters=result.adapter_count,
            skipped=len(result.skipped),
        )
        return result

    def _emit_tier1(
        self,
        component: Component,
        registry: list[dict[str, str]],
        manifest: list[dict[str, str]],
        result: GenerateResult,
    ) -> None:
        if component.category not in {CategoryName.VALIDATION_RULE, CategoryName.FORMULA_FIELD}:
            # Tier 1 dispatch matrix points other categories here too, but
            # the translator currently only handles formulas. Skip cleanly.
            raise NotImplementedError(
                f"Tier 1 emitter does not yet handle {component.category.value}"
            )
        rule = tier1.translate(component)
        path = self.out_dir / "tier1" / f"{rule.function_name}.py"
        path.write_text(rule.code, encoding="utf-8")
        registry.append(
            {
                "module": rule.function_name,
                "rule_id": rule.rule_id,
                "sobject": rule.sobject,
                "ooe_step": str(rule.ooe_step),
                "kind": rule.kind,
                "function_name": rule.function_name,
                "error_message_template": rule.error_message_template or "",
                "error_display_field": rule.error_display_field or "",
                "fixes_field": rule.fixes_field or "",
            }
        )
        manifest.append({"name": component.name, "tier": "tier1_rules", "rule_id": rule.rule_id})
        result.tier1_count += 1

    def _emit_tier2(
        self,
        component: Component,
        manifest: list[dict[str, str]],
        result: GenerateResult,
    ) -> None:
        wf = tier2.translate(component)
        safe = wf.workflow_name
        (self.out_dir / "tier2" / f"{safe}.py").write_text(wf.code, encoding="utf-8")
        manifest.append(
            {"name": component.name, "tier": "tier2_temporal", "workflow_id": wf.workflow_id}
        )
        result.tier2_count += 1

    def _emit_tier3(
        self,
        component: Component,
        manifest: list[dict[str, str]],
        result: GenerateResult,
    ) -> None:
        ag = tier3.translate(component)
        (self.out_dir / "tier3" / f"{ag.builder_name}.py").write_text(ag.code, encoding="utf-8")
        manifest.append(
            {"name": component.name, "tier": "tier3_langgraph", "agent_id": ag.agent_id}
        )
        result.tier3_count += 1

    def _emit_dual_target(
        self,
        component: Component,
        manifest: list[dict[str, str]],
        result: GenerateResult,
    ) -> None:
        dt = emit_dual_target(component)
        if dt is None:
            return
        # Dual-target adds a *secondary* tier 2 wrapper alongside the primary
        # emission. Only the workflow goes in tier2/ — the rule was already
        # written in _emit_tier1.
        (self.out_dir / "tier2" / f"{dt.tier2.workflow_name}.py").write_text(
            dt.tier2.code, encoding="utf-8"
        )
        manifest.append(
            {
                "name": component.name,
                "tier": "dual_target",
                "rule_id": dt.tier1.rule_id,
                "workflow_id": dt.tier2.workflow_id,
            }
        )
        result.dual_target_count += 1

    def _write_tier1_init(self, entries: list[dict[str, str]]) -> None:
        """Emit a tier1/__init__.py that exposes REGISTRY: list[Rule]."""
        if not entries:
            (self.out_dir / "tier1" / "__init__.py").write_text("REGISTRY = []\n", encoding="utf-8")
            return
        lines = [
            '"""Auto-generated Tier 1 rule registry."""',
            "from offramp.runtime.rules.engine import Rule",
            "",
        ]
        for e in entries:
            lines.append(f"from . import {e['module']} as _{e['module']}")
        lines.append("")
        lines.append("REGISTRY: list[Rule] = [")
        for e in entries:
            lines.append("    Rule(")
            lines.append(f"        rule_id={e['rule_id']!r},")
            lines.append(f"        sobject={e['sobject']!r},")
            lines.append(f"        ooe_step={e['ooe_step']},")
            lines.append(f"        fn=_{e['module']}.{e['function_name']},")
            lines.append(f"        kind={e['kind']!r},")
            if e["error_message_template"]:
                lines.append(f"        error_message_template={e['error_message_template']!r},")
            if e["error_display_field"]:
                lines.append(f"        error_display_field={e['error_display_field']!r},")
            if e["fixes_field"]:
                lines.append(f"        fixes_field={e['fixes_field']!r},")
            lines.append("    ),")
        lines.append("]")
        (self.out_dir / "tier1" / "__init__.py").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


def write_skipped_report(result: GenerateResult) -> str:
    """Render a small text summary the CLI prints on completion."""
    out = [f"Generated artifact written to {result.artifact_dir}"]
    out.append(
        f"  tier1={result.tier1_count}  tier2={result.tier2_count}  "
        f"tier3={result.tier3_count}  dual_target={result.dual_target_count}  "
        f"adapters={result.adapter_count}  skipped={len(result.skipped)}"
    )
    if result.skipped:
        out.append("Skipped components:")
        for name, reason in result.skipped:
            out.append(f"  - {name}: {reason}")
    return "\n".join(out)


# Re-exposed so the CLI can importable-test the result type.
__all__ = ["GenerateOrchestrator", "GenerateResult", "asdict", "write_skipped_report"]
