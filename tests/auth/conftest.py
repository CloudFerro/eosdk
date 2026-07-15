import json
from pathlib import Path

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent.parent / "fixtures"
KEYCLOAK = "https://auth.example.eu"
REALM = "eodata"
TOKEN_URL = f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token"
DEVICE_URL = f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/auth/device"
OIDC_URL = f"{KEYCLOAK}/realms/{REALM}/.well-known/openid-configuration"


@pytest.fixture
def oidc_document() -> dict[str, object]:
    return json.loads((FIXTURES / "keycloak_openid_configuration.json").read_text())


@pytest.fixture
def mock_oidc(oidc_document: dict[str, object]) -> respx.Router:
    with respx.mock(assert_all_called=False) as router:
        router.get(OIDC_URL).mock(return_value=httpx.Response(200, json=oidc_document))
        yield router


def token_response(
    *,
    access: str = "AT",
    refresh: str | None = "RT",
    expires_in: int = 300,
    refresh_expires_in: int = 1800,
) -> httpx.Response:
    payload: dict[str, object] = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }
    if refresh is not None:
        payload["refresh_token"] = refresh
        payload["refresh_expires_in"] = refresh_expires_in
    return httpx.Response(200, json=payload)


def oauth_error(error: str, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": error, "error_description": error})
