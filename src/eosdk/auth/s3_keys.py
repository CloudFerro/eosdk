"""S3 Keys Manager client (SPEC §6.4), aligned to the CloudFerro API.

Real API (https://s3-keys-manager.cloudferro.com/api/user/docs, v1.8.x):

- ``GET  /credentials?offset&limit`` -> ``{credentials: [{access_id, user_name,
  organization, expiration_date}], count, offset, limit}`` — **no secrets**.
- ``POST /credentials`` (optional ``{expiration_date}``) ->
  ``{access_id, secret, expiration_date}`` — the only time the secret is shown.
- ``DELETE /credentials/access_id/{access_id}``
- ``PATCH  /credentials/access_id/{access_id}/secret_key`` — rotate the secret.

The service has **no label concept**, so the SPEC's labeled-reuse policy is
implemented client-side: labels map to ``access_id`` + secret in a per-profile
on-disk store (mode 0600). The configured base URL already contains the API
root (e.g. ``.../api/user``); routes carry no version segment.

Authenticated with the Keycloak JWT; produces S3 credentials for the Exos
backend, which never sees Keycloak tokens.

The service caps the number of concurrent key pairs per account. ``create()``
maps the cap refusal to :class:`~eosdk.exceptions.S3KeyLimitReached`; labeled
reuse (``get_or_create``) and revoke-on-exit ephemeral keys exist precisely to
stay under that cap.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, SecretStr

from eosdk.exceptions import AuthError, S3KeyLimitReached
from eosdk.transport import route

if TYPE_CHECKING:
    from collections.abc import Iterator

    from eosdk.auth.base import CredentialsProvider
    from eosdk.transport import Transport


class S3Credentials(BaseModel):
    access_key: str  # the API's `access_id`; doubles as AWS_ACCESS_KEY_ID
    secret_key: SecretStr | None = None  # None on listings: never returned there
    expiration_date: str | None = None
    label: str | None = None  # client-side only; the service has no labels
    organization: str | None = None

    @property
    def key_id(self) -> str:  # canonical identifier for revoke/display
        return self.access_key

    def require_secret(self) -> str:
        if self.secret_key is None:
            raise AuthError(
                f"no secret available for S3 key {self.access_key!r}; "
                "the keys manager only reveals secrets at creation time — create a new key"
            )
        return self.secret_key.get_secret_value()

    def expired(self, *, now: dt.datetime | None = None) -> bool:
        if not self.expiration_date:
            return False
        try:
            expires = dt.datetime.fromisoformat(self.expiration_date.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        return expires <= (now or dt.datetime.now(dt.timezone.utc))


# The service caps concurrent key pairs per account; POST /credentials at the
# cap answers HTTP 403 with "Max number of credentials reached." (observed on
# v1.8). The wording is not a documented contract, so also accept nearby
# phrasings — but only on the create route, where the cap is the only
# limit-shaped refusal possible.
_KEY_LIMIT_MARKERS = (
    "max number of credentials",  # the actual v1.8 message
    "maximum number of credentials",
    "credentials limit",
    "too many credentials",
)


def _is_key_limit(response: Any, detail: str) -> bool:
    if response.status_code not in (400, 403, 409):
        return False
    lowered = detail.lower()
    return any(marker in lowered for marker in _KEY_LIMIT_MARKERS)


def default_s3keys_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "eosdk" / "s3keys"


class _SecretStore:
    """Per-profile on-disk secret cache, keyed by label; file mode 0600."""

    def __init__(self, directory: Path, profile: str) -> None:
        self._path = directory / f"{profile}.json"

    def load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self._path.read_text())
            return dict(data)
        except (OSError, ValueError):
            return {}

    def save(self, entries: dict[str, dict[str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(entries, fh)
            os.replace(tmp_name, self._path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    def get(self, label: str) -> dict[str, str] | None:
        return self.load().get(label)

    def put(self, label: str, access_id: str, secret: str) -> None:
        entries = self.load()
        entries[label] = {"access_id": access_id, "secret_key": secret}
        self.save(entries)

    def drop_key(self, access_id: str) -> None:
        entries = {
            label: entry
            for label, entry in self.load().items()
            if entry.get("access_id") != access_id
        }
        self.save(entries)


class S3KeysProvider:
    """Create/list/revoke S3 key pairs; labeled-reuse policy is client-side."""

    def __init__(
        self,
        *,
        auth: CredentialsProvider,
        base_url: str,
        transport: Transport,
        profile: str = "default",
        cache_dir: Path | None = None,
    ) -> None:
        self._auth = auth
        self._base = base_url
        self._transport = transport
        self._store = _SecretStore(cache_dir or default_s3keys_dir(), profile)

    def _request(self, method: str, template: str, /, **kwargs: Any) -> Any:
        response = self._transport.request(
            method,
            route(self._base, template, **kwargs.pop("route_params", {})),
            service="keys_manager",
            auth=self._auth.httpx_auth(),
            **kwargs,
        )
        if response.status_code >= 400:
            detail = response.text[:200]
            if method == "POST" and template == "credentials" and _is_key_limit(response, detail):
                raise S3KeyLimitReached(detail=detail)
            raise AuthError(
                f"keys manager request failed with HTTP {response.status_code}: {detail}"
            )
        return response.json() if response.content else None

    def create(
        self, label: str | None = None, *, expiration_date: str | None = None
    ) -> S3Credentials:
        """Create a fresh key pair.

        Raises :class:`~eosdk.exceptions.S3KeyLimitReached` when the account's
        cap on concurrent keys is hit; prefer :meth:`get_or_create`, which only
        creates when the label has no live key.
        """
        body = {"expiration_date": expiration_date} if expiration_date else {}
        payload = self._request("POST", "credentials", json=body)
        credentials = S3Credentials(
            access_key=str(payload["access_id"]),
            secret_key=SecretStr(str(payload["secret"])),
            expiration_date=payload.get("expiration_date"),
            label=label,
        )
        if label is not None:
            self._store.put(label, credentials.access_key, credentials.require_secret())
        return credentials

    def list(self) -> list[S3Credentials]:
        """All key pairs (paginated server-side; secrets are never included)."""
        entries: list[S3Credentials] = []
        offset = 0
        while True:
            payload = self._request("GET", "credentials", params={"offset": offset})
            page = payload.get("credentials", [])
            entries.extend(
                S3Credentials(
                    access_key=str(entry["access_id"]),
                    secret_key=None,
                    expiration_date=entry.get("expiration_date"),
                    organization=entry.get("organization"),
                )
                for entry in page
            )
            count = int(payload.get("count", len(entries)))
            offset += len(page)
            if offset >= count or not page:
                break
        return entries

    def revoke(self, access_id: str) -> None:
        self._request(
            "DELETE",
            "credentials/access_id/{access_id}",
            route_params={"access_id": access_id},
        )
        self._store.drop_key(access_id)

    def get_or_create(self, label: str) -> S3Credentials:
        """Labeled-reuse policy (SPEC §6.4 default), implemented client-side."""
        cached = self._store.get(label)
        if cached is not None:
            active = {c.access_key: c for c in self.list()}
            entry = active.get(cached["access_id"])
            if entry is not None and not entry.expired():
                return S3Credentials(
                    access_key=cached["access_id"],
                    secret_key=SecretStr(cached["secret_key"]),
                    expiration_date=entry.expiration_date,
                    label=label,
                )
            self._store.drop_key(cached["access_id"])  # revoked or expired: recreate
        return self.create(label=label)

    @contextmanager
    def ephemeral(self, label_prefix: str = "eosdk-ephemeral") -> Iterator[S3Credentials]:
        """Explicit alternative policy: a fresh key revoked on context exit.

        Counts against the account's key cap while the context is open, so
        entry can raise :class:`~eosdk.exceptions.S3KeyLimitReached`; a key
        leaked by a hard kill must be cleaned up with ``eo keys revoke``.
        """
        credentials = self.create(label=None)
        try:
            yield credentials
        finally:
            self.revoke(credentials.access_key)
