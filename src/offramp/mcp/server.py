"""MCP gateway skeleton.

The gateway is the **only** path to Salesforce in production. Every read,
write, and CDC subscription routes through it so we can centralize:

* OAuth + JWT auth (single Connected App per customer)
* API budget management (AD-24, ``/limits`` polling + per-process allocation)
* Engram anchoring of every call
* Tool-level permission scoping
* Pluggable backend: real ``simple-salesforce`` for prod, in-memory for tests

Phase 0 ships the gateway skeleton + the in-memory backend used by the smoke
test. The real Salesforce backend lands in Phase 1 alongside the extract
engine; the API quota allocator (AD-24) lands in Phase 3 (task 3.13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from offramp.core.logging import get_logger
from offramp.engram.client import EngramClient

log = get_logger(__name__)


class SalesforceBackend(Protocol):
    """Backend-agnostic Salesforce shim.

    Real impl wraps ``simple-salesforce``; test impl returns canned responses.
    """

    async def query(self, soql: str) -> dict[str, Any]: ...
    async def create(self, sobject: str, record: dict[str, Any]) -> dict[str, Any]: ...
    async def update(
        self, sobject: str, record_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def delete(self, sobject: str, record_id: str) -> dict[str, Any]: ...
    async def describe(self, sobject: str) -> dict[str, Any]: ...


@dataclass
class InMemorySalesforceBackend:
    """Test backend backed by a per-sObject record dict.

    Used by ``tests/integration/test_smoke.py`` and any unit test that wants
    to assert MCP gateway behavior without standing up a scratch org.
    """

    records: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    async def query(self, soql: str) -> dict[str, Any]:
        log.debug("mcp.in_memory.query", soql=soql)
        # Dumb pattern: SELECT ... FROM <Object> [WHERE Id='X'] — extract the object
        # and (optionally) Id; enough for the smoke test, not enough for real use.
        token = soql.upper().split(" FROM ")[-1].strip().split()[0]
        sobject = token.title()
        store = self.records.get(sobject, {})
        return {"totalSize": len(store), "done": True, "records": list(store.values())}

    async def create(self, sobject: str, record: dict[str, Any]) -> dict[str, Any]:
        store = self.records.setdefault(sobject, {})
        record_id = f"{sobject[:3].upper()}{len(store):015d}"
        store[record_id] = {**record, "Id": record_id}
        return {"id": record_id, "success": True, "errors": []}

    async def update(self, sobject: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        store = self.records.get(sobject, {})
        if record_id not in store:
            raise KeyError(f"{sobject} record {record_id} not found")
        store[record_id] = {**store[record_id], **fields}
        return {"success": True}

    async def delete(self, sobject: str, record_id: str) -> dict[str, Any]:
        store = self.records.get(sobject, {})
        if record_id not in store:
            raise KeyError(f"{sobject} record {record_id} not found")
        del store[record_id]
        return {"success": True}

    async def describe(self, sobject: str) -> dict[str, Any]:
        return {"name": sobject, "fields": []}


@dataclass
class MCPGateway:
    """The single Salesforce interface used by every runtime.

    Constructed with a backend (real or in-memory) and an Engram client.
    Every tool method anchors its call payload before returning.
    """

    backend: SalesforceBackend
    engram: EngramClient
    component: str = "mcp.gateway"

    async def sf_query(self, soql: str) -> dict[str, Any]:
        result = await self.backend.query(soql)
        await self.engram.anchor(self.component, {"tool": "sf_query", "soql": soql})
        return result

    async def sf_create(self, sobject: str, record: dict[str, Any]) -> dict[str, Any]:
        result = await self.backend.create(sobject, record)
        await self.engram.anchor(
            self.component,
            {"tool": "sf_create", "sobject": sobject, "result_id": result.get("id")},
        )
        return result

    async def sf_update(
        self, sobject: str, record_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self.backend.update(sobject, record_id, fields)
        await self.engram.anchor(
            self.component,
            {
                "tool": "sf_update",
                "sobject": sobject,
                "record_id": record_id,
                "field_keys": sorted(fields.keys()),
            },
        )
        return result

    async def sf_delete(self, sobject: str, record_id: str) -> dict[str, Any]:
        result = await self.backend.delete(sobject, record_id)
        await self.engram.anchor(
            self.component,
            {"tool": "sf_delete", "sobject": sobject, "record_id": record_id},
        )
        return result

    async def sf_describe(self, sobject: str) -> dict[str, Any]:
        result = await self.backend.describe(sobject)
        await self.engram.anchor(self.component, {"tool": "sf_describe", "sobject": sobject})
        return result
