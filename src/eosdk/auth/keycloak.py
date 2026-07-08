"""Keycloak JWT credential provider (SPEC §6.4).

Owns the whole token lifecycle: password and device-flow login, transparent
access-token refresh with a 30 s expiry leeway, an on-disk cache (mode 0600,
keyed by profile) shared between processes, and the 401 → one forced refresh →
retry → ``AuthError`` behaviour, implemented as an ``httpx.Auth`` so every
service using this provider gets it for free.

The access token is persisted alongside the refresh token (the SPEC mandates
only the latter); without it every short-lived CLI invocation would pay a
refresh round-trip.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from eosdk.discovery.oidc import fetch_oidc_endpoints
from eosdk.exceptions import AuthError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from eosdk.discovery.oidc import OidcEndpoints
    from eosdk.transport import Transport

EXPIRY_LEEWAY = 30.0  # seconds: treat tokens expiring sooner than this as expired
LOGIN_HINT = "run `eo auth login`"


@dataclass
class TokenState:
    access_token: str | None = None
    access_expires_at: float = 0.0
    refresh_token: str | None = None
    refresh_expires_at: float | None = None

    def access_valid(self, now: float) -> bool:
        return self.access_token is not None and self.access_expires_at - now > EXPIRY_LEEWAY

    def refresh_valid(self, now: float) -> bool:
        if self.refresh_token is None:
            return False
        return self.refresh_expires_at is None or self.refresh_expires_at - now > EXPIRY_LEEWAY


def default_token_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "eosdk" / "tokens"


class TokenCache:
    """On-disk token store: ``<dir>/<profile>.json``, dir 0700, file 0600."""

    def __init__(self, directory: Path | None = None, *, profile: str = "default") -> None:
        self._dir = directory or default_token_dir()
        self._path = self._dir / f"{profile}.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> TokenState | None:
        try:
            data = json.loads(self._path.read_text())
            return TokenState(**data)
        except (OSError, ValueError, TypeError):
            return None

    def save(self, state: TokenState) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(0o700)
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(asdict(state), fh)
            os.replace(tmp_name, self._path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


@dataclass(frozen=True)
class DeviceCodeInfo:
    verification_uri: str
    verification_uri_complete: str | None
    user_code: str


@dataclass(frozen=True)
class AuthStatus:
    profile: str
    realm: str
    logged_in: bool
    access_valid_until: float | None
    refresh_valid_until: float | None


class _BearerAuth(httpx.Auth):
    """Attach the JWT; on 401 force one refresh and retry once (SPEC §6.4).

    Without a session the request goes out anonymously — public services
    (e.g. the CDSE STAC catalogue) work without login; a 401 then raises the
    login hint instead of failing before the request is even attempted.
    """

    requires_response_body = False

    def __init__(self, provider: KeycloakAuth) -> None:
        self._provider = provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        anonymous = False
        try:
            request.headers["Authorization"] = f"Bearer {self._provider.access_token()}"
        except AuthError:
            anonymous = True
        response = yield request
        if response.status_code == 401:
            if anonymous:
                raise AuthError(
                    f"this service requires a session; {LOGIN_HINT}",
                    realm=self._provider.realm,
                    profile=self._provider.profile,
                )
            request.headers["Authorization"] = f"Bearer {self._provider.force_refresh()}"
            response = yield request
            if response.status_code == 401:
                raise AuthError(
                    "request kept failing with HTTP 401 after a forced token refresh",
                    realm=self._provider.realm,
                    profile=self._provider.profile,
                )


class KeycloakAuth:
    """JWT credentials provider backed by a Keycloak realm."""

    def __init__(
        self,
        *,
        url: str,
        realm: str,
        client_id: str,
        transport: Transport,
        profile: str = "default",
        cache_dir: Path | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.realm = realm
        self.profile = profile
        self._url = url
        self._client_id = client_id
        self._transport = transport
        self._now = now
        self._sleep = sleep
        self._cache = TokenCache(cache_dir, profile=profile)
        self._state = TokenState()
        self._oidc: OidcEndpoints | None = None
        self._lock = threading.Lock()

    # -- OIDC endpoints ------------------------------------------------------

    def _endpoints(self) -> OidcEndpoints:
        if self._oidc is None:
            self._oidc = fetch_oidc_endpoints(self._transport, self._url, self.realm)
        return self._oidc

    # -- token grants --------------------------------------------------------

    def _token_request(self, data: dict[str, str]) -> TokenState:
        response = self._transport.request(
            "POST",
            self._endpoints().token_endpoint,
            service="keycloak",
            data={"client_id": self._client_id, **data},
        )
        payload: dict[str, Any] = response.json() if response.content else {}
        if response.status_code != 200:
            error = payload.get("error", f"HTTP {response.status_code}")
            description = payload.get("error_description", "")
            raise AuthError(
                f"token request failed: {error} {description}".strip(),
                realm=self.realm,
                profile=self.profile,
            )
        now = self._now()
        refresh_expires = payload.get("refresh_expires_in")
        return TokenState(
            access_token=payload["access_token"],
            access_expires_at=now + float(payload.get("expires_in", 0)),
            refresh_token=payload.get("refresh_token"),
            refresh_expires_at=(
                now + float(refresh_expires) if refresh_expires not in (None, 0) else None
            ),
        )

    def login(self, username: str, password: str) -> None:
        """Resource-owner password grant."""
        state = self._token_request(
            {"grant_type": "password", "username": username, "password": password}
        )
        with self._lock:
            self._state = state
            self._cache.save(state)

    def login_device(
        self,
        on_prompt: Callable[[DeviceCodeInfo], None],
        *,
        poll_timeout: float = 300.0,
    ) -> None:
        """OAuth 2.0 device flow for headless CLI use."""
        device_endpoint = self._endpoints().device_authorization_endpoint
        if device_endpoint is None:
            raise AuthError(
                "this Keycloak realm does not advertise a device authorization endpoint",
                realm=self.realm,
                profile=self.profile,
            )
        response = self._transport.request(
            "POST", device_endpoint, service="keycloak", data={"client_id": self._client_id}
        )
        if response.status_code != 200:
            raise AuthError(
                f"device authorization failed with HTTP {response.status_code}",
                realm=self.realm,
                profile=self.profile,
            )
        grant = response.json()
        on_prompt(
            DeviceCodeInfo(
                verification_uri=grant["verification_uri"],
                verification_uri_complete=grant.get("verification_uri_complete"),
                user_code=grant["user_code"],
            )
        )
        interval = float(grant.get("interval", 5))
        deadline = self._now() + min(poll_timeout, float(grant.get("expires_in", poll_timeout)))
        while True:
            self._sleep(interval)
            try:
                state = self._token_request(
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": grant["device_code"],
                    }
                )
            except AuthError as exc:
                message = str(exc)
                if "authorization_pending" in message:
                    pass
                elif "slow_down" in message:
                    interval += 5
                else:
                    raise
                if self._now() >= deadline:
                    raise AuthError(
                        "device flow timed out before the login was approved",
                        realm=self.realm,
                        profile=self.profile,
                    ) from exc
                continue
            break
        with self._lock:
            self._state = state
            self._cache.save(state)

    def logout(self) -> None:
        with self._lock:
            self._state = TokenState()
            self._cache.clear()

    # -- token access --------------------------------------------------------

    def access_token(self) -> str:
        """Return a valid access token, refreshing transparently (SPEC §6.4)."""
        with self._lock:
            now = self._now()
            if self._state.access_valid(now):
                assert self._state.access_token is not None
                return self._state.access_token
            cached = self._cache.load()
            if cached is not None:
                if cached.access_valid(now):
                    self._state = cached
                    assert cached.access_token is not None
                    return cached.access_token
                if cached.refresh_valid(now) and not self._state.refresh_valid(now):
                    self._state = cached
            return self._refresh_locked()

    def force_refresh(self) -> str:
        with self._lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> str:
        """Refresh-token grant; caller must hold the lock."""
        if not self._state.refresh_valid(self._now()):
            cached = self._cache.load()
            if cached is not None and cached.refresh_valid(self._now()):
                self._state = cached
            else:
                raise AuthError(
                    f"no valid session; {LOGIN_HINT}", realm=self.realm, profile=self.profile
                )
        assert self._state.refresh_token is not None
        try:
            state = self._token_request(
                {"grant_type": "refresh_token", "refresh_token": self._state.refresh_token}
            )
        except AuthError as exc:
            if "invalid_grant" in str(exc):
                raise AuthError(
                    f"session expired; {LOGIN_HINT}", realm=self.realm, profile=self.profile
                ) from exc
            raise
        self._state = state
        self._cache.save(state)
        assert state.access_token is not None
        return state.access_token

    def status(self) -> AuthStatus:
        state = self._cache.load() or self._state
        now = self._now()
        return AuthStatus(
            profile=self.profile,
            realm=self.realm,
            logged_in=state.refresh_valid(now) or state.access_valid(now),
            access_valid_until=state.access_expires_at if state.access_token else None,
            refresh_valid_until=state.refresh_expires_at,
        )

    def httpx_auth(self) -> httpx.Auth:
        return _BearerAuth(self)
