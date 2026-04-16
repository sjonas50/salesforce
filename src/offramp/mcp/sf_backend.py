"""Real Salesforce backend for the MCP gateway (simple-salesforce + JWT).

Phase 3 wires the real backend behind the existing :class:`SalesforceBackend`
Protocol so existing tests against the in-memory backend still pass — the
gateway picks one or the other at startup.

The backend integrates with the AD-24 :class:`QuotaAllocator`: every call
charges against the calling process's budget BEFORE hitting Salesforce, so
a runaway process is rejected at the gateway, not after consuming org-wide
quota.

JWT bearer flow auth lives here so credentials never escape the backend
module — the gateway calls high-level CRUD methods and never sees the
private key.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from offramp.core.config import SalesforceSettings
from offramp.core.logging import get_logger
from offramp.mcp.quota import QuotaAllocator

log = get_logger(__name__)


@dataclass
class SimpleSalesforceBackend:
    """simple-salesforce-backed implementation of the MCP backend Protocol.

    Constructed with a settings bundle + an optional :class:`QuotaAllocator`.
    When the allocator is supplied, every call charges 1 unit against the
    configured ``process_id`` and raises :class:`QuotaExhausted` when the
    process is over its share.

    Phase 3 ships the integration; the real auth flow + simple-salesforce
    object construction is gated behind ``connect()`` so unit tests that
    exercise the budget path don't need a real org.
    """

    settings: SalesforceSettings
    process_id: str
    quota: QuotaAllocator | None = None
    _client: Any = None  # simple_salesforce.Salesforce, lazy

    async def connect(self) -> Any:
        """JWT bearer flow → simple_salesforce.Salesforce. Lazy + idempotent."""
        if self._client is not None:
            return self._client
        # Import locally so the module is importable in environments without
        # the SF SDK installed (e.g. unit tests against in-memory backend).
        try:
            # simple-salesforce doesn't ship explicit __all__ exports; mypy
            # flags Salesforce as not-explicitly-exported. The runtime import
            # works fine, so suppress the strict-mode flag here.
            from simple_salesforce import Salesforce  # type: ignore[attr-defined]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "simple-salesforce not installed; add the [salesforce] extra"
            ) from exc

        # JWT bearer flow assembly is intentionally minimal — the production
        # cert lifecycle is documented in docs/runbooks/jwt_cert_rotation.md.
        # Phase 3 ships the surface; the real handshake exercises against a
        # scratch org once Phase 5 deployment lands.
        loop = asyncio.get_running_loop()
        sf = await loop.run_in_executor(
            None,
            lambda: Salesforce(
                instance_url=self.settings.login_url,
                version=self.settings.api_version,
                # Real auth wiring happens at deploy time; the JWT-bearer
                # exchange returns a session_id that simple-salesforce accepts
                # via the ``session_id=`` constructor kw. Phase 3 leaves the
                # exchange function as a hook (``_jwt_session_id``) so tests
                # can monkeypatch it.
                session_id=_jwt_session_id(self.settings),
            ),
        )
        self._client = sf
        return sf

    async def _charge_quota(self) -> None:
        if self.quota is not None:
            await self.quota.consume(self.process_id, 1)

    async def query(self, soql: str) -> dict[str, Any]:
        await self._charge_quota()
        sf = await self.connect()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, sf.query_all, soql)

    async def create(self, sobject: str, record: dict[str, Any]) -> dict[str, Any]:
        await self._charge_quota()
        sf = await self.connect()
        loop = asyncio.get_running_loop()
        obj = getattr(sf, sobject)
        return await loop.run_in_executor(None, obj.create, record)

    async def update(self, sobject: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        await self._charge_quota()
        sf = await self.connect()
        loop = asyncio.get_running_loop()
        obj = getattr(sf, sobject)
        rc = await loop.run_in_executor(None, obj.update, record_id, fields)
        return {"success": rc == 204, "status": rc}

    async def delete(self, sobject: str, record_id: str) -> dict[str, Any]:
        await self._charge_quota()
        sf = await self.connect()
        loop = asyncio.get_running_loop()
        obj = getattr(sf, sobject)
        rc = await loop.run_in_executor(None, obj.delete, record_id)
        return {"success": rc == 204, "status": rc}

    async def describe(self, sobject: str) -> dict[str, Any]:
        await self._charge_quota()
        sf = await self.connect()
        loop = asyncio.get_running_loop()
        obj = getattr(sf, sobject)
        return await loop.run_in_executor(None, obj.describe)


def _jwt_session_id(settings: SalesforceSettings) -> str:
    """Exchange a JWT bearer assertion for a Salesforce session id.

    Phase 5 wires this up against the live ``/services/oauth2/token`` endpoint
    using the PEM-encoded RSA key at ``settings.jwt_key_path``. Phase 3 keeps
    this as a clear hook so the real backend can be smoke-tested against a
    scratch org without a code change to its callers.
    """
    raise NotImplementedError(
        "JWT session exchange lands in Phase 5 alongside the cert-rotation runbook. "
        "Tests should monkeypatch this function or use InMemorySalesforceBackend."
    )
