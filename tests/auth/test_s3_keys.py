# Fixture shapes follow the CloudFerro S3 Keys Manager OpenAPI spec
# (https://s3-keys-manager.cloudferro.com/api/user/docs, v1.8.x):
# GET/POST /credentials, DELETE /credentials/access_id/{access_id}.

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk.auth.s3_keys import (
    S3Credentials,
    S3KeysProvider,
    _SecretStore,
    default_s3keys_dir,
)
from eosdk.exceptions import AuthError, S3KeyLimitReached
from eosdk.transport import RetryPolicy, Transport

BASE = "https://s3-keys-manager.example.eu/api/user"
CREDENTIALS_URL = f"{BASE}/credentials"
SECRET = "SUPER-SECRET-KEY-MATERIAL"


class FakeAuth:
    def httpx_auth(self) -> httpx.Auth:
        class _A(httpx.Auth):
            def auth_flow(self, request):  # type: ignore[no-untyped-def]
                request.headers["Authorization"] = "Bearer JWT"
                yield request

        return _A()


def created(access_id: str = "AKIA001") -> dict[str, Any]:
    return {
        "access_id": access_id,
        "secret": SECRET,
        "expiration_date": "2027-07-08T00:00:00Z",
    }


def listing(*access_ids: str, expired: bool = False) -> dict[str, Any]:
    expiry = "2020-01-01T00:00:00Z" if expired else "2027-07-08T00:00:00Z"
    credentials = [
        {
            "access_id": aid,
            "user_name": "alice",
            "organization": "org-1",
            "expiration_date": expiry,
        }
        for aid in access_ids
    ]
    return {
        "credentials": credentials,
        "count": len(credentials),
        "offset": 0,
        "limit": 100,
    }


@pytest.fixture
def provider(tmp_path: Path) -> Iterator[S3KeysProvider]:
    with Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None) as transport:
        yield S3KeysProvider(
            auth=FakeAuth(),
            base_url=BASE,
            transport=transport,
            profile="test",
            cache_dir=tmp_path / "s3keys",
        )


class TestS3Credentials:
    def test_key_id_is_the_access_key(self) -> None:
        assert S3Credentials(access_key="AKIA001").key_id == "AKIA001"

    def test_no_expiration_date_never_expires(self) -> None:
        assert S3Credentials(access_key="A").expired() is False
        assert S3Credentials(access_key="A", expiration_date="").expired() is False

    def test_unparseable_expiration_treated_as_live(self) -> None:
        # A malformed date from the service must not silently discard a key.
        creds = S3Credentials(access_key="A", expiration_date="not-a-date")
        assert creds.expired() is False

    def test_naive_expiration_assumed_utc(self) -> None:
        creds = S3Credentials(access_key="A", expiration_date="2026-01-01T00:00:00")
        after = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        before = dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)
        assert creds.expired(now=after) is True
        assert creds.expired(now=before) is False


class TestDefaultS3KeysDir:
    def test_honours_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert default_s3keys_dir() == tmp_path / "xdg" / "eosdk" / "s3keys"

    def test_falls_back_to_home_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert default_s3keys_dir() == Path.home() / ".config" / "eosdk" / "s3keys"


class TestSecretStore:
    def test_failed_save_keeps_old_entries_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        store = _SecretStore(tmp_path, "test")
        store.put("my-pipeline", "AKIA001", "secret")
        broken: Any = {"my-pipeline": {"secret_key": b"bytes are not JSON"}}
        with pytest.raises(TypeError):
            store.save(broken)
        assert store.get("my-pipeline") == {"access_id": "AKIA001", "secret_key": "secret"}
        assert list(tmp_path.glob("*.tmp")) == []  # temp file cleaned up


class TestCreateListRevoke:
    @respx.mock
    def test_create_hits_credentials_with_bearer(self, provider: S3KeysProvider) -> None:
        mock = respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(200, json=created()))
        credentials = provider.create(label="my-pipeline")
        assert credentials.access_key == "AKIA001"
        assert credentials.require_secret() == SECRET
        request = mock.calls.last.request
        assert request.url.path.endswith("/api/user/credentials")
        assert request.headers["Authorization"] == "Bearer JWT"

    @respx.mock
    def test_list_paginates_and_has_no_secrets(self, provider: S3KeysProvider) -> None:
        page1 = {
            "credentials": listing("AKIA001")["credentials"],
            "count": 2,
            "offset": 0,
            "limit": 1,
        }
        page2 = {
            "credentials": listing("AKIA002")["credentials"],
            "count": 2,
            "offset": 1,
            "limit": 1,
        }
        respx.get(url__startswith=CREDENTIALS_URL).mock(
            side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
        )
        entries = provider.list()
        assert [e.access_key for e in entries] == ["AKIA001", "AKIA002"]
        assert all(e.secret_key is None for e in entries)
        with pytest.raises(AuthError, match="creation time"):
            entries[0].require_secret()

    @respx.mock
    def test_revoke_uses_access_id_path(self, provider: S3KeysProvider) -> None:
        mock = respx.delete(f"{CREDENTIALS_URL}/access_id/AKIA001").mock(
            return_value=httpx.Response(204)
        )
        provider.revoke("AKIA001")
        assert mock.call_count == 1

    @respx.mock
    def test_http_error_raises_auth_error(self, provider: S3KeysProvider) -> None:
        respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(403, text="forbidden"))
        with pytest.raises(AuthError, match="403"):
            provider.create()


class TestKeyLimit:
    """The service caps concurrent key pairs; create() maps the refusal."""

    @respx.mock
    @pytest.mark.parametrize(
        ("status", "detail"),
        [
            (403, {"detail": "Max number of credentials reached."}),  # actual v1.8 response
            (403, {"detail": "Maximum number of credentials exceeded"}),
            (400, {"detail": "credentials limit reached"}),
            (409, {"detail": "too many credentials for user"}),
        ],
    )
    def test_create_at_cap_raises_key_limit(
        self, provider: S3KeysProvider, status: int, detail: dict[str, str]
    ) -> None:
        respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(status, json=detail))
        with pytest.raises(S3KeyLimitReached, match="revoke") as excinfo:
            provider.create()
        assert detail["detail"] in str(excinfo.value)  # server wording preserved

    @respx.mock
    def test_key_limit_is_not_an_auth_error(self, provider: S3KeysProvider) -> None:
        respx.post(CREDENTIALS_URL).mock(
            return_value=httpx.Response(403, json={"detail": "Max number of credentials reached."})
        )
        with pytest.raises(S3KeyLimitReached):
            provider.create()
        with pytest.raises(S3KeyLimitReached):  # not swallowed by `except AuthError`
            try:
                provider.create()
            except AuthError:
                pytest.fail("key limit must not surface as a generic AuthError")

    @respx.mock
    def test_non_limit_4xx_still_auth_error(self, provider: S3KeysProvider) -> None:
        respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(403, text="forbidden"))
        with pytest.raises(AuthError, match="403"):
            provider.create()

    @respx.mock
    def test_limit_wording_on_other_routes_stays_auth_error(self, provider: S3KeysProvider) -> None:
        # Only POST /credentials can hit the cap; a validation error elsewhere
        # that happens to say "limit" must not be misread as the key cap.
        respx.delete(f"{CREDENTIALS_URL}/access_id/AKIA001").mock(
            return_value=httpx.Response(400, json={"detail": "limit malformed"})
        )
        with pytest.raises(AuthError, match="400"):
            provider.revoke("AKIA001")

    @respx.mock
    def test_limit_wording_on_5xx_stays_auth_error(self, provider: S3KeysProvider) -> None:
        # The cap answers 400/403/409; a 5xx that happens to echo the wording
        # is a server fault, not the key cap.
        respx.post(CREDENTIALS_URL).mock(
            return_value=httpx.Response(500, json={"detail": "max number of credentials reached"})
        )
        with pytest.raises(AuthError, match="500"):
            provider.create()

    @respx.mock
    def test_get_or_create_reuses_without_create_at_cap(self, provider: S3KeysProvider) -> None:
        # Seed the label, then put the account "at the cap": reuse must succeed
        # because get_or_create never POSTs while the labeled key is live.
        respx.post(CREDENTIALS_URL).mock(
            side_effect=[
                httpx.Response(200, json=created()),
                httpx.Response(403, json={"detail": "Max number of credentials reached."}),
            ]
        )
        respx.get(url__startswith=CREDENTIALS_URL).mock(
            return_value=httpx.Response(200, json=listing("AKIA001"))
        )
        provider.get_or_create("my-pipeline")
        reused = provider.get_or_create("my-pipeline")
        assert reused.access_key == "AKIA001"

    @respx.mock
    def test_ephemeral_at_cap_raises_before_yield(self, provider: S3KeysProvider) -> None:
        respx.post(CREDENTIALS_URL).mock(
            return_value=httpx.Response(403, json={"detail": "Max number of credentials reached."})
        )
        with pytest.raises(S3KeyLimitReached), provider.ephemeral():
            pytest.fail("context body must not run when creation is refused")


class TestLabeledReuse:
    """The service has no labels; reuse is client-side via the secret store."""

    @respx.mock
    def test_get_or_create_creates_once_then_reuses(self, provider: S3KeysProvider) -> None:
        create_mock = respx.post(CREDENTIALS_URL).mock(
            return_value=httpx.Response(200, json=created())
        )
        respx.get(url__startswith=CREDENTIALS_URL).mock(
            return_value=httpx.Response(200, json=listing("AKIA001"))
        )
        first = provider.get_or_create("my-pipeline")
        second = provider.get_or_create("my-pipeline")
        assert create_mock.call_count == 1  # second call served from the secret store
        assert first.access_key == second.access_key
        assert second.require_secret() == SECRET

    @respx.mock
    def test_revoked_out_of_band_recreates(self, provider: S3KeysProvider) -> None:
        create_mock = respx.post(CREDENTIALS_URL).mock(
            side_effect=[
                httpx.Response(200, json=created("AKIA001")),
                httpx.Response(200, json=created("AKIA002")),
            ]
        )
        respx.get(url__startswith=CREDENTIALS_URL).mock(
            return_value=httpx.Response(200, json=listing("AKIA002"))
        )
        provider.get_or_create("my-pipeline")
        renewed = provider.get_or_create("my-pipeline")
        assert create_mock.call_count == 2
        assert renewed.access_key == "AKIA002"

    @respx.mock
    def test_expired_key_recreated(self, provider: S3KeysProvider) -> None:
        create_mock = respx.post(CREDENTIALS_URL).mock(
            side_effect=[
                httpx.Response(200, json=created("AKIA001")),
                httpx.Response(200, json=created("AKIA002")),
            ]
        )
        respx.get(url__startswith=CREDENTIALS_URL).mock(
            return_value=httpx.Response(200, json=listing("AKIA001", expired=True))
        )
        provider.get_or_create("my-pipeline")
        renewed = provider.get_or_create("my-pipeline")
        assert create_mock.call_count == 2
        assert renewed.access_key == "AKIA002"

    def test_secret_store_file_permissions(self, provider: S3KeysProvider, tmp_path: Path) -> None:
        import os
        import stat

        with respx.mock:
            respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(200, json=created()))
            provider.create(label="my-pipeline")
        store_file = tmp_path / "s3keys" / "test.json"
        assert store_file.is_file()
        if os.name != "nt":  # Windows has no POSIX modes
            assert stat.S_IMODE(store_file.stat().st_mode) == 0o600


class TestEphemeral:
    @respx.mock
    def test_revokes_on_exit(self, provider: S3KeysProvider) -> None:
        respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(200, json=created("AKIA-E")))
        revoke_mock = respx.delete(f"{CREDENTIALS_URL}/access_id/AKIA-E").mock(
            return_value=httpx.Response(204)
        )
        with provider.ephemeral() as credentials:
            assert credentials.access_key == "AKIA-E"
        assert revoke_mock.call_count == 1

    @respx.mock
    def test_revokes_on_exception(self, provider: S3KeysProvider) -> None:
        respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(200, json=created("AKIA-E")))
        revoke_mock = respx.delete(f"{CREDENTIALS_URL}/access_id/AKIA-E").mock(
            return_value=httpx.Response(204)
        )
        with pytest.raises(RuntimeError), provider.ephemeral():
            raise RuntimeError("boom")
        assert revoke_mock.call_count == 1


class TestSecrecy:
    @respx.mock
    def test_secret_never_in_repr_or_str(self, provider: S3KeysProvider) -> None:
        respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(200, json=created()))
        credentials = provider.create(label="my-pipeline")
        assert SECRET not in repr(credentials)
        assert SECRET not in str(credentials)
        assert SECRET not in credentials.model_dump_json()
