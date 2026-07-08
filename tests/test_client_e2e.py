"""Phase-1 exit criterion: search -> download via Zipper, library surface.

Uses the shared platform mocks from conftest (Keycloak OIDC + token endpoint,
STAC landing page + two search pages, Zipper $value bodies).
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

    reports = client.download(products, target=tmp_path / "data", via="zipper", concurrency=2)
    assert sorted(r.path.name for r in reports) == ["PRODUCT_A.zip", "PRODUCT_B.zip"]
    for report in reports:
        assert report.path.read_bytes() == PAYLOAD
        assert report.checksum_verified is True

    # SearchResult is re-iterable: downloading did not consume it.
    assert [p.name for p in products] == ["PRODUCT_A", "PRODUCT_B"]

    # every authenticated request carried the JWT
    zipper_calls = [c for c in platform_mocks.calls if c.request.url.host == "zipper.example.eu"]
    assert zipper_calls
    assert all(c.request.headers["Authorization"] == "Bearer JWT-AT" for c in zipper_calls)


def test_search_pending_endpoint_names_pin(tmp_path: Path) -> None:
    with (
        Client(
            platform="https://platform.example.eu",
            cwd=tmp_path,
            user_config=tmp_path / "missing.toml",
        ) as client,
        pytest.raises(ConfigError, match="EOSDK_CATALOGUE_STAC_URL"),
    ):
        client.search(collection="SENTINEL-2")


def test_download_via_exos_not_yet(tmp_path: Path) -> None:
    from eosdk.exceptions import UnsupportedCapability
    from eosdk.models import Product

    config = write_profile_config(tmp_path / "config.toml")
    with (
        Client(profile="test", cwd=tmp_path, user_config=config) as client,
        pytest.raises(UnsupportedCapability, match="exos"),
    ):
        client.download(Product(id="x", name="X"), target=".", via="exos")


def test_odata_protocol_honest_error(tmp_path: Path) -> None:
    config = write_profile_config(tmp_path / "config.toml")
    with (
        Client(profile="test", cwd=tmp_path, user_config=config) as client,
        pytest.raises(ConfigError, match="later release"),
    ):
        client.search(collection="S1", protocol="odata")
