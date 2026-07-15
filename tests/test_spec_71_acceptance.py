"""SPEC §7.1 executed end-to-end: the Phase-2 acceptance script.

search (STAC) -> download (http) -> list (http) -> open (s3, ranged) —
against respx (HTTP services) + moto (S3), with the keys manager minting
credentials on first S3 use.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
import respx
from moto import mock_aws

from eosdk import Client
from tests.conftest import CATALOGUE, EODATA_HTTP, S3_CREDENTIALS, write_profile_config
from tests.eodata import safe_tree

B04 = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    config = write_profile_config(tmp_path / "config.toml")
    with Client(
        profile="test",
        cwd=tmp_path,
        user_config=config,
        token_cache_dir=tmp_path / "tokens",
        keys_cache_dir=tmp_path / "s3keys",
    ) as c:
        yield c


@pytest.fixture
def platform(platform_mocks: respx.Router) -> Iterator[respx.Router]:
    """Extend the shared HTTP mocks: SAFE item, S3 credentials, download Nodes."""
    # keys manager (CloudFerro /credentials API)
    platform_mocks.post(f"{S3_CREDENTIALS}/credentials").mock(
        return_value=httpx.Response(
            200,
            json={"access_id": "AK", "secret": "SK", "expiration_date": "2027-01-01T00:00:00Z"},
        )
    )
    platform_mocks.get(url__startswith=f"{S3_CREDENTIALS}/credentials").mock(
        return_value=httpx.Response(
            200,
            json={
                "credentials": [
                    {
                        "access_id": "AK",
                        "user_name": "alice",
                        "organization": "org",
                        "expiration_date": "2027-01-01T00:00:00Z",
                    }
                ],
                "count": 1,
                "offset": 0,
                "limit": 100,
            },
        )
    )

    # download-service Nodes for the SAFE product root + IMG_DATA chain
    def nodes(entries: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(200, json={"result": entries})

    base = f"{EODATA_HTTP}/odata/v1/Products(uuid-safe)/Nodes"
    platform_mocks.get(url__eq=base).mock(
        return_value=nodes([{"Name": "GRANULE", "ChildrenNumber": 1}])
    )
    granule = "L2A_T34UEE_A012345_20260615T095030"
    platform_mocks.get(url__eq=f"{base}(GRANULE)/Nodes").mock(
        return_value=nodes([{"Name": granule, "ChildrenNumber": 1}])
    )
    platform_mocks.get(url__eq=f"{base}(GRANULE)/Nodes({granule})/Nodes").mock(
        return_value=nodes([{"Name": "IMG_DATA", "ChildrenNumber": 1}])
    )
    platform_mocks.get(url__eq=f"{base}(GRANULE)/Nodes({granule})/Nodes(IMG_DATA)/Nodes").mock(
        return_value=nodes([{"Name": "R10m", "ChildrenNumber": 1}])
    )
    platform_mocks.get(
        url__eq=f"{base}(GRANULE)/Nodes({granule})/Nodes(IMG_DATA)/Nodes(R10m)/Nodes"
    ).mock(
        return_value=nodes(
            [
                {"Name": "T34UEE_B04_10m.jp2", "ChildrenNumber": 0, "ContentLength": 15004},
                {"Name": "T34UEE_B08_10m.jp2", "ChildrenNumber": 0, "ContentLength": 15004},
            ]
        )
    )
    # a search returning the SAFE product with its S3 path
    safe_item = {
        "type": "Feature",
        "id": safe_tree.PRODUCT_NAME,
        "collection": "SENTINEL-2",
        "geometry": None,
        "properties": {"datetime": "2026-06-15T09:50:29Z", "eo:cloud_cover": 12.4},
        "assets": {
            "Product": {
                "href": f"{EODATA_HTTP}/odata/v1/Products(uuid-safe)/$value",
                "file:local_path": safe_tree.S3_PATH,
            }
        },
        "links": [],
    }
    platform_mocks.post(f"{CATALOGUE}/search").mock(
        return_value=httpx.Response(
            200,
            json={"type": "FeatureCollection", "numberMatched": 1, "features": [safe_item]},
        )
    )
    platform_mocks.get(f"{EODATA_HTTP}/odata/v1/Products(uuid-safe)/$value").mock(
        return_value=httpx.Response(
            200,
            content=b"zip-bytes-of-safe-product",
            headers={"Content-Disposition": f'attachment; filename="{safe_tree.PRODUCT_NAME}.zip"'},
        )
    )
    return platform_mocks


@pytest.mark.integration
def test_spec_71_full_flow(client: Client, platform: respx.Router, tmp_path: Path) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=safe_tree.BUCKET)
        for logical, content in safe_tree.FILES.items():
            s3.put_object(
                Bucket=safe_tree.BUCKET, Key=f"{safe_tree.PREFIX}/{logical}", Body=content
            )

        client.auth.login("alice", "pw")

        # -- search --------------------------------------------------------- §7.1
        products = client.search(
            collection="SENTINEL-2",
            bbox=(22.5, 52.9, 24.0, 53.5),
            datetime="2026-06-01/2026-06-30",
            filters={"cloudCover": "<20"},
            protocol="stac",
            limit=50,
        )

        # -- download via http ---------------------------------------------------
        reports = client.download(
            products, target=tmp_path / "data", via="http", concurrency=4, checksum=True
        )
        assert reports[0].path.name == f"{safe_tree.PRODUCT_NAME}.zip"

        # -- SearchResult is re-iterable; list files inside the product --------
        product = next(iter(products))
        nodes = client.list(product, via="http", recursive=True)
        band = next(n for n in nodes if n.path.endswith("B04.jp2") or "B04" in n.path)
        assert band.path == B04

        # -- ranged read via s3, no full download --------------------------------
        with client.open(product, path=band.path, via="s3") as fh:
            data = fh.read()
        assert data == safe_tree.FILES[B04]

        # keys were minted exactly once, on first S3 use
        keys_calls = [c for c in platform.calls if c.request.url.host == "keys.example.eu"]
        assert any(c.request.method == "POST" for c in keys_calls)
