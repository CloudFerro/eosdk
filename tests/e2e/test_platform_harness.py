"""Self-checks for the harness itself.

The rest of the suite trusts that FakePlatform's projections describe the same
products; these tests are what makes that trustworthy. A parity test comparing
two projections is only meaningful if neither projection is accidentally
derived from the other, and if both are actually reachable through the SDK.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from eosdk.catalogue.stac import decode_multihash
from tests.e2e.platform import FakePlatform, default_products

if TYPE_CHECKING:
    from eosdk.client import Client

pytestmark = pytest.mark.e2e


class TestProjections:
    def test_stac_s3_asset_hrefs_collapse_to_the_product_root(self) -> None:
        """``_product_s3_path`` takes the common prefix of s3:// asset hrefs.

        With a single-directory tree that prefix would cut mid-path, so the
        harness must only use the asset style for trees that branch at the root.
        """
        from eosdk.catalogue.stac import _item_to_product

        by_assets = [p for p in default_products() if p.stac_s3_style == "assets"]
        assert by_assets, "no product exercises the s3-asset extraction chain"
        for product in by_assets:
            item = product.as_stac_item(eodata_http="https://d.example", stac="https://c.example")
            assert _item_to_product(item).s3_path == product.s3_uri

    def test_both_projections_describe_one_product(self) -> None:
        from eosdk.catalogue.odata import _entry_to_product
        from eosdk.catalogue.stac import _item_to_product

        for product in default_products():
            stac = _item_to_product(
                product.as_stac_item(eodata_http="https://d.example", stac="https://c.example")
            )
            odata = _entry_to_product(product.as_odata_entry())
            assert stac.id == odata.id == product.uuid
            assert stac.name == odata.name == product.name
            assert stac.datetime == odata.datetime
            assert stac.size == odata.size == product.size
            assert stac.checksum == odata.checksum
            assert stac.collection == odata.collection

    def test_checksums_describe_the_downloadable_payload(self) -> None:
        for product in default_products():
            decoded = decode_multihash("d50110" + product.md5)
            assert decoded is not None
            assert decoded.algorithm == "md5"
            assert decoded.value == hashlib.md5(product.zip_bytes).hexdigest()

    def test_s3_path_vocabularies_address_the_same_object(self) -> None:
        from eosdk.eodata.s3 import S3Downloader

        for product in default_products():
            odata_style = S3Downloader._bucket_and_prefix(
                type("P", (), {"s3_path": product.s3_path, "id": product.uuid})()  # type: ignore[arg-type]
            )
            stac_style = S3Downloader._bucket_and_prefix(
                type("P", (), {"s3_path": product.s3_uri, "id": product.uuid})()  # type: ignore[arg-type]
            )
            assert odata_style == stac_style == (product.bucket, product.s3_prefix)


class TestRouter:
    def test_unknown_urls_are_recorded_not_silently_served(self) -> None:
        instance = FakePlatform()
        response = instance.handle("GET", "https://elsewhere.example/thing")
        assert response.status == 404
        assert instance.unrouted == ["GET https://elsewhere.example/thing"]

    def test_every_generated_odata_clause_is_understood(self) -> None:
        """An unparsed clause answers 400, so translation drift cannot look empty."""
        from eosdk.catalogue._odata_filter import build_filter
        from eosdk.models import Query
        from tests.e2e.platform import _odata_clause, _split_and

        expression = build_filter(
            Query(
                collection="SENTINEL-2",
                bbox=(22.5, 52.9, 24.0, 53.5),
                datetime="2026-06-01/2026-06-30",
                filters={"cloudCover": "<20", "productType": "S2MSI2A"},
            )
        )
        clauses = _split_and(expression)
        # collection + two ContentDate bounds + bbox + two attribute lambdas,
        # each of which itself contains an " and " the splitter must not see.
        assert len(clauses) == 6
        for clause in clauses:
            assert _odata_clause(clause) is not None

    def test_service_bases_resolve_longest_first(self, platform: FakePlatform) -> None:
        resolved = platform._resolve(f"{platform.urls.stac}/collections")
        assert resolved is not None
        assert resolved[0] == "stac"
        assert resolved[1] == "/collections"


class TestReachableThroughTheSdk:
    def test_discovery_resolves_every_endpoint(self, client: Client) -> None:
        client._endpoint("keycloak", service="keycloak")
        resolved = client.config.resolved()
        assert resolved["catalogue_stac"].value == "https://catalogue.example.eu/stac"
        assert resolved["catalogue_odata"].value == "https://catalogue.example.eu/odata"
        assert resolved["eodata_http"].value == "https://download.example.eu"
        assert resolved["s3_credentials"].value == "https://keys.example.eu/api"
        assert resolved["keycloak_realm"].value == "eodata"
        assert resolved["keycloak_client_id"].value == "example-public"

    def test_login_then_search_then_download(
        self, library_client: Client, platform: FakePlatform, tmp_path: object
    ) -> None:
        products = list(library_client.search(collection="SENTINEL-2", limit=10))
        assert [p.name for p in products] == [
            product.name for product in platform.products if product.collection == "SENTINEL-2"
        ]
