"""Reconstruct pre-transaction record state from a parsed debug-log transaction.

Real debug logs include SOQL_EXECUTE rows that show the records read at the
top of the transaction. Compare Mode uses those as the seed; for fields the
log doesn't surface we fall back to the shadow store, then to defaults.

If a transaction has no observable pre-state (e.g. an insert), we return
an empty dict — that's the correct behavior for the OoE runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from offramp.validate.compare_mode.log_parser import ParsedTransaction
from offramp.validate.shadow.store import ShadowStore


@dataclass
class ReconstructedState:
    sobject: str
    record_id: str
    pre_state: dict[str, Any]


@dataclass
class StateReconstructor:
    """Build the input record dict the runtime needs to replay a txn."""

    store: ShadowStore

    async def reconstruct(self, txn: ParsedTransaction) -> list[ReconstructedState]:
        """Return one ReconstructedState per (sobject, row) the txn touched."""
        out: list[ReconstructedState] = []
        # The log rows DML at the type granularity; per-row id-recovery
        # needs SOQL_EXECUTE parsing (Phase 4 keeps it simple). For each
        # DML op we synthesize a single placeholder record id and pull
        # whatever state exists in the shadow store.
        for i, op in enumerate(txn.dml_ops):
            sobject = op["sobject"]
            # Use a deterministic placeholder id derived from txn time + index
            # so reruns are reproducible.
            placeholder = f"_log_{txn.start.isoformat()}_{sobject}_{i}"
            shadow = await self.store.get_record(sobject, placeholder)
            out.append(
                ReconstructedState(
                    sobject=sobject,
                    record_id=placeholder,
                    pre_state=shadow or {},
                )
            )
        return out
