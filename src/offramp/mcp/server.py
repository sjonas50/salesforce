"""MCP gateway skeleton.

The gateway is the **only** path to Salesforce in production. Every read,
write, and CDC subscription routes through it so we can centralize:

* OAuth + JWT auth (single Connected App per customer)
* API budget management (AD-24, ``/limits`` polling + per-process allocation)
* Engram anchoring of every call
* Tool-level permission scoping
* Pluggable backend: real ``simple-salesforce`` for prod, in-memory for tests

Two backends satisfy :class:`SalesforceBackend`: the in-memory one used by
tests, and :class:`offramp.mcp.sf_backend.SimpleSalesforceBackend` (JWT bearer
+ simple-salesforce) for real orgs. The AD-24 quota allocator lives in
:mod:`offramp.mcp.quota`.
"""

from __future__ import annotations

import re
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
    async def describe_global(self) -> dict[str, Any]: ...
    async def tooling_query(self, soql: str) -> dict[str, Any]: ...
    async def restful(self, path: str, params: dict[str, Any] | None = None) -> Any: ...
    async def request(self, method: str, path: str, json: Any | None = None) -> Any: ...
    async def get_text(self, path: str) -> str: ...
    async def mdapi_retrieve(self, unpackaged: dict[str, list[str]]) -> bytes: ...
    async def mdapi_deploy(self, zip_bytes: bytes) -> dict[str, Any]: ...
    async def mdapi_list(self, metadata_type: str, folder: str | None = None) -> list[str]: ...


@dataclass
class InMemorySalesforceBackend:
    """Test backend backed by a per-sObject record dict.

    Used by ``tests/integration/test_smoke.py`` and any unit test that wants
    to assert MCP gateway behavior without standing up a scratch org.
    """

    records: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # Tooling objects (ApexClass, FlowDefinition, ...) keyed by object name, plus
    # canned describe payloads and REST paths — enough to test the REST pull path.
    tooling: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    describes: dict[str, dict[str, Any]] = field(default_factory=dict)
    rest: dict[str, Any] = field(default_factory=dict)
    # Metadata API stand-in: a ZIP the retrieve returns, and folder listings.
    mdapi_zip: bytes = b""
    mdapi_folders: dict[str, list[str]] = field(default_factory=dict)
    responses: dict[tuple[str, str], Any] = field(default_factory=dict)
    deployed: list[bytes] = field(default_factory=list)
    requests: list[tuple[str, str, Any]] = field(default_factory=list)

    async def query(self, soql: str) -> dict[str, Any]:
        log.debug("mcp.in_memory.query", soql=soql)
        # Dumb pattern: SELECT ... FROM <Object> [WHERE Id='X'] — extract the object
        # and (optionally) Id; enough for the smoke test, not enough for real use.
        token = soql.upper().split(" FROM ")[-1].strip().split()[0]
        store: dict[str, dict[str, Any]] = {}
        for name, rows in self.records.items():
            if name.upper() == token:
                store = rows
                break
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
        return self.describes.get(sobject, {"name": sobject, "fields": []})

    async def describe_global(self) -> dict[str, Any]:
        names = set(self.records) | set(self.describes)
        return {
            "sobjects": [
                {
                    "name": n,
                    "label": n,
                    "custom": n.endswith("__c"),
                    "queryable": True,
                    "triggerable": True,
                }
                for n in sorted(names)
            ]
        }

    async def tooling_query(self, soql: str) -> dict[str, Any]:
        """Filter canned rows by FROM object and a simple ``WHERE Field = 'x'`` / ``Id IN (...)``."""
        log.debug("mcp.in_memory.tooling_query", soql=soql)
        upper = soql.upper()
        obj = upper.split(" FROM ", 1)[1].strip().split()[0] if " FROM " in upper else ""
        rows = [r for r in self.tooling.get(obj, [])] or [
            r for k, v in self.tooling.items() if k.upper() == obj for r in v
        ]
        m = re.search(r"WHERE\s+([A-Za-z_.]+)\s*=\s*'([^']*)'", soql, re.I)
        if m:
            fld, val = m.group(1), m.group(2)
            rows = [r for r in rows if str(_dig(r, fld)) == val]
        m = re.search(r"WHERE\s+([A-Za-z_.]+)\s+IN\s*\(([^)]*)\)", soql, re.I)
        if m:
            fld = m.group(1)
            vals = {v.strip().strip("'") for v in m.group(2).split(",")}
            rows = [r for r in rows if str(_dig(r, fld)) in vals]
        m = re.search(r"WHERE\s+([A-Za-z_.]+)\s+LIKE\s*'([^']*)'", soql, re.I)
        if m:
            fld, pat = m.group(1), m.group(2)
            rx = "^" + re.escape(pat).replace("%", ".*").replace("_", ".") + "$"
            rows = [r for r in rows if re.match(rx, str(_dig(r, fld) or ""), re.I)]
        return {"totalSize": len(rows), "done": True, "records": list(rows)}

    async def restful(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path in self.rest:
            return self.rest[path]
        if path.startswith("query") and params and "q" in params:
            return await self.query(str(params["q"]))
        if path == "limits/recordCount" and params:
            names = str(params.get("sObjects", "")).split(",")
            return {
                "sObjects": [{"name": n, "count": len(self.records.get(n, {}))} for n in names if n]
            }
        return {}

    async def get_text(self, path: str) -> str:
        """Raw (non-JSON) GET such as an ApexLog body: ``responses[("GET", path)]``."""
        self.requests.append(("GET", path, None))
        v = self.responses.get(("GET", path), "")
        return v if isinstance(v, str) else str(v)

    async def request(self, method: str, path: str, json: Any | None = None) -> Any:
        """Canned non-GET REST/Tooling calls: ``responses[(METHOD, path)]``; every call is
        appended to ``requests`` so tests can assert what the runner did."""
        self.requests.append((method.upper(), path, json))
        key = (method.upper(), path)
        if key in self.responses:
            return self.responses[key]
        if method.upper() == "POST" and "/sobjects/" in path:
            return {
                "id": f"{path.rsplit('/', 1)[-1][:3]}{len(self.requests):012d}",
                "success": True,
            }
        return {}

    async def mdapi_deploy(self, zip_bytes: bytes) -> dict[str, Any]:
        self.deployed.append(zip_bytes)
        return {"status": "Succeeded", "id": f"0Af{len(self.deployed):015d}", "errors": []}

    async def mdapi_retrieve(self, unpackaged: dict[str, list[str]]) -> bytes:
        log.debug("mcp.in_memory.mdapi_retrieve", types=sorted(unpackaged))
        return self.mdapi_zip

    async def mdapi_list(self, metadata_type: str, folder: str | None = None) -> list[str]:
        return list(self.mdapi_folders.get(f"{metadata_type}:{folder or ''}", []))


def _dig(row: dict[str, Any], dotted: str) -> Any:
    cur: Any = row
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


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

    async def sf_describe_global(self) -> dict[str, Any]:
        result = await self.backend.describe_global()
        await self.engram.anchor(self.component, {"tool": "sf_describe_global"})
        return result

    async def sf_tooling_query(self, soql: str) -> dict[str, Any]:
        result = await self.backend.tooling_query(soql)
        await self.engram.anchor(self.component, {"tool": "sf_tooling_query", "soql": soql})
        return result

    async def sf_restful(self, path: str, params: dict[str, Any] | None = None) -> Any:
        result = await self.backend.restful(path, params)
        await self.engram.anchor(self.component, {"tool": "sf_restful", "path": path})
        return result

    async def sf_get_text(self, path: str) -> str:
        """Raw GET returning text (ApexLog bodies are plain text, not JSON)."""
        text = await self.backend.get_text(path)
        await self.engram.anchor(
            self.component, {"tool": "sf_get_text", "path": path, "bytes": len(text)}
        )
        return text

    async def sf_request(self, method: str, path: str, json: Any | None = None) -> Any:
        """Non-GET REST / Tooling call (create a TraceFlag, invoke a flow action, ...)."""
        result = await self.backend.request(method, path, json)
        await self.engram.anchor(
            self.component, {"tool": "sf_request", "method": method.upper(), "path": path}
        )
        return result

    async def sf_mdapi_deploy(self, zip_bytes: bytes) -> dict[str, Any]:
        """Metadata API deploy of a ZIP (round-trip verification deploys rendered flows)."""
        result = await self.backend.mdapi_deploy(zip_bytes)
        await self.engram.anchor(
            self.component,
            {"tool": "sf_mdapi_deploy", "bytes": len(zip_bytes), "status": result.get("status")},
        )
        return result

    async def sf_mdapi_retrieve(self, unpackaged: dict[str, list[str]]) -> bytes:
        result = await self.backend.mdapi_retrieve(unpackaged)
        await self.engram.anchor(
            self.component,
            {"tool": "sf_mdapi_retrieve", "types": sorted(unpackaged), "bytes": len(result)},
        )
        return result

    async def sf_mdapi_list(self, metadata_type: str, folder: str | None = None) -> list[str]:
        result = await self.backend.mdapi_list(metadata_type, folder)
        await self.engram.anchor(
            self.component, {"tool": "sf_mdapi_list", "type": metadata_type, "folder": folder}
        )
        return result
