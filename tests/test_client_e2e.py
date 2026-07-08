"""Phase-1 exit criterion: search -> download via Zipper, library surface.

One respx fixture set: Keycloak OIDC + token endpoint, STAC landing page +
two search pages, Zipper $value bodies.
"""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk import Client
from eosdk.exceptions import ConfigError

FIXTURES = Path(__file__).parent / "fixtures"

KEYCLOAK = "https://auth.example.eu"
CATALOGUE = "https://catalogue.example.eu/stac"
ZIPPER = "https://zipper.example.eu"
TOKEN_URL = f"{KEYCLOAK}/realms/eodata/protocol/openid-connect/token"
PAYLOAD = b"zipped product bytes " * 128


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    config = tmp_path / "config.toml"
    config.write_text(
        'default_profile = "test"\n'
        "[profiles.test]\n"
        f'catalogue_stac = "{CATALOGUE}"\n'
        f'zipper = "{ZIPPER}"\n'
        f'keycloak = "{KEYCLOAK}"\n'
    )
    with Client(
        profile="test",
        cwd=tmp_path,
        user_config=config,
        token_cache_dir=tmp_path / "tokens",
    ) as c:
        yield c


def stac_item(uuid: str, name: str) -> dict[str, Any]:
    item = json.loads((FIXTURES / "stac_item_s2.json").read_text())
    item["id"] = name
    item["properties"]["eodata:uuid"] = uuid
    item["assets"]["PRODUCT"]["file:checksum"] = "d510" + hashlib.md5(PAYLOAD).hexdigest()
    return item


@pytest.fixture
def platform_mocks() -> Iterator[respx.Router]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{KEYCLOAK}/realms/eodata/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json=json.loads((FIXTURES / "keycloak_openid_configuration.json").read_text()),
            )
        )
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "JWT-AT",
                    "expires_in": 300,
                    "refresh_token": "JWT-RT",
                    "refresh_expires_in": 1800,
                },
            )
        )
        router.get(CATALOGUE).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "Catalog",
                    "id": "root",
                    "conformsTo": [
                        "https://api.stacspec.org/v1.0.0/core",
                        "https://api.stacspec.org/v1.0.0/item-search",
                        "https://api.stacspec.org/v1.0.0/item-search#query",
                    ],
                    "links": [{"rel": "search", "href": f"{CATALOGUE}/search", "method": "POST"}],
                },
            )
        )
        page2 = f"{CATALOGUE}/search?token=2"
        router.post(f"{CATALOGUE}/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "numberMatched": 2,
                    "features": [stac_item("uuid-a", "PRODUCT_A")],
                    "links": [{"rel": "next", "href": page2, "method": "GET"}],
                },
            )
        )
        router.get(page2).mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": [stac_item("uuid-b", "PRODUCT_B")],
                    "links": [],
                },
            )
        )
        for uuid in ("uuid-a", "uuid-b"):
            router.get(f"{ZIPPER}/odata/v1/Products({uuid})/$value").mock(
                return_value=httpx.Response(
                    200,
                    content=PAYLOAD,
                    headers={"Content-Length": str(len(PAYLOAD))},
                )
            )
        yield router


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


def test_download_via_exos_not_yet(client: Client) -> None:
    from eosdk.exceptions import UnsupportedCapability
    from eosdk.models import Product

    with pytest.raises(UnsupportedCapability, match="exos"):
        client.download(Product(id="x", name="X"), target=".", via="exos")


def test_odata_protocol_honest_error(client: Client) -> None:
    with pytest.raises(ConfigError, match="later release"):
        client.search(collection="S1", protocol="odata")
