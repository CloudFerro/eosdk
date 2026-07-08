import stat
import threading
from pathlib import Path

import httpx
import pytest
import respx

from eosdk.auth.keycloak import (
    EXPIRY_LEEWAY,
    DeviceCodeInfo,
    KeycloakAuth,
    TokenCache,
    TokenState,
)
from eosdk.exceptions import AuthError
from eosdk.transport import RetryPolicy, Transport
from tests.auth.conftest import DEVICE_URL, KEYCLOAK, REALM, TOKEN_URL, oauth_error, token_response


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def auth(tmp_path: Path, clock: Clock) -> KeycloakAuth:
    transport = Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None)
    yield KeycloakAuth(
        url=KEYCLOAK,
        realm=REALM,
        client_id="eosdk",
        transport=transport,
        profile="test",
        cache_dir=tmp_path / "tokens",
        now=clock,
        sleep=clock.sleep,
    )
    transport.close()


class TestLogin:
    def test_password_grant_and_cache_permissions(
        self, auth: KeycloakAuth, mock_oidc: respx.Router, tmp_path: Path
    ) -> None:
        token_mock = mock_oidc.post(TOKEN_URL).mock(return_value=token_response())
        auth.login("alice", "s3cret")

        body = token_mock.calls.last.request.content.decode()
        assert "grant_type=password" in body
        assert "client_id=eosdk" in body
        assert "username=alice" in body

        cache_file = tmp_path / "tokens" / "test.json"
        assert cache_file.is_file()
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(cache_file.parent.stat().st_mode) == 0o700

    def test_bad_credentials(self, auth: KeycloakAuth, mock_oidc: respx.Router) -> None:
        mock_oidc.post(TOKEN_URL).mock(return_value=oauth_error("invalid_grant", 401))
        with pytest.raises(AuthError, match="invalid_grant") as exc_info:
            auth.login("alice", "wrong")
        assert "eodata" in str(exc_info.value)
        assert "test" in str(exc_info.value)


class TestAccessTokenStateMachine:
    def test_valid_in_memory_no_http(self, auth: KeycloakAuth, mock_oidc: respx.Router) -> None:
        mock_oidc.post(TOKEN_URL).mock(return_value=token_response())
        auth.login("alice", "pw")
        calls_after_login = len(mock_oidc.calls)
        assert auth.access_token() == "AT"
        assert len(mock_oidc.calls) == calls_after_login

    def test_valid_on_disk_no_http(
        self, auth: KeycloakAuth, clock: Clock, tmp_path: Path, mock_oidc: respx.Router
    ) -> None:
        cache = TokenCache(tmp_path / "tokens", profile="test")
        cache.save(
            TokenState(access_token="DISK", access_expires_at=clock.now + 300, refresh_token="RT")
        )
        assert auth.access_token() == "DISK"
        assert len(mock_oidc.calls) == 0

    def test_expired_triggers_one_refresh(
        self, auth: KeycloakAuth, clock: Clock, mock_oidc: respx.Router
    ) -> None:
        token_mock = mock_oidc.post(TOKEN_URL).mock(
            return_value=token_response(access="AT2", refresh="RT2")
        )
        auth.login("alice", "pw")
        clock.now += 400  # past expires_in=300
        assert auth.access_token() == "AT2"
        body = token_mock.calls.last.request.content.decode()
        assert "grant_type=refresh_token" in body

    def test_leeway_treats_soon_expiring_as_expired(
        self, auth: KeycloakAuth, clock: Clock, mock_oidc: respx.Router
    ) -> None:
        mock_oidc.post(TOKEN_URL).mock(return_value=token_response(access="FRESH"))
        auth.login("alice", "pw")
        clock.now += 300 - EXPIRY_LEEWAY / 2  # inside leeway window
        assert auth.access_token() == "FRESH"

    def test_refresh_invalid_grant_asks_for_login(
        self, auth: KeycloakAuth, clock: Clock, mock_oidc: respx.Router
    ) -> None:
        route = mock_oidc.post(TOKEN_URL).mock(return_value=token_response())
        auth.login("alice", "pw")
        clock.now += 400
        route.mock(return_value=oauth_error("invalid_grant"))
        with pytest.raises(AuthError, match="eo auth login"):
            auth.access_token()

    def test_empty_cache_asks_for_login(self, auth: KeycloakAuth, mock_oidc: respx.Router) -> None:
        with pytest.raises(AuthError, match="eo auth login"):
            auth.access_token()

    def test_concurrent_expiry_causes_single_refresh(
        self, auth: KeycloakAuth, clock: Clock, mock_oidc: respx.Router
    ) -> None:
        token_mock = mock_oidc.post(TOKEN_URL).mock(return_value=token_response())
        auth.login("alice", "pw")
        clock.now += 400
        calls_before = token_mock.call_count

        threads = [threading.Thread(target=auth.access_token) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert token_mock.call_count == calls_before + 1


class TestBearerAuth401:
    SERVICE = "https://svc.example.eu/data"

    def test_401_refresh_retry_succeeds(self, auth: KeycloakAuth, mock_oidc: respx.Router) -> None:
        mock_oidc.post(TOKEN_URL).mock(return_value=token_response(access="AT2"))
        auth.login("alice", "pw")
        service = mock_oidc.get(self.SERVICE).mock(
            side_effect=[httpx.Response(401), httpx.Response(200, text="ok")]
        )
        with httpx.Client(auth=auth.httpx_auth()) as client:
            response = client.get(self.SERVICE)
        assert response.status_code == 200
        assert service.call_count == 2
        assert service.calls.last.request.headers["Authorization"] == "Bearer AT2"

    def test_persistent_401_raises_auth_error(
        self, auth: KeycloakAuth, mock_oidc: respx.Router
    ) -> None:
        mock_oidc.post(TOKEN_URL).mock(return_value=token_response())
        auth.login("alice", "pw")
        mock_oidc.get(self.SERVICE).mock(return_value=httpx.Response(401))
        with httpx.Client(auth=auth.httpx_auth()) as client, pytest.raises(AuthError):
            client.get(self.SERVICE)


class TestDeviceFlow:
    def device_grant(self) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "DEV123",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://auth.example.eu/device",
                "verification_uri_complete": "https://auth.example.eu/device?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 5,
            },
        )

    def test_pending_then_slow_down_then_success(
        self, auth: KeycloakAuth, clock: Clock, mock_oidc: respx.Router
    ) -> None:
        mock_oidc.post(DEVICE_URL).mock(return_value=self.device_grant())
        mock_oidc.post(TOKEN_URL).mock(
            side_effect=[
                oauth_error("authorization_pending"),
                oauth_error("slow_down"),
                oauth_error("authorization_pending"),
                token_response(access="DEVICE-AT"),
            ]
        )
        prompts: list[DeviceCodeInfo] = []
        auth.login_device(prompts.append)

        assert prompts[0].user_code == "ABCD-EFGH"
        assert prompts[0].verification_uri_complete is not None
        assert clock.slept[:4] == [5, 5, 10, 10]  # slow_down raises the interval
        assert auth.access_token() == "DEVICE-AT"

    def test_expired_token_error_propagates(
        self, auth: KeycloakAuth, mock_oidc: respx.Router
    ) -> None:
        mock_oidc.post(DEVICE_URL).mock(return_value=self.device_grant())
        mock_oidc.post(TOKEN_URL).mock(return_value=oauth_error("expired_token"))
        with pytest.raises(AuthError, match="expired_token"):
            auth.login_device(lambda info: None)

    def test_timeout(self, auth: KeycloakAuth, clock: Clock, mock_oidc: respx.Router) -> None:
        mock_oidc.post(DEVICE_URL).mock(return_value=self.device_grant())
        mock_oidc.post(TOKEN_URL).mock(return_value=oauth_error("authorization_pending"))
        with pytest.raises(AuthError, match="timed out"):
            auth.login_device(lambda info: None, poll_timeout=30)


class TestLogoutAndStatus:
    def test_logout_clears_cache(
        self, auth: KeycloakAuth, tmp_path: Path, mock_oidc: respx.Router
    ) -> None:
        mock_oidc.post(TOKEN_URL).mock(return_value=token_response())
        auth.login("alice", "pw")
        cache_file = tmp_path / "tokens" / "test.json"
        assert cache_file.exists()
        auth.logout()
        assert not cache_file.exists()
        with pytest.raises(AuthError):
            auth.access_token()

    def test_status_contains_no_token_material(
        self, auth: KeycloakAuth, mock_oidc: respx.Router
    ) -> None:
        mock_oidc.post(TOKEN_URL).mock(
            return_value=token_response(access="SECRET-AT", refresh="SECRET-RT")
        )
        auth.login("alice", "pw")
        status = auth.status()
        assert status.logged_in is True
        assert "SECRET" not in repr(status)

    def test_status_logged_out(self, auth: KeycloakAuth) -> None:
        assert auth.status().logged_in is False
