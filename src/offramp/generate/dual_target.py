"""Dual-target generation for Tier 1 ↔ Tier 2 boundary components.

Per v2.1 §9.3.1 — for components near the boundary the translator emits
BOTH a Tier 1 rule AND a thin Tier 2 Temporal wrapper. The deployment
choice is deferred to deployment time (an operational config switch) and
made on operational characteristics (durability needed? human-visible
state needed? cross-process coordination?) rather than code generation.

Effect: the 20% of Phase 3 engineering effort that v2.0 reserved for
re-translation when a tier choice changes drops to ~8%, because moving
between targets is now a config flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from offramp.core.models import Component
from offramp.generate.tier1 import GeneratedRule
from offramp.generate.tier1 import translate as translate_tier1
from offramp.generate.tier2 import GeneratedWorkflow, _translate_generic_workflow
from offramp.generate.translation_matrix import is_dual_target_candidate


@dataclass(frozen=True)
class DualTarget:
    """The Tier 1 + Tier 2 emissions for one component."""

    component_id: str
    tier1: GeneratedRule
    tier2: GeneratedWorkflow


def emit(component: Component) -> DualTarget | None:
    """Return both emissions when the component is a boundary candidate.

    Tier 1 translation may fail (unsupported formula, etc.) — in that case
    the dual emission is skipped and the caller falls back to a single
    Tier 2 emission.
    """
    if not is_dual_target_candidate(component):
        return None
    try:
        rule = translate_tier1(component)
    except (NotImplementedError, ValueError):
        return None
    workflow = _translate_generic_workflow(component)
    return DualTarget(component_id=str(component.id), tier1=rule, tier2=workflow)
