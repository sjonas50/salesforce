"""SimpleSalesforceBackend integration with the JWT SessionCache.

Exercises the lazy connect path + invalidate_session + concurrent-get
serialization. Uses respx to mock the token endpoint and monkeypatches the
simple_salesforce.Salesforce constructor so we never need the real client.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from offramp.core.config import SalesforceSettings
from offramp.mcp.sf_backend import SimpleSalesforceBackend


def _write_rsa_key(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "sf_jwt.pem"
    path.write_bytes(pem)
    return path


def _settings(pem_path: Path) -> SalesforceSettings:
    return SalesforceSettings(
        org_alias="test_org",
        login_url="https://login.salesforce.com",
        client_id=SecretStr("CONSUMER_KEY"),
        username="integration@example.com",
        jwt_key_path=pem_path,
        api_version="66.0",
    )


class _FakeSalesforce:
    """Stand-in for simple_salesforce.Salesforce — records constructor args."""

    # ClassVar to make the construction counter explicit for ruff.
    construction_count: ClassVar[int] = 0
    last_kwargs: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).construction_count += 1
        type(self).last_kwargs = dict(kwargs)


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    _FakeSalesforce.construction_count = 0
    _FakeSalesforce.last_kwargs = {}


@pytest.fixture
def patched_salesforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch simple_salesforce.Salesforce to the fake."""
    import simple_salesforce

    monkeypatch.setattr(simple_salesforce, "Salesforce", _FakeSalesforce)


@pytest.mark.asyncio
async def test_connect_exchanges_token_then_constructs_client(
    tmp_path: Path, patched_salesforce: None
) -> None:
    settings = _settings(_write_rsa_key(tmp_path))
    with respx.mock(base_url=settings.login_url) as mock:
        route = mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "00D000.test-session",
                    "instance_url": "https://fisher.my.salesforce.com",
                    "token_type": "Bearer",
                },
            )
        )
        backend = SimpleSalesforceBackend(settings=settings, process_id="p1")
        try:
            client = await backend.connect()
        finally:
            await backend.aclose()

    assert isinstance(client, _FakeSalesforce)
    assert route.call_count == 1
    # The client was constructed with the SESSION's instance_url, not the
    # login_url — this is the previously-stubbed bug our implementation fixes.
    assert _FakeSalesforce.last_kwargs["instance_url"] == "https://fisher.my.salesforce.com"
    assert _FakeSalesforce.last_kwargs["session_id"] == "00D000.test-session"
    assert _FakeSalesforce.last_kwargs["version"] == "66.0"


@pytest.mark.asyncio
async def test_connect_is_idempotent(tmp_path: Path, patched_salesforce: None) -> None:
    settings = _settings(_write_rsa_key(tmp_path))
    with respx.mock(base_url=settings.login_url) as mock:
        mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "x", "instance_url": "https://x"},
            )
        )
        backend = SimpleSalesforceBackend(settings=settings, process_id="p1")
        try:
            c1 = await backend.connect()
            c2 = await backend.connect()
        finally:
            await backend.aclose()
    assert c1 is c2
    assert _FakeSalesforce.construction_count == 1


@pytest.mark.asyncio
async def test_concurrent_connects_serialize(tmp_path: Path, patched_salesforce: None) -> None:
    """10 concurrent connect() calls -> exactly one JWT exchange + one client."""
    settings = _settings(_write_rsa_key(tmp_path))
    with respx.mock(base_url=settings.login_url) as mock:
        route = mock.post("/services/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "single", "instance_url": "https://x"},
            )
        )
        backend = SimpleSalesforceBackend(settings=settings, process_id="p1")
        try:
            clients = await asyncio.gather(*(backend.connect() for _ in range(10)))
        finally:
            await backend.aclose()
    assert route.call_count == 1
    assert _FakeSalesforce.construction_count == 1
    assert all(c is clients[0] for c in clients)


@pytest.mark.asyncio
async def test_invalidate_session_triggers_re_auth(
    tmp_path: Path, patched_salesforce: None
) -> None:
    settings = _settings(_write_rsa_key(tmp_path))

    responses = iter(
        [
            httpx.Response(200, json={"access_token": "first", "instance_url": "https://a"}),
            httpx.Response(200, json={"access_token": "second", "instance_url": "https://b"}),
        ]
    )
    with respx.mock(base_url=settings.login_url) as mock:
        mock.post("/services/oauth2/token").mock(side_effect=lambda request: next(responses))
        backend = SimpleSalesforceBackend(settings=settings, process_id="p1")
        try:
            c1 = await backend.connect()
            await backend.invalidate_session()
            c2 = await backend.connect()
        finally:
            await backend.aclose()
    assert c1 is not c2
    assert _FakeSalesforce.construction_count == 2
    assert _FakeSalesforce.last_kwargs["session_id"] == "second"
    assert _FakeSalesforce.last_kwargs["instance_url"] == "https://b"
