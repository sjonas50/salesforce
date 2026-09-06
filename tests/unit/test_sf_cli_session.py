from __future__ import annotations

import json

import pytest

from offramp.mcp.jwt_auth import JWTAuthError, sf_cli_session


def test_sf_cli_session_parses_org_display() -> None:
    out = json.dumps(
        {
            "status": 0,
            "result": {"accessToken": "00Dxx!abc", "instanceUrl": "https://x.my.salesforce.com/"},
        }
    )
    sess = sf_cli_session("scratch", runner=lambda argv: out)
    assert sess.access_token == "00Dxx!abc" and sess.instance_url == "https://x.my.salesforce.com"


def test_sf_cli_session_errors_are_pointed() -> None:
    with pytest.raises(JWTAuthError, match="no accessToken"):
        sf_cli_session(
            "scratch", runner=lambda argv: json.dumps({"status": 1, "message": "No authorization"})
        )
    with pytest.raises(JWTAuthError, match="non-JSON"):
        sf_cli_session("scratch", runner=lambda argv: "not json")

    def boom(argv: list[str]) -> str:
        raise OSError("sf not found")

    with pytest.raises(JWTAuthError, match="sf CLI session lookup failed"):
        sf_cli_session("scratch", runner=boom)
