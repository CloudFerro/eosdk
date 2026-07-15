"""Phase-1 exit criterion: search -> download via HTTP, library surface.

Uses the shared platform mocks from conftest (Keycloak OIDC + token endpoint,
STAC landing page + two search pages, download $value bodies).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import respx

from eosdk import Client
from eosdk.exceptions import ConfigError
from tests.conftest import PAYLOAD, write_profile_config


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    config = write_profile_config(tmp_path / "config.toml")
    with Client(
        profile="test",
        cwd=tmp_path,
        user_config=config,
        token_cache_dir=tmp_path / "tokens",
    ) as c:
        yield c


@pytest.mark.integration
def test_search_then_download_end_to_end(
    client: Client, platform_mocks: respx.Router, tmp_path: Path
) -> None:
    client.auth.login("alice", "pw")

    products = client.search(
        collection="SENTINEL-2",
        bbox=(22.5, 52.9, 24.0, 53.5),
        datetime="2026-06-01/2026-06-30",
        filters={"cloudCover": "<20"},
        limit=50,
    )
    assert len(products) == 2

    reports = client.download(products, target=tmp_path / "data", via="http", concurrency=2)
    assert sorted(r.path.name for r in reports) == ["PRODUCT_A.zip", "PRODUCT_B.zip"]
    for report in reports:
        assert report.path.read_bytes() == PAYLOAD
        assert report.checksum_verified is True

    # SearchResult is re-iterable: downloading did not consume it.
    assert [p.name for p in products] == ["PRODUCT_A", "PRODUCT_B"]

    # every authenticated request carried the JWT
    download_calls = [
        c for c in platform_mocks.calls if c.request.url.host == "download.example.eu"
    ]
    assert download_calls
    assert all(c.request.headers["Authorization"] == "Bearer JWT-AT" for c in download_calls)


@respx.mock
def test_search_pending_endpoint_with_unreachable_discovery(tmp_path: Path) -> None:
    import httpx

    from eosdk.exceptions import EndpointUnreachable

    respx.get("https://platform.example.eu/.well-known/eo-services.json").mock(
        side_effect=httpx.ConnectError("down")
    )
    with (
        Client(
            platform="https://platform.example.eu",
            cwd=tmp_path,
            user_config=tmp_path / "missing.toml",
            discovery_cache_dir=tmp_path / "discovery",
        ) as client,
        # pending endpoint triggers lazy discovery; unreachable -> actionable error
        pytest.raises(EndpointUnreachable, match="EOSDK_DISCOVERY_URL"),
    ):
        client.search(collection="SENTINEL-2")


def test_open_via_http_is_unsupported_capability(client: Client) -> None:
    from eosdk.exceptions import UnsupportedCapability
    from eosdk.models import Product

    with pytest.raises(UnsupportedCapability, match="open"):
        client.open(Product(id="x", name="X"), path="a/b.jp2", via="http")


def test_unknown_via_rejected(client: Client) -> None:
    from eosdk.exceptions import ConfigError
    from eosdk.models import Product

    with pytest.raises(ConfigError, match="ftp") as exc_info:
        client.download(Product(id="x", name="X"), target=".", via="ftp")
    assert "http" in str(exc_info.value)
    assert "s3" in str(exc_info.value)


def test_old_via_names_rejected_with_hint(client: Client) -> None:
    """The pre-rename backend names must fail as unknown, pointing at http | s3."""
    from eosdk.exceptions import ConfigError
    from eosdk.models import Product

    for legacy in ("zipper", "exos"):
        with pytest.raises(ConfigError, match=f"unknown download backend '{legacy}'"):
            client.download(Product(id="x", name="X"), target=".", via=legacy)


def test_odata_protocol_requires_endpoint(client: Client) -> None:
    # profile has no catalogue_odata pinned -> actionable ConfigError, no HTTP
    with pytest.raises(ConfigError, match="EOSDK_CATALOGUE_ODATA_URL"):
        client.search(collection="S1", protocol="odata")


def test_unknown_protocol_rejected(client: Client) -> None:
    with pytest.raises(ConfigError, match="unknown catalogue protocol"):
        client.search(collection="S1", protocol="carrier-pigeon")


class TestCatalogueDelegation:
    """get/collections/queryables route through _catalogue, incl. plugin protocols."""

    @pytest.fixture
    def plugin_client(self, client: Client, monkeypatch: pytest.MonkeyPatch) -> Client:
        from types import SimpleNamespace

        from eosdk.models import Collection, Product, Queryable
        from eosdk.plugins import PluginSpec

        class FakeCatalogue:
            def get(self, product_id: str) -> Product:
                return Product(id=product_id, name=f"FAKE_{product_id}")

            def collections(self) -> list[Collection]:
                return [Collection(id="FAKE-COLLECTION", title="Fake")]

            def queryables(self, collection: str) -> list[Queryable]:
                return [Queryable(name="cloudCover", type="number", raw={"for": collection})]

        plugin = PluginSpec(name="fake", kind="catalogue", factory=lambda c: FakeCatalogue())
        monkeypatch.setattr(
            "eosdk.plugins.entry_points",
            lambda group: [SimpleNamespace(name="fake", value="fake", load=lambda: plugin)],
        )
        return client

    def test_get_dispatches_to_plugin_catalogue(self, plugin_client: Client) -> None:
        product = plugin_client.get("uuid-x", protocol="fake")
        assert product.id == "uuid-x"
        assert product.name == "FAKE_uuid-x"

    def test_collections_dispatches_to_plugin_catalogue(self, plugin_client: Client) -> None:
        (collection,) = plugin_client.collections(protocol="fake")
        assert collection.id == "FAKE-COLLECTION"

    def test_queryables_dispatches_to_plugin_catalogue(self, plugin_client: Client) -> None:
        (queryable,) = plugin_client.queryables("SENTINEL-2", protocol="fake")
        assert queryable.name == "cloudCover"
        assert queryable.raw == {"for": "SENTINEL-2"}

    def test_downloader_plugin_never_serves_catalogue_protocol(
        self, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        from eosdk.plugins import PluginSpec

        plugin = PluginSpec(name="dl", kind="downloader", factory=lambda c: object())
        monkeypatch.setattr(
            "eosdk.plugins.entry_points",
            lambda group: [SimpleNamespace(name="dl", value="dl", load=lambda: plugin)],
        )
        with pytest.raises(ConfigError, match="unknown catalogue protocol"):
            client.collections(protocol="dl")
