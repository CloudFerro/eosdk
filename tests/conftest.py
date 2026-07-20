"""Shared fixtures: a fully mocked platform (Keycloak + STAC + HTTP download)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"

KEYCLOAK = "https://auth.example.eu"
CATALOGUE = "https://catalogue.example.eu/stac"
EODATA_HTTP = "https://download.example.eu"
TOKEN_URL = f"{KEYCLOAK}/realms/eodata/protocol/openid-connect/token"
PAYLOAD = b"zipped product bytes " * 128


def stac_item(uuid: str, name: str) -> dict[str, Any]:
    item = json.loads((FIXTURES / "stac_item_s2.json").read_text())
    item["id"] = name
    product = item["assets"]["Product"]
    # the UUID lives in the Product asset's download href (real CDSE shape)
    product["href"] = f"{EODATA_HTTP}/odata/v1/Products({uuid})/$value"
    # varint multihash: md5 = code d5 (varint d5 01) + length 10 + digest
    product["file:checksum"] = "d50110" + hashlib.md5(PAYLOAD).hexdigest()
    return item


S3_CREDENTIALS = "https://keys.example.eu/api"
S3_ENDPOINT = "https://s3.us-east-1.amazonaws.com"  # moto intercepts AWS endpoints only


def write_profile_config(path: Path) -> Path:
    path.write_text(
        'default_profile = "test"\n'
        "[profiles.test]\n"
        f'catalogue_stac = "{CATALOGUE}"\n'
        f'eodata_http = "{EODATA_HTTP}"\n'
        f'keycloak = "{KEYCLOAK}"\n'
        'keycloak_realm = "eodata"\n'
        'keycloak_client_id = "eosdk-tests"\n'
        f's3_credentials = "{S3_CREDENTIALS}"\n'
        f's3_endpoint = "{S3_ENDPOINT}"\n'
        's3_region = "us-east-1"\n'
    )
    return path


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
            router.get(f"{EODATA_HTTP}/odata/v1/Products({uuid})/$value").mock(
                return_value=httpx.Response(
                    200,
                    content=PAYLOAD,
                    headers={"Content-Length": str(len(PAYLOAD))},
                )
            )
        yield router
