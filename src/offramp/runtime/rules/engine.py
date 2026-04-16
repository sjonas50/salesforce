"""Tier 1 rules engine.

A rule is a pure Python function with the canonical signature::

    def evaluate(record, context) -> RuleResult

The engine loads rules from a generated artifact module, dispatches them by
the OoE step + sObject they target, and returns aggregated results that
the OoE runtime consumes for commit/abort decisions.

The engine is a **library**, not a service — it embeds in the MCP gateway
(write-time before-save validation) and in Temporal workers (when after-save
flows compose with rules in the same transaction).
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

# A rule is a callable evaluated once per (record, context) pair. The
# context carries OoE transaction state and any user/org-scoped knobs.
RuleFn = Callable[[dict[str, Any], dict[str, Any]], Any]


@dataclass(frozen=True)
class RuleResult:
    """Outcome of one rule evaluation."""

    rule_id: str
    kind: Literal["validation", "computation", "noop"]
    passed: bool
    error_message: str | None = None
    field_mutations: dict[str, Any] = field(default_factory=dict)


@dataclass
class Rule:
    """One registered rule with its targeting metadata."""

    rule_id: str
    sobject: str
    ooe_step: int
    fn: RuleFn
    kind: Literal["validation", "computation"] = "validation"
    error_message_template: str | None = None
    error_display_field: str | None = None
    fixes_field: str | None = None  # for before-save mutations

    def evaluate(self, record: dict[str, Any], context: dict[str, Any]) -> RuleResult:
        try:
            value = self.fn(record, context)
        except Exception as exc:
            return RuleResult(
                rule_id=self.rule_id,
                kind=self.kind,
                passed=False,
                error_message=f"rule raised {type(exc).__name__}: {exc}",
            )
        if self.kind == "validation":
            # Validation rules return True when the error condition holds —
            # i.e. value=True means the rule FAILED validation.
            failed = bool(value)
            return RuleResult(
                rule_id=self.rule_id,
                kind="validation",
                passed=not failed,
                error_message=self.error_message_template if failed else None,
            )
        # computation rule returns the new field value
        if self.fixes_field is not None and value is not None:
            return RuleResult(
                rule_id=self.rule_id,
                kind="computation",
                passed=True,
                field_mutations={self.fixes_field: value},
            )
        return RuleResult(rule_id=self.rule_id, kind="noop", passed=True)


class RulesEngine:
    """Registry + dispatcher for Tier 1 rules."""

    def __init__(self) -> None:
        self._rules: list[Rule] = []

    def register(self, rule: Rule) -> None:
        self._rules.append(rule)

    def rules_for(self, sobject: str, ooe_step: int) -> list[Rule]:
        return [r for r in self._rules if r.sobject == sobject and r.ooe_step == ooe_step]

    def evaluate_step(
        self,
        sobject: str,
        ooe_step: int,
        record: dict[str, Any],
        context: dict[str, Any],
    ) -> list[RuleResult]:
        """Run every rule registered for ``(sobject, step)`` and return results.

        The OoE runtime applies short-circuit logic on top of these results
        (e.g. a failed validation aborts the transaction).
        """
        return [r.evaluate(record, context) for r in self.rules_for(sobject, ooe_step)]

    def __len__(self) -> int:
        return len(self._rules)


def load_artifact(artifact_path: Path) -> RulesEngine:
    """Load a generated artifact and instantiate a RulesEngine from it.

    ``artifact_path`` may be either:
    * a single ``.py`` module exposing ``REGISTRY`` or ``register()``, or
    * a directory of submodules whose ``__init__.py`` exposes the same.

    Generated modules are self-contained (they import their runtime helpers
    at the top), so loading is a straightforward package import. The artifact
    parent dir is added to ``sys.path`` for the import duration.
    """
    import sys

    if artifact_path.name == "__init__.py":
        package_dir = artifact_path.parent
    elif artifact_path.is_dir():
        package_dir = artifact_path
    else:
        # Single-file module path.
        return _build_engine(_load_single_module(artifact_path), artifact_path)

    parent = package_dir.parent
    package_name = package_dir.name
    parent_str = str(parent.resolve())

    added = parent_str not in sys.path
    if added:
        sys.path.insert(0, parent_str)
    try:
        # Drop any cached version so reloads pick up regenerated artifacts.
        for cached in [
            k for k in list(sys.modules) if k == package_name or k.startswith(f"{package_name}.")
        ]:
            del sys.modules[cached]
        package = importlib.import_module(package_name)
    finally:
        if added:
            sys.path.remove(parent_str)
    return _build_engine(package, package_dir / "__init__.py")


def _load_single_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_offramp_artifact_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_engine(module: ModuleType, source_label: Path) -> RulesEngine:
    engine = RulesEngine()
    if hasattr(module, "register"):
        module.register(engine)
        return engine
    registry = getattr(module, "REGISTRY", None)
    if registry is None:
        raise ImportError(f"artifact {source_label} exposes neither register() nor REGISTRY")
    for r in registry:
        engine.register(r)
    return engine
