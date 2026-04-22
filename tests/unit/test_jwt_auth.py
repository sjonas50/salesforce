"""Salesforce JWT Bearer flow — real crypto + mocked token endpoint.

Tests exercise the actual RS256 signing path with an ephemeral RSA keypair
generated per-test, then decode the JWT to verify its claims. The token
exchange uses ``respx`` to intercept the HTTP call so no network traffic
is required.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import jwt as _jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from offramp.core.config import SalesforceSettings
from offramp.mcp.jwt_auth import (
    JWTAuthError,
    SessionCache,
    build_jwt_assertion,
    exchange_assertion,
    session_id,
)

# -- Test fixtures ----------------------------------------------------------


def _rsa_keypair() -> tuple[bytes, Any]:
    """Generate an ephemeral 2048-bit RSA keypair for signing tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, key.public_key()


def _settings_with_key(pem_path: Path) -> SalesforceSettings:
    return SalesforceSettings(
        org_alias="test_org",
        login_url="https://login.salesforce.com",
        client_id=SecretStr("CONNECTED_APP_CONSUMER_KEY"),
        username="integration@example.com",
        jwt_key_path=pem_path,
        api_version="66.0",
    )


# -- build_jwt_assertion ----------------------------------------------------


def test_jwt_assertion_carries_required_claims() -> None:
    pem, public_key = _rsa_keypair()
    now = 1_700_000_000
    token = build_jwt_assertion(
        client_id="3MVG9ABC",
        username="user@example.com",
        audience="https://login.salesforce.com",
        private_key_pem=pem,
        lifetime_seconds=180,
        now=now,
    )
    # Disable exp verification on decode — the test fixes ``now`` to a
    # deterministic past timestamp to assert the claim shape, not validity.
    decoded = _jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        audience="https://login.salesforce.com",
        options={"verify_exp": False},
    )
    assert decoded["iss"] == "3MVG9ABC"
    assert decoded["sub"] == "user@example.com"
    assert decoded["aud"] == "https://login.salesforce.com"
    assert decoded["iat"] == now
    assert decoded["exp"] == now + 180


def test_jwt_assertion_strips_trailing_slash_from_audience() -> None:
    pem, public_key = _rsa_keypair()
    token = build_jwt_assertion(
        client_id="id",
        username="u",
        audience="https://login.salesforce.com/",
        private_key_pem=pem,
    )
    decoded = _jwt.decode(
        token, public_key, algorithms=["RS256"], audience="https://login.salesforce.com"
    )
    assert decoded["aud"] == "https://login.salesforce.com"


def test_jwt_assertion_rejects_empty_client_id() -> None:
    pem, _ = _rsa_keypair()
    with pytest.raises(JWTAuthError, match="client_id"):
        build_jwt_assertion(
            client_id="",
            username="u",
            audience="https://x",
            private_key_pem=pem,
        )


def test_jwt_assertion_rejects_empty_username() -> None:
    pem, _ = _rsa_keypair()
    with pytest.raises(JWTAuthError, match="username"):
        build_jwt_assertion(client_id="c", username="", audience="a", private_key_pem=pem)


def test_jwt_assertion_rejects_empty_audience() -> None:
    pem, _ = _rsa_keypair()
    with pytest.raises(JWTAuthError, match="audience"):
        build_jwt_assertion(client_id="c", username="u", audience="", private_key_pem=pem)


def test_jwt_assertion_rejects_bad_pem_key() -> None:
    with pytest.raises(JWTAuthError, match="signing failed"):
        build_jwt_assertion(
            client_id="c",
            username="u",
            audience="https://x",
            private_key_pem=b"not a real key",
        )


# -- exchange_assertion -----------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_happy_path() -> None:
    token_url = "https://login.salesforce.com/services/oauth2/token"
    with respx.mock(base_url="https://login.salesforce.com") as mock:
        mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "00D000.mock-session",
                    "instance_url": "https://fisher.my.salesforce.com",
                    "token_type": "Bearer",
                    "scope": "full refresh_token",
                },
            )
        )
        sess = await exchange_assertion(assertion="jwt-str", token_url=token_url)
    assert sess.access_token == "00D000.mock-session"
    assert sess.instance_url == "https://fisher.my.salesforce.com"
    assert sess.token_type == "Bearer"


@pytest.mark.asyncio
async def test_exchange_invalid_grant_raises_with_diagnostic() -> None:
    token_url = "https://login.salesforce.com/services/oauth2/token"
    with respx.mock(base_url="https://login.salesforce.com") as mock:
        mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "user hasn't approved this consumer",
                },
            )
        )
        with pytest.raises(JWTAuthError, match=r"invalid_grant.*hasn't approved"):
            await exchange_assertion(assertion="bad", token_url=token_url)


@pytest.mark.asyncio
async def test_exchange_missing_fields_in_response_raises() -> None:
    token_url = "https://login.salesforce.com/services/oauth2/token"
    with respx.mock(base_url="https://login.salesforce.com") as mock:
        # Server returns 200 but omits instance_url — a malformed response.
        mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "x", "token_type": "Bearer"},
            )
        )
        with pytest.raises(JWTAuthError, match="missing required fields"):
            await exchange_assertion(assertion="jwt", token_url=token_url)


@pytest.mark.asyncio
async def test_exchange_non_json_error_body() -> None:
    """HTML 500 error pages shouldn't crash the parser."""
    token_url = "https://login.salesforce.com/services/oauth2/token"
    with respx.mock(base_url="https://login.salesforce.com") as mock:
        mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                500,
                text="<html><body>Internal Server Error</body></html>",
            )
        )
        with pytest.raises(JWTAuthError, match="500"):
            await exchange_assertion(assertion="jwt", token_url=token_url)


# -- SessionCache -----------------------------------------------------------


@pytest.mark.asyncio
async def test_session_cache_exchanges_once_then_serves_cached(tmp_path: Path) -> None:
    pem, _ = _rsa_keypair()
    pem_path = tmp_path / "sf_jwt.pem"
    pem_path.write_bytes(pem)
    settings = _settings_with_key(pem_path)

    with respx.mock(base_url=settings.login_url) as mock:
        route = mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "A",
                    "instance_url": "https://fisher.my.salesforce.com",
                    "token_type": "Bearer",
                },
            )
        )
        cache = SessionCache(settings=settings)
        try:
            s1 = await cache.get()
            s2 = await cache.get()  # hits cache, no new HTTP call
        finally:
            await cache.close()
    assert s1 is s2
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_session_cache_refreshes_after_invalidate(tmp_path: Path) -> None:
    pem, _ = _rsa_keypair()
    pem_path = tmp_path / "sf_jwt.pem"
    pem_path.write_bytes(pem)
    settings = _settings_with_key(pem_path)

    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "access_token": "session_v1",
                    "instance_url": "https://fisher.my.salesforce.com",
                },
            ),
            httpx.Response(
                200,
                json={
                    "access_token": "session_v2",
                    "instance_url": "https://fisher.my.salesforce.com",
                },
            ),
        ]
    )

    with respx.mock(base_url=settings.login_url) as mock:
        mock.post("/services/oauth2/token").mock(side_effect=lambda request: next(responses))
        cache = SessionCache(settings=settings)
        try:
            s1 = await cache.get()
            assert s1.access_token == "session_v1"
            await cache.invalidate()
            s2 = await cache.get()
            assert s2.access_token == "session_v2"
        finally:
            await cache.close()


@pytest.mark.asyncio
async def test_session_cache_refreshes_after_ttl(tmp_path: Path) -> None:
    pem, _ = _rsa_keypair()
    pem_path = tmp_path / "sf_jwt.pem"
    pem_path.write_bytes(pem)
    settings = _settings_with_key(pem_path)

    responses = iter(
        [
            httpx.Response(200, json={"access_token": "v1", "instance_url": "https://x"}),
            httpx.Response(200, json={"access_token": "v2", "instance_url": "https://x"}),
        ]
    )

    with respx.mock(base_url=settings.login_url) as mock:
        mock.post("/services/oauth2/token").mock(side_effect=lambda request: next(responses))
        cache = SessionCache(settings=settings, ttl_seconds=0)  # immediately expire
        try:
            s1 = await cache.get()
            # TTL=0 means the cached entry is stale on the very next call.
            s2 = await cache.get()
        finally:
            await cache.close()
    assert s1.access_token == "v1"
    assert s2.access_token == "v2"


@pytest.mark.asyncio
async def test_session_cache_concurrent_gets_exchange_once(tmp_path: Path) -> None:
    """N concurrent get() calls trigger exactly one HTTP exchange."""
    import asyncio

    pem, _ = _rsa_keypair()
    pem_path = tmp_path / "sf_jwt.pem"
    pem_path.write_bytes(pem)
    settings = _settings_with_key(pem_path)

    with respx.mock(base_url=settings.login_url) as mock:
        route = mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "only_once", "instance_url": "https://x"},
            )
        )
        cache = SessionCache(settings=settings)
        try:
            results = await asyncio.gather(*(cache.get() for _ in range(10)))
        finally:
            await cache.close()
    assert route.call_count == 1
    assert {r.access_token for r in results} == {"only_once"}


@pytest.mark.asyncio
async def test_session_cache_missing_key_raises_clear_error() -> None:
    settings = SalesforceSettings(
        org_alias="t",
        login_url="https://login.salesforce.com",
        client_id=SecretStr("id"),
        username="u",
        jwt_key_path=Path("/does/not/exist.pem"),
    )
    cache = SessionCache(settings=settings)
    try:
        with pytest.raises(JWTAuthError, match="not found"):
            await cache.get()
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_session_cache_non_pem_file_raises_clear_error(tmp_path: Path) -> None:
    bogus = tmp_path / "not_a_pem.txt"
    bogus.write_text("this is not a PEM file")
    settings = SalesforceSettings(
        org_alias="t",
        login_url="https://login.salesforce.com",
        client_id=SecretStr("id"),
        username="u",
        jwt_key_path=bogus,
    )
    cache = SessionCache(settings=settings)
    try:
        with pytest.raises(JWTAuthError, match="PEM"):
            await cache.get()
    finally:
        await cache.close()


# -- session_id convenience fn ----------------------------------------------


@pytest.mark.asyncio
async def test_session_id_one_shot(tmp_path: Path) -> None:
    pem, _ = _rsa_keypair()
    pem_path = tmp_path / "k.pem"
    pem_path.write_bytes(pem)
    settings = _settings_with_key(pem_path)

    with respx.mock(base_url=settings.login_url) as mock:
        mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "single-shot-session",
                    "instance_url": "https://fisher.my.salesforce.com",
                },
            )
        )
        access, instance = await session_id(settings)
    assert access == "single-shot-session"
    assert instance == "https://fisher.my.salesforce.com"


# -- Age tracking -----------------------------------------------------------


def test_session_age_seconds() -> None:
    from offramp.mcp.jwt_auth import Session

    past = time.monotonic() - 100
    s = Session(access_token="x", instance_url="y", issued_at=past)
    assert s.age_seconds() >= 100
