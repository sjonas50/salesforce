"""Detect common Apex trigger framework shapes.

Three patterns the architecture calls out (v2.1 §7.4.1):

* **Kevin O'Hara** trigger framework — single dispatcher with handler classes
  organized per sObject.
* **fflib / Apex Enterprise Patterns** — domain layer + selector + service.
* **Apex Trigger Actions Framework** — CMT-driven, the highest-confidence
  signal because the dispatch graph lives in metadata records.

Each detector returns a confidence score in [0, 1]. Confidence ≥ 0.7
unlocks framework-specific resolution rules in
:mod:`offramp.extract.dispatch.class_resolver`. Lower scores fall back to
generic resolution.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameworkSignal:
    """One framework-detection observation."""

    framework: str  # 'kevin_ohara' | 'fflib' | 'trigger_actions' | 'unknown'
    confidence: float
    evidence: tuple[str, ...]


def detect(apex_class_names: set[str], cmt_types_present: set[str]) -> list[FrameworkSignal]:
    """Score each framework against the available class + CMT corpus."""
    signals: list[FrameworkSignal] = []

    # Trigger Actions Framework — strongest signal: CMT type by that name.
    if any(t.lower().startswith("trigger_action") for t in cmt_types_present):
        signals.append(
            FrameworkSignal(
                framework="trigger_actions",
                confidence=0.95,
                evidence=("Trigger_Action__mdt CMT present",),
            )
        )

    # Kevin O'Hara — class names like ``TriggerHandler`` + per-sObject ``XxxHandler``.
    has_base = any(name == "TriggerHandler" for name in apex_class_names)
    has_handlers = sum(1 for name in apex_class_names if name.endswith("TriggerHandler"))
    if has_base and has_handlers >= 2:
        signals.append(
            FrameworkSignal(
                framework="kevin_ohara",
                confidence=0.85,
                evidence=(f"TriggerHandler base + {has_handlers} *TriggerHandler subclasses",),
            )
        )

    # fflib / Apex Enterprise Patterns — fflib_* class prefix.
    fflib_count = sum(1 for name in apex_class_names if name.startswith("fflib_"))
    if fflib_count >= 3:
        signals.append(
            FrameworkSignal(
                framework="fflib",
                confidence=0.8,
                evidence=(f"{fflib_count} fflib_* classes",),
            )
        )

    if not signals:
        signals.append(FrameworkSignal(framework="unknown", confidence=0.0, evidence=()))
    return signals
