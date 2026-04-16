"""REST-based full record re-fetch (AD-21).

When a Pub/Sub subscriber lags past the 72h replay-id window, or when a
gap event arrives, the shadow store is out of sync with the live org.
This module re-queries the affected records via the MCP gateway and
overwrites the shadow store so the next CDC event has a correct baseline.

Strategy:
* per-record: SELECT FIELDS(STANDARD) FROM <sobject> WHERE Id = :id
* per-batch (for full-table reconciliation): a SOQL with IN clause, capped
  at 200 records per batch (the SF SOQL list-literal limit).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from offramp.core.logging import get_logger
from offramp.validate.shadow.store import ShadowStore

log = get_logger(__name__)


@dataclass
class Resyncer:
    """Performs REST-driven shadow-store reconciliation via the MCP gateway."""

    gateway: Any  # offramp.mcp.server.MCPGateway
    store: ShadowStore
    batch_size: int = 200

    async def resync_record(self, *, sobject: str, record_id: str) -> dict[str, Any]:
        soql = f"SELECT FIELDS(STANDARD) FROM {sobject} WHERE Id='{record_id}'"
        try:
            resp = await self.gateway.sf_query(soql)
        except Exception as exc:
            log.error(
                "shadow.reconcile.fetch_failed",
                sobject=sobject,
                record_id=record_id,
                error=str(exc),
            )
            return {"sobject": sobject, "record_id": record_id, "ok": False, "error": str(exc)}

        records = resp.get("records") or []
        if not records:
            await self.store.delete_record(sobject, record_id)
            return {"sobject": sobject, "record_id": record_id, "ok": True, "deleted": True}

        record: dict[str, Any] = records[0]
        await self.store.upsert_record(
            sobject=sobject,
            record_id=record_id,
            fields=record,
            replay_id=None,
        )
        return {"sobject": sobject, "record_id": record_id, "ok": True}

    async def resync_batch(
        self, *, sobject: str, record_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        ids = list(record_ids)
        outcomes: list[dict[str, Any]] = []
        for i in range(0, len(ids), self.batch_size):
            chunk = ids[i : i + self.batch_size]
            id_list = ",".join(f"'{x}'" for x in chunk)
            soql = f"SELECT FIELDS(STANDARD) FROM {sobject} WHERE Id IN ({id_list})"
            try:
                resp = await self.gateway.sf_query(soql)
            except Exception as exc:
                log.error(
                    "shadow.reconcile.batch_fetch_failed",
                    sobject=sobject,
                    chunk_size=len(chunk),
                    error=str(exc),
                )
                for rid in chunk:
                    outcomes.append(
                        {"sobject": sobject, "record_id": rid, "ok": False, "error": str(exc)}
                    )
                continue
            seen: set[str] = set()
            for rec in resp.get("records") or []:
                rid = rec.get("Id")
                if rid is None:
                    continue
                seen.add(rid)
                await self.store.upsert_record(
                    sobject=sobject,
                    record_id=rid,
                    fields=rec,
                    replay_id=None,
                )
                outcomes.append({"sobject": sobject, "record_id": rid, "ok": True})
            for missing in set(chunk) - seen:
                await self.store.delete_record(sobject, missing)
                outcomes.append(
                    {"sobject": sobject, "record_id": missing, "ok": True, "deleted": True}
                )
        return outcomes
