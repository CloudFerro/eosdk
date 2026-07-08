# Fixture payload shapes are authored from the plan, not recorded from the real
# Keys Manager yet.  UNVERIFIED-FIXTURE: re-record against staging at step 2.12.

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk.auth.s3_keys import S3KeysProvider
from eosdk.exceptions import AuthError
from eosdk.transport import RetryPolicy, Transport

BASE = "https://keys.example.eu/api"
KEYS_URL = f"{BASE}/v1/keys"


class FakeAuth:
    def httpx_auth(self) -> httpx.Auth:
        class _A(httpx.Auth):
            def auth_flow(self, request):  # type: ignore[no-untyped-def]
                request.headers["Authorization"] = "Bearer JWT"
                yield request

        return _A()


def created(key_id: str = "kid-1", label: str | None = "my-pipeline") -> dict[str, Any]:
    return {
        "key_id": key_id,
        "access_key": f"AK-{key_id}",
        "secret_key": f"SECRET-{key_id}",
        "label": label,
        "created_at": "2026-07-08T09:00:00Z",
    }


def listing(*key_ids: str) -> dict[str, Any]:
    return {
        "keys": [
            {"key_id": kid, "access_key": f"AK-{kid}", "label": "my-pipeline"} for kid in key_ids
        ]
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
    def test_create_pins_v1_and_bearer(self, provider: S3KeysProvider) -> None:
        mock = respx.post(KEYS_URL).mock(return_value=httpx.Response(200, json=created()))
        credentials = provider.create(label="my-pipeline")
        assert credentials.key_id == "kid-1"
        assert credentials.require_secret() == "SECRET-kid-1"
        request = mock.calls.last.request
        assert "/v1/" in str(request.url)
        assert request.headers["Authorization"] == "Bearer JWT"

    @respx.mock
    def test_list_has_no_secrets(self, provider: S3KeysProvider) -> None:
        respx.get(KEYS_URL).mock(return_value=httpx.Response(200, json=listing("kid-1")))
        (entry,) = provider.list()
        assert entry.secret_key is None
        with pytest.raises(AuthError, match="no secret"):
            entry.require_secret()

    @respx.mock
    def test_revoke(self, provider: S3KeysProvider) -> None:
        mock = respx.delete(f"{KEYS_URL}/kid-1").mock(return_value=httpx.Response(204))
        provider.revoke("kid-1")
        assert mock.call_count == 1

    @respx.mock
    def test_http_error_raises_auth_error(self, provider: S3KeysProvider) -> None:
        respx.post(KEYS_URL).mock(return_value=httpx.Response(403, text="forbidden"))
        with pytest.raises(AuthError, match="403"):
            provider.create()


class TestLabeledReuse:
    @respx.mock
    def test_get_or_create_creates_once_then_reuses(self, provider: S3KeysProvider) -> None:
        create_mock = respx.post(KEYS_URL).mock(return_value=httpx.Response(200, json=created()))
        respx.get(KEYS_URL).mock(return_value=httpx.Response(200, json=listing("kid-1")))

        first = provider.get_or_create("my-pipeline")
        second = provider.get_or_create("my-pipeline")
        assert create_mock.call_count == 1  # second call served from the secret store
        assert first.key_id == second.key_id
        assert second.require_secret() == "SECRET-kid-1"
        assert second.access_key == "AK-kid-1"

    @respx.mock
    def test_revoked_out_of_band_recreates(self, provider: S3KeysProvider) -> None:
        create_mock = respx.post(KEYS_URL).mock(
            side_effect=[
                httpx.Response(200, json=created("kid-1")),
                httpx.Response(200, json=created("kid-2")),
            ]
        )
        respx.get(KEYS_URL).mock(return_value=httpx.Response(200, json=listing("kid-2")))
        provider.get_or_create("my-pipeline")
        renewed = provider.get_or_create("my-pipeline")
        assert create_mock.call_count == 2
        assert renewed.key_id == "kid-2"

    def test_secret_store_file_permissions(self, provider: S3KeysProvider, tmp_path: Path) -> None:
        import stat

        with respx.mock:
            respx.post(KEYS_URL).mock(return_value=httpx.Response(200, json=created()))
            provider.create(label="my-pipeline")
        store_file = tmp_path / "s3keys" / "test.json"
        assert store_file.is_file()
        assert stat.S_IMODE(store_file.stat().st_mode) == 0o600


class TestEphemeral:
    @respx.mock
    def test_revokes_on_exit(self, provider: S3KeysProvider) -> None:
        respx.post(KEYS_URL).mock(return_value=httpx.Response(200, json=created("kid-e", None)))
        revoke_mock = respx.delete(f"{KEYS_URL}/kid-e").mock(return_value=httpx.Response(204))
        with provider.ephemeral() as credentials:
            assert credentials.key_id == "kid-e"
        assert revoke_mock.call_count == 1

    @respx.mock
    def test_revokes_on_exception(self, provider: S3KeysProvider) -> None:
        respx.post(KEYS_URL).mock(return_value=httpx.Response(200, json=created("kid-e", None)))
        revoke_mock = respx.delete(f"{KEYS_URL}/kid-e").mock(return_value=httpx.Response(204))
        with pytest.raises(RuntimeError), provider.ephemeral():
            raise RuntimeError("boom")
        assert revoke_mock.call_count == 1


class TestSecrecy:
    @respx.mock
    def test_secret_never_in_repr_or_str(self, provider: S3KeysProvider) -> None:
        respx.post(KEYS_URL).mock(return_value=httpx.Response(200, json=created()))
        credentials = provider.create(label="my-pipeline")
        assert "SECRET-kid-1" not in repr(credentials)
        assert "SECRET-kid-1" not in str(credentials)
        assert "SECRET-kid-1" not in credentials.model_dump_json()
