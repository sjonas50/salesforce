"""Hand-tuned adapter for Salesforce CPQ (Steelbrick / SBQQ namespace).

Phase 3 ships ONE hand-tuned adapter as the reference implementation; the
remaining four (Conga, DocuSign, Marketing Cloud Connect, Pardot) follow the
same shape and are wired in subsequent phases.

The adapter exposes domain-specific MCP tools (``cpq_quote_configure``,
``cpq_quote_calculate``, etc.) that map cleanly onto CPQ's public Apex
interface. Generated workflows call these tools by name; the gateway routes
the call to the customer's live Salesforce org.
"""

from __future__ import annotations

from typing import Any

PACKAGE_NAME = "cpq"
NAMESPACE = "sbqq"


def cpq_quote_configure(quote_id: str, line_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Add or modify line items on a SBQQ__Quote__c.

    Phase 5 routes this to the MCP gateway → Salesforce REST. Phase 3 raises
    NotImplementedError so callers see exactly where the seam is.
    """
    raise NotImplementedError("cpq_quote_configure requires a live MCP gateway (Phase 5).")


def cpq_quote_calculate(quote_id: str) -> dict[str, Any]:
    """Trigger SBQQ.QuoteCalculator on a quote."""
    raise NotImplementedError("cpq_quote_calculate requires a live MCP gateway (Phase 5).")


def cpq_quote_to_order(quote_id: str) -> str:
    """Convert a quote to an order via CPQ's standard endpoint. Returns Order Id."""
    raise NotImplementedError("cpq_quote_to_order requires a live MCP gateway (Phase 5).")


# MCP tool registry — the gateway introspects this to expose the adapter.
TOOLS: tuple[str, ...] = (
    "cpq_quote_configure",
    "cpq_quote_calculate",
    "cpq_quote_to_order",
)
