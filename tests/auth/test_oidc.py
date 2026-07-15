import httpx
import pytest
import respx

from eosdk.discovery.oidc import fetch_oidc_endpoints
from eosdk.exceptions import AuthError, EndpointUnreachable
from eosdk.transport import RetryPolicy, Transport
from tests.auth.conftest import DEVICE_URL, KEYCLOAK, OIDC_URL, REALM, TOKEN_URL


@pytest.fixture
def transport() -> Transport:
    with Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None) as t:
        yield t


def test_resolves_endpoints(mock_oidc: respx.Router, transport: Transport) -> None:
    endpoints = fetch_oidc_endpoints(transport, KEYCLOAK, REALM)
    assert endpoints.token_endpoint == TOKEN_URL
    assert endpoints.device_authorization_endpoint == DEVICE_URL
    assert endpoints.issuer == f"{KEYCLOAK}/realms/{REALM}"


@respx.mock
def test_missing_token_endpoint(transport: Transport) -> None:
    respx.get(OIDC_URL).mock(return_value=httpx.Response(200, json={"issuer": "x"}))
    with pytest.raises(AuthError, match="token_endpoint"):
        fetch_oidc_endpoints(transport, KEYCLOAK, REALM)


@respx.mock
def test_http_error(transport: Transport) -> None:
    respx.get(OIDC_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(AuthError, match="404"):
        fetch_oidc_endpoints(transport, KEYCLOAK, REALM)


@respx.mock
def test_unreachable(transport: Transport) -> None:
    respx.get(OIDC_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(EndpointUnreachable, match="keycloak"):
        fetch_oidc_endpoints(transport, KEYCLOAK, REALM)
