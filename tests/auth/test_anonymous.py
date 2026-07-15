"""Anonymous access: public services work without a session (CDSE STAC)."""

from pathlib import Path

import httpx
import pytest
import respx

from eosdk.auth.keycloak import KeycloakAuth
from eosdk.exceptions import AuthError
from eosdk.transport import RetryPolicy, Transport

SERVICE = "https://public.example.eu/search"


@pytest.fixture
def auth(tmp_path: Path) -> KeycloakAuth:
    transport = Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None)
    yield KeycloakAuth(
        url="https://auth.example.eu",
        realm="eodata",
        client_id="eosdk",
        transport=transport,
        profile="test",
        cache_dir=tmp_path / "tokens",
    )
    transport.close()


@respx.mock
def test_no_session_sends_anonymously(auth: KeycloakAuth) -> None:
    mock = respx.get(SERVICE).mock(return_value=httpx.Response(200, text="ok"))
    with httpx.Client(auth=auth.httpx_auth()) as client:
        response = client.get(SERVICE)
    assert response.status_code == 200
    assert "Authorization" not in mock.calls.last.request.headers


@respx.mock
def test_401_without_session_raises_login_hint(auth: KeycloakAuth) -> None:
    respx.get(SERVICE).mock(return_value=httpx.Response(401))
    with (
        httpx.Client(auth=auth.httpx_auth()) as client,
        pytest.raises(AuthError, match="eo auth login"),
    ):
        client.get(SERVICE)
