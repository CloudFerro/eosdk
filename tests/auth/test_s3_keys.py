# Fixture shapes follow the CloudFerro S3 Keys Manager OpenAPI spec
# (https://s3-keys-manager.cloudferro.com/api/user/docs, v1.8.x):
# GET/POST /credentials, DELETE /credentials/access_id/{access_id}.

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk.auth.s3_keys import S3KeysProvider
from eosdk.exceptions import AuthError
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
        import stat

        with respx.mock:
            respx.post(CREDENTIALS_URL).mock(return_value=httpx.Response(200, json=created()))
            provider.create(label="my-pipeline")
        store_file = tmp_path / "s3keys" / "test.json"
        assert store_file.is_file()
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
