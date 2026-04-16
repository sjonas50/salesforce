"""Engram client.

Phase 0 ships an in-memory + optional-HTTP stub so callers can integrate today
and switch to the real backend without code changes once Engram E1/E2 are
available (weeks 6 and 12 of the v2.1 plan).

The contract is intentionally narrow: ``anchor`` is the only method everything
else builds on. Higher-level helpers belong in caller packages, not here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from offramp.core.hashing import canonical_json, content_hash
from offramp.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class AnchorRecord:
    """One Engram anchor.

    ``anchor_id`` is unique per record. ``content_hash`` is the SHA-256 of the
    canonical-JSON payload — two anchors with the same hash refer to the same
    underlying artifact.
    """

    anchor_id: str
    content_hash: str
    component: str  # the calling component, e.g. 'extract.pull.salto'
    payload: dict[str, Any]


class EngramClient(Protocol):
    """Async contract; both in-memory and HTTP backends satisfy this."""

    async def anchor(self, component: str, payload: dict[str, Any]) -> AnchorRecord: ...

    async def get(self, anchor_id: str) -> AnchorRecord | None: ...

    async def find_by_hash(self, content_hash: str) -> list[AnchorRecord]: ...


@dataclass
class InMemoryEngramClient:
    """Used by unit tests and ``make smoke`` when no Engram backend is reachable."""

    _records: dict[str, AnchorRecord] = field(default_factory=dict)
    _by_hash: dict[str, list[str]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def anchor(self, component: str, payload: dict[str, Any]) -> AnchorRecord:
        async with self._lock:
            ch = content_hash(payload)
            anchor_id = f"engram:{ch[:12]}:{len(self._records)}"
            record = AnchorRecord(
                anchor_id=anchor_id,
                content_hash=ch,
                component=component,
                payload=payload,
            )
            self._records[anchor_id] = record
            self._by_hash.setdefault(ch, []).append(anchor_id)
            log.debug("engram.anchor.in_memory", component=component, anchor_id=anchor_id)
            return record

    async def get(self, anchor_id: str) -> AnchorRecord | None:
        return self._records.get(anchor_id)

    async def find_by_hash(self, content_hash: str) -> list[AnchorRecord]:
        return [self._records[aid] for aid in self._by_hash.get(content_hash, [])]


@dataclass
class HTTPEngramClient:
    """Talks to a running Engram server over HTTP.

    The server is a placeholder for the real Rust impl — same wire shape, so
    swapping in production is a base-URL change.
    """

    base_url: str
    _http: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        return self._http

    async def anchor(self, component: str, payload: dict[str, Any]) -> AnchorRecord:
        client = await self._ensure_client()
        body = canonical_json({"component": component, "payload": payload})
        resp = await client.post(
            "/anchor",
            content=body,
            headers={"content-type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return AnchorRecord(
            anchor_id=data["anchor_id"],
            content_hash=data["content_hash"],
            component=component,
            payload=payload,
        )

    async def get(self, anchor_id: str) -> AnchorRecord | None:
        client = await self._ensure_client()
        resp = await client.get(f"/anchor/{anchor_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return AnchorRecord(
            anchor_id=data["anchor_id"],
            content_hash=data["content_hash"],
            component=data["component"],
            payload=data["payload"],
        )

    async def find_by_hash(self, content_hash: str) -> list[AnchorRecord]:
        client = await self._ensure_client()
        resp = await client.get(f"/by-hash/{content_hash}")
        resp.raise_for_status()
        return [
            AnchorRecord(
                anchor_id=item["anchor_id"],
                content_hash=item["content_hash"],
                component=item["component"],
                payload=item["payload"],
            )
            for item in resp.json()
        ]

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


@asynccontextmanager
async def open_client(base_url: str | None = None) -> AsyncIterator[EngramClient]:
    """Pick a backend and clean up cleanly on exit.

    ``None`` (the default) returns an in-memory client. Passing a base URL
    returns an HTTP-backed client and closes its connection pool on exit.
    """
    if base_url is None:
        yield InMemoryEngramClient()
        return

    client = HTTPEngramClient(base_url=base_url)
    try:
        yield client
    finally:
        await client.aclose()
