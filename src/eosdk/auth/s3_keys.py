"""S3 Keys Manager client (SPEC §6.4).

Authenticated with the Keycloak JWT; produces S3 credentials for the Exos
backend, which never sees Keycloak tokens. Pins ``/v1`` via ``route()`` —
never in the configured base URL (SPEC §6.3).

The Keys Manager is assumed NOT to return secret keys on list, so
``get_or_create`` caches the secret from ``create`` on disk
(``~/.config/eosdk/s3keys/<profile>.json``, mode 0600, keyed by label).
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, SecretStr

from eosdk.exceptions import AuthError
from eosdk.transport import route

if TYPE_CHECKING:
    from collections.abc import Iterator

    from eosdk.auth.base import CredentialsProvider
    from eosdk.transport import Transport


class S3Credentials(BaseModel):
    key_id: str
    access_key: str
    secret_key: SecretStr | None = None  # None on listings: the service returns no secrets
    label: str | None = None
    created_at: str | None = None

    def require_secret(self) -> str:
        if self.secret_key is None:
            raise AuthError(
                f"no secret available for S3 key {self.key_id!r}; create a new labeled key"
            )
        return self.secret_key.get_secret_value()


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

    def put(self, label: str, key_id: str, access_key: str, secret: str) -> None:
        entries = self.load()
        entries[label] = {"key_id": key_id, "access_key": access_key, "secret_key": secret}
        self.save(entries)

    def drop_key(self, key_id: str) -> None:
        entries = {
            label: entry for label, entry in self.load().items() if entry["key_id"] != key_id
        }
        self.save(entries)


class S3KeysProvider:
    """Create/list/revoke S3 key pairs; labeled-reuse policy by default."""

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
            route(self._base, template, **kwargs.pop("params", {})),
            service="keys_manager",
            auth=self._auth.httpx_auth(),
            **kwargs,
        )
        if response.status_code >= 400:
            raise AuthError(
                f"keys manager request failed with HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response.json() if response.content else None

    def create(self, label: str | None = None) -> S3Credentials:
        payload = self._request("POST", "v1/keys", json={"label": label} if label else {})
        credentials = S3Credentials(
            key_id=str(payload["key_id"]),
            access_key=str(payload["access_key"]),
            secret_key=SecretStr(str(payload["secret_key"])),
            label=payload.get("label", label),
            created_at=payload.get("created_at"),
        )
        if label is not None:
            self._store.put(
                label, credentials.key_id, credentials.access_key, credentials.require_secret()
            )
        return credentials

    def list(self) -> list[S3Credentials]:
        payload = self._request("GET", "v1/keys")
        return [
            S3Credentials(
                key_id=str(entry["key_id"]),
                access_key=str(entry.get("access_key", "")),
                secret_key=None,
                label=entry.get("label"),
                created_at=entry.get("created_at"),
            )
            for entry in payload.get("keys", payload if isinstance(payload, list) else [])
        ]

    def revoke(self, key_id: str) -> None:
        self._request("DELETE", "v1/keys/{key_id}", params={"key_id": key_id})
        self._store.drop_key(key_id)

    def get_or_create(self, label: str) -> S3Credentials:
        """Labeled-reuse policy (SPEC §6.4 default): one key pair per label."""
        cached = self._store.get(label)
        if cached is not None:
            active = {c.key_id for c in self.list()}
            if cached["key_id"] in active:
                return S3Credentials(
                    key_id=cached["key_id"],
                    access_key=cached.get("access_key", cached["key_id"]),
                    secret_key=SecretStr(cached["secret_key"]),
                    label=label,
                )
            self._store.drop_key(cached["key_id"])  # revoked out-of-band: recreate
        return self.create(label=label)

    @contextmanager
    def ephemeral(self, label_prefix: str = "eosdk-ephemeral") -> Iterator[S3Credentials]:
        """Explicit alternative policy: a fresh key revoked on context exit."""
        credentials = self.create(label=None)
        try:
            yield credentials
        finally:
            self.revoke(credentials.key_id)
