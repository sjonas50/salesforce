"""Forked data environment (architecture §10.3).

Reads proxy through the MCP gateway to production Salesforce; writes are
intercepted and routed to the shadow Postgres. Subsequent reads within the
same transaction first check the shadow store, then fall through to
production. This lets shadow execution chain operations realistically (lead
create -> task create -> account update) without polluting production data
or consuming production write quota.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from offramp.core.logging import get_logger
from offramp.validate.shadow.store import ShadowStore

log = get_logger(__name__)


# A read function: ``read(sobject, record_id) -> dict | None``.
ReadFn = Callable[[str, str], Awaitable[dict[str, Any] | None]]


@dataclass
class ForkedDataEnv:
    """One transaction's view of the forked environment.

    Construct one per shadow transaction. Within the transaction:
    * ``write`` always lands in shadow Postgres
    * ``read`` checks shadow Postgres first, then falls through to production
    * ``intercepted_writes`` accumulates the writes for trace + diff
    """

    store: ShadowStore
    production_read: ReadFn
    process_id: str
    intercepted_writes: list[dict[str, Any]] = field(default_factory=list)
    _local_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    async def read(self, sobject: str, record_id: str) -> dict[str, Any] | None:
        # 1. local cache (writes earlier in this txn).
        cached = self._local_cache.get((sobject, record_id))
        if cached is not None:
            return cached
        # 2. shadow Postgres (writes from a prior txn's chained operations).
        shadow = await self.store.get_record(sobject, record_id)
        if shadow is not None:
            return shadow
        # 3. fall through to production via the supplied reader.
        return await self.production_read(sobject, record_id)

    async def write(
        self,
        *,
        op: str,
        sobject: str,
        record_id: str,
        fields: dict[str, Any],
        replay_id: str | None = None,
    ) -> dict[str, Any]:
        if op not in {"create", "update", "delete"}:
            raise ValueError(f"unknown write op: {op}")

        if op == "delete":
            await self.store.delete_record(sobject, record_id)
            self._local_cache.pop((sobject, record_id), None)
        else:
            # Merge with prior cache value (update == partial fields).
            base = self._local_cache.get((sobject, record_id))
            if base is None:
                base = await self.store.get_record(sobject, record_id) or {}
            merged = {**base, **fields, "Id": record_id}
            self._local_cache[(sobject, record_id)] = merged
            await self.store.upsert_record(
                sobject=sobject,
                record_id=record_id,
                fields=merged,
                replay_id=replay_id,
            )

        write_record = {
            "op": op,
            "sobject": sobject,
            "record_id": record_id,
            "field_keys": sorted(fields.keys()),
        }
        self.intercepted_writes.append(write_record)
        return write_record


def production_read_via_mcp(gateway: Any) -> ReadFn:
    """Build a read-through function that issues a SOQL via the MCP gateway."""

    async def _read(sobject: str, record_id: str) -> dict[str, Any] | None:
        # Pull every field via SELECT * isn't valid in SOQL — use the describe
        # to enumerate fields. For the shadow path we keep it cheap: query
        # only the most-commonly-needed fields. Real customers configure
        # their own per-object projections at deploy time.
        soql = f"SELECT FIELDS(STANDARD) FROM {sobject} WHERE Id='{record_id}'"
        try:
            resp = await gateway.sf_query(soql)
        except Exception as exc:
            log.warning(
                "shadow.data_env.read_through_failed",
                sobject=sobject,
                record_id=record_id,
                error=str(exc),
            )
            return None
        records = resp.get("records") or []
        if not records:
            return None
        first: dict[str, Any] = records[0]
        return first

    return _read
