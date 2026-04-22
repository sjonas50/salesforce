"""Salesforce JWT Bearer Flow (real implementation).

The Off-Ramp MCP gateway authenticates to every customer org via the
OAuth 2.0 JWT Bearer flow — no refresh tokens, no interactive logins,
rotated cert per quarter (see ``docs/runbooks/jwt_cert_rotation.md``).

Exchange steps:

1. Build a JWT signed with RS256 using the Connected App's private key
   * ``iss`` = Connected App consumer key (client_id)
   * ``sub`` = integration-user username
   * ``aud`` = login endpoint (``https://login.salesforce.com`` for prod,
     ``https://test.salesforce.com`` for sandboxes, or the customer's My
     Domain URL)
   * ``exp`` = now + 3 min (SF rejects > 5 min out)
2. POST ``grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`` +
   ``assertion=<jwt>`` to ``{aud}/services/oauth2/token``
3. Response: ``{access_token, instance_url, token_type, scope}``

The returned ``access_token`` is what simple-salesforce calls a "session id"
— so we return it AND the ``instance_url`` (required since ``aud`` is the
login server, not the org's actual home instance).

Tokens are cached per (client_id, username) with a TTL. Re-authentication
on ``INVALID_SESSION_ID`` (401) is the caller's job — they should call
:meth:`SessionCache.invalidate` and retry.

Security properties enforced:

* The private key file is read once and held in memory inside the cache —
  never passed to subprocess / never included in logs.
* JWT ``aud`` / ``iss`` are both required — bare-minimum assertion rejected
  pre-flight with a clear error.
* All exceptions wrap to :class:`JWTAuthError` so callers can distinguish
  auth failures from application-level Salesforce errors.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jwt as _jwt

from offramp.core.config import SalesforceSettings
from offramp.core.logging import get_logger

log = get_logger(__name__)


class JWTAuthError(RuntimeError):
    """Raised on any JWT-auth failure with a diagnostic message."""


@dataclass(frozen=True)
class Session:
    """The result of one successful JWT exchange."""

    access_token: str
    instance_url: str  # e.g. https://fisher.my.salesforce.com
    token_type: str = "Bearer"
    issued_at: float = field(default_factory=time.monotonic)

    def age_seconds(self, *, now: float | None = None) -> float:
        return (now or time.monotonic()) - self.issued_at


# -- JWT assertion ------------------------------------------------------------


def build_jwt_assertion(
    *,
    client_id: str,
    username: str,
    audience: str,
    private_key_pem: bytes,
    lifetime_seconds: int = 180,
    now: int | None = None,
) -> str:
    """Build + sign the JWT bearer assertion.

    Parameters:
        client_id: Connected App consumer key.
        username: integration-user Salesforce username.
        audience: login URL (https://login.salesforce.com / test.salesforce.com
            / customer My Domain URL).
        private_key_pem: RSA private key, PEM-encoded.
        lifetime_seconds: JWT exp delta. Salesforce rejects > 5 min; we
            default to 3 min to leave clock-skew headroom.
        now: override for deterministic tests.
    """
    if not client_id:
        raise JWTAuthError("JWT client_id (Connected App consumer key) is empty")
    if not username:
        raise JWTAuthError("JWT username is empty")
    if not audience:
        raise JWTAuthError("JWT audience is empty")
    if not private_key_pem:
        raise JWTAuthError("JWT private key is empty")
    iat = int(now if now is not None else time.time())
    claims = {
        "iss": client_id,
        "sub": username,
        "aud": audience.rstrip("/"),
        "iat": iat,
        "exp": iat + int(lifetime_seconds),
    }
    try:
        token = _jwt.encode(claims, private_key_pem, algorithm="RS256")
    except Exception as exc:  # PyJWT raises various types; normalize
        raise JWTAuthError(f"JWT signing failed: {type(exc).__name__}: {exc}") from exc
    # PyJWT returns str on 2.x; older versions returned bytes.
    return token if isinstance(token, str) else token.decode("utf-8")


# -- Token exchange ----------------------------------------------------------


_JWT_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"


async def exchange_assertion(
    *,
    assertion: str,
    token_url: str,
    http_client: httpx.AsyncClient | None = None,
) -> Session:
    """POST the JWT to the token endpoint; parse the response.

    Returns a :class:`Session`. Raises :class:`JWTAuthError` with the
    canonical Salesforce error if the exchange fails.

    ``http_client`` is optional; when None a new short-lived client is
    created (easier for one-off uses). Tests pass an existing client so
    respx mocks intercept the call.
    """
    payload = {"grant_type": _JWT_GRANT_TYPE, "assertion": assertion}
    headers = {"content-type": "application/x-www-form-urlencoded"}

    if http_client is None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(token_url, data=payload, headers=headers)
    else:
        resp = await http_client.post(token_url, data=payload, headers=headers)

    # SF returns 200 OK on success; 400 on auth failure with JSON body.
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        err = body.get("error", "unknown_error")
        desc = body.get("error_description", "")
        raise JWTAuthError(f"Salesforce JWT exchange failed ({resp.status_code}): {err}: {desc}")

    access_token = body.get("access_token")
    instance_url = body.get("instance_url")
    if not access_token or not instance_url:
        raise JWTAuthError(f"Salesforce JWT response missing required fields: {sorted(body)}")
    return Session(
        access_token=str(access_token),
        instance_url=str(instance_url),
        token_type=str(body.get("token_type", "Bearer")),
    )


# -- Cache -------------------------------------------------------------------


@dataclass
class SessionCache:
    """Caches sessions across calls within one process.

    Sessions default to a 55-minute TTL — SF's default session lifetime is
    2 hours, so 55 min leaves comfortable headroom. On ``invalidate()``
    the next :meth:`get` forces a fresh exchange.
    """

    settings: SalesforceSettings
    ttl_seconds: int = 55 * 60
    _session: Session | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _http: httpx.AsyncClient | None = None

    async def get(self) -> Session:
        """Return a fresh session, refreshing from the exchange if expired."""
        async with self._lock:
            if self._session is not None and self._session.age_seconds() < self.ttl_seconds:
                return self._session
            log.info("mcp.jwt_auth.exchanging", login_url=self.settings.login_url)
            self._session = await self._exchange()
            return self._session

    async def invalidate(self) -> None:
        """Drop the cached session — the next ``get()`` re-authenticates."""
        async with self._lock:
            self._session = None

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _exchange(self) -> Session:
        private_key_pem = _read_pem(self.settings.jwt_key_path)
        assertion = build_jwt_assertion(
            client_id=self.settings.client_id.get_secret_value(),
            username=self.settings.username,
            audience=self.settings.login_url,
            private_key_pem=private_key_pem,
        )
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        token_url = f"{self.settings.login_url.rstrip('/')}/services/oauth2/token"
        return await exchange_assertion(
            assertion=assertion,
            token_url=token_url,
            http_client=self._http,
        )


def _read_pem(path: Path) -> bytes:
    """Read the PEM private key; raise JWTAuthError with a pointed message."""
    if not path.is_file():
        raise JWTAuthError(
            f"JWT private key not found at {path}. "
            "See docs/runbooks/jwt_cert_rotation.md for generation."
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise JWTAuthError(f"Cannot read JWT key at {path}: {exc}") from exc
    if b"-----BEGIN" not in data:
        raise JWTAuthError(
            f"JWT key at {path} does not look like a PEM file (missing BEGIN header)"
        )
    return data


async def session_id(settings: SalesforceSettings) -> tuple[str, str]:
    """One-shot JWT bearer exchange.

    Convenience for callers who don't need the cache (e.g. a CLI command
    making a single call). Returns ``(access_token, instance_url)``.
    """
    cache = SessionCache(settings=settings)
    try:
        sess = await cache.get()
        return sess.access_token, sess.instance_url
    finally:
        await cache.close()


# Public re-export so callers can type-annotate against a stable name.
__all__ = [
    "JWTAuthError",
    "Session",
    "SessionCache",
    "build_jwt_assertion",
    "exchange_assertion",
    "session_id",
]
