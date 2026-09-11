"""Apex static analysis (C20, AD-31).

Two engines behind one contract, :class:`offramp.extract.apex.model.ApexAnalysis`:

* ``ast`` — Salesforce's ANTLR grammar via ``tools/apex-parser`` (Node), walked by
  :mod:`offramp.extract.apex.ast_analyzer`; scoped variable typing, exact
  statement shapes, no enum/inner-type false positives.
* ``tokenizer`` — :mod:`offramp.extract.apex.references`, pure Python, always
  available; the fallback when Node or the grammar package is missing or a file
  does not parse.

``OFFRAMP_APEX_ENGINE`` (``auto`` | ``ast`` | ``tokenizer``) forces a choice.
"""

from __future__ import annotations

import os

from offramp.core.logging import get_logger
from offramp.extract.apex import ast_bridge
from offramp.extract.apex.ast_analyzer import analyze_tree
from offramp.extract.apex.model import ApexAnalysis, DmlRef, SoqlRef
from offramp.extract.apex.references import analyze as analyze_tokens

log = get_logger(__name__)

__all__ = ["ApexAnalysis", "DmlRef", "SoqlRef", "analyze", "analyze_tokens", "analyze_tree"]

_warned = False


def selected_engine(engine: str | None = None) -> str:
    """Resolve ``engine`` / ``$OFFRAMP_APEX_ENGINE`` to ``ast`` or ``tokenizer``."""
    choice = (engine or os.environ.get("OFFRAMP_APEX_ENGINE") or "auto").lower()
    if choice == "tokenizer":
        return "tokenizer"
    if choice == "ast":
        return "ast"
    return "ast" if ast_bridge.is_available() else "tokenizer"


def analyze(
    source: str, *, name_hint: str | None = None, engine: str | None = None
) -> ApexAnalysis:
    """Analyze one Apex class / interface / trigger body with the selected engine."""
    global _warned
    if selected_engine(engine) == "ast":
        try:
            result = ast_bridge.parse(source)
        except (ast_bridge.AstUnavailable, ast_bridge.AstError) as exc:
            if engine == "ast":
                raise
            if not _warned:
                log.warning("apex.ast.unavailable", error=str(exc))
                _warned = True
        else:
            if result.ok:
                return analyze_tree(result.tree, source, name_hint=name_hint)
            log.debug(
                "apex.ast.parse_errors",
                name=name_hint,
                errors=len(result.errors),
                first=result.errors[0],
            )
            a = analyze_tokens(source, name_hint=name_hint)
            a.parse_errors = len(result.errors)
            return a
    return analyze_tokens(source, name_hint=name_hint)
