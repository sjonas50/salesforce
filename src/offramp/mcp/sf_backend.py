"""Real Salesforce backend for the MCP gateway (simple-salesforce + JWT).

Phase 3 wired the real backend behind the :class:`SalesforceBackend`
Protocol so existing tests against the in-memory backend still pass — the
gateway picks one or the other at startup. The JWT bearer flow is now
fully implemented in :mod:`offramp.mcp.jwt_auth`; this module owns the
backend lifecycle (connect / invalidate / aclose) and the CRUD methods.

Integrates with the AD-24 :class:`QuotaAllocator`: every call charges
against the calling process's budget BEFORE hitting Salesforce, so a
runaway process is rejected at the gateway, not after consuming org-wide
quota.

Credentials never escape this process:
* Private key PEM is read once by :class:`SessionCache` and held in memory
* Access tokens are cached with a 55-minute TTL (SF default is 2h)
* Callers invalidate on 401 via :meth:`invalidate_session`
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from offramp.core.config import SalesforceSettings
from offramp.core.logging import get_logger
from offramp.mcp.jwt_auth import JWTAuthError, SessionCache
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
    _session_cache: SessionCache | None = None
    _connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _cache(self) -> SessionCache:
        if self._session_cache is None:
            self._session_cache = SessionCache(settings=self.settings)
        return self._session_cache

    async def connect(self) -> Any:
        """JWT bearer flow → simple_salesforce.Salesforce. Lazy + idempotent.

        Concurrent callers are serialized on ``_connect_lock`` so the
        expensive JWT exchange happens exactly once per backend instance
        (until invalidated).
        """
        cached = self._client
        if cached is not None:
            return cached
        async with self._connect_lock:
            # Re-check after acquiring the lock — a concurrent caller may
            # have populated _client while we were waiting. Read via a
            # fresh local to defeat mypy's cross-branch narrowing.
            cached = self._client
            if cached is not None:
                return cached
            # Import locally so the module is importable in environments
            # without the SF SDK installed (unit tests, MCP smoke).
            try:
                # simple-salesforce doesn't declare __all__; the runtime
                # attribute resolves fine but mypy needs a hint.
                from simple_salesforce import Salesforce  # type: ignore[attr-defined]
            except ImportError as exc:  # pragma: no cover — install-time only
                raise RuntimeError(
                    "simple-salesforce not installed; add the [salesforce] extra"
                ) from exc

            session = await self._cache().get()
            loop = asyncio.get_running_loop()
            try:
                sf = await loop.run_in_executor(
                    None,
                    lambda: Salesforce(
                        instance_url=session.instance_url,
                        version=self.settings.api_version,
                        session_id=session.access_token,
                    ),
                )
            except Exception as exc:
                raise JWTAuthError(
                    f"Salesforce client construction failed after JWT exchange: {exc}"
                ) from exc
            self._client = sf
            return sf

    async def invalidate_session(self) -> None:
        """Drop cached session + client. Next call re-auths via JWT.

        Call this on ``INVALID_SESSION_ID`` (401) from the SF API.
        """
        async with self._connect_lock:
            self._client = None
            if self._session_cache is not None:
                await self._session_cache.invalidate()

    async def aclose(self) -> None:
        """Release httpx pool held by the session cache."""
        if self._session_cache is not None:
            await self._session_cache.close()

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


# JWT exchange now lives in offramp.mcp.jwt_auth. The old ``_jwt_session_id``
# hook was replaced by :class:`SessionCache` which handles signing, exchange,
# TTL caching, and invalidation-on-401. Callers wanting a one-shot exchange
# can use ``offramp.mcp.jwt_auth.session_id(settings)``.
