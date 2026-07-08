from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from eosdk.catalogue.odata import ODataCatalogue
from eosdk.catalogue.stac import _item_to_product
from eosdk.exceptions import ProductNotFound
from eosdk.models import Query
from eosdk.transport import RetryPolicy, Transport
from tests.conftest import stac_item

BASE = "https://catalogue.example.eu/odata-svc"
PRODUCTS = f"{BASE}/odata/v1/Products"


def odata_entry(uuid: str, name: str, *, cloud: float | None = 12.4) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "Id": uuid,
        "Name": name,
        "ContentLength": 1207959552,
        "ContentDate": {"Start": "2026-06-15T09:50:29.000Z", "End": "2026-06-15T09:50:29.000Z"},
        "GeoFootprint": {
            "type": "Polygon",
            "coordinates": [[[22.5, 52.9], [24.0, 52.9], [24.0, 53.5], [22.5, 53.5], [22.5, 52.9]]],
        },
        "Checksum": [{"Algorithm": "MD5", "Value": "9e107d9d372bb6826bd81d3542a419d6"}],
        "S3Path": f"/eodata/Sentinel-2/MSI/L2A/2026/06/15/{name}",
        "Collection": {"Name": "SENTINEL-2"},
    }
    if cloud is not None:
        entry["Attributes"] = [{"Name": "cloudCover", "Value": str(cloud), "ValueType": "Double"}]
    return entry


def page(
    entries: list[dict[str, Any]], *, count: int | None = None, next_link: str | None = None
) -> dict[str, Any]:
    doc: dict[str, Any] = {"value": entries}
    if count is not None:
        doc["@odata.count"] = count
    if next_link is not None:
        doc["@odata.nextLink"] = next_link
    return doc


@pytest.fixture
def catalogue() -> Iterator[ODataCatalogue]:
    with Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None) as transport:
        yield ODataCatalogue(BASE, transport=transport)


class TestSearch:
    @respx.mock
    def test_three_page_reiteration_three_calls_total(self, catalogue: ODataCatalogue) -> None:
        # One route serves all pages in sequence (respx matches the path for any
        # query, so per-$skiptoken routes would be shadowed by the first one).
        page2, page3 = f"{PRODUCTS}?$skiptoken=2", f"{PRODUCTS}?$skiptoken=3"
        mock = respx.get(url__startswith=PRODUCTS).mock(
            side_effect=[
                httpx.Response(
                    200, json=page([odata_entry("u1", "P1")], count=3, next_link=page2)
                ),
                httpx.Response(200, json=page([odata_entry("u2", "P2")], next_link=page3)),
                httpx.Response(200, json=page([odata_entry("u3", "P3")])),
            ]
        )
        result = catalogue.search(Query(collection="SENTINEL-2"))
        names = [p.name for p in result]
        assert names == ["P1", "P2", "P3"]
        assert len(result) == 3  # @odata.count
        assert [p.name for p in result] == names  # re-iteration: cached
        assert mock.call_count == 3  # exactly one HTTP call per page, ever
        # follow-up requests hit the advertised nextLink
        assert "%24skiptoken=3" in str(mock.calls.last.request.url) or "$skiptoken=3" in str(
            mock.calls.last.request.url
        )

    @respx.mock
    def test_filter_and_options_sent(self, catalogue: ODataCatalogue) -> None:
        mock = respx.get(PRODUCTS).mock(return_value=httpx.Response(200, json=page([])))
        list(
            catalogue.search(Query(collection="SENTINEL-2", filters={"cloudCover": "<20"}, limit=5))
        )
        request_params = dict(mock.calls.last.request.url.params)
        assert "Collection/Name eq 'SENTINEL-2'" in request_params["$filter"]
        assert "cloudCover" in request_params["$filter"]
        assert request_params["$top"] == "5"
        assert request_params["$count"] == "true"

    @respx.mock
    def test_limit_truncates_across_pages(self, catalogue: ODataCatalogue) -> None:
        page2 = f"{PRODUCTS}?$skiptoken=2"
        mock = respx.get(url__startswith=PRODUCTS).mock(
            return_value=httpx.Response(
                200,
                json=page([odata_entry("u1", "P1"), odata_entry("u2", "P2")], next_link=page2),
            )
        )
        result = catalogue.search(Query(collection="X", limit=2))
        assert [p.name for p in result] == ["P1", "P2"]
        assert mock.call_count == 1  # limit reached; next page never fetched


class TestGet:
    @respx.mock
    def test_hit_normalizes(self, catalogue: ODataCatalogue) -> None:
        uuid = "f9d8a1c2-3b4e-5f60-7a8b-9c0d1e2f3a4b"
        name = "S2B_MSIL2A_20260615T095029_N0511_R079_T34UEE_20260615T105512.SAFE"
        respx.get(f"{BASE}/odata/v1/Products('{uuid}')").mock(
            return_value=httpx.Response(200, json=odata_entry(uuid, name))
        )
        product = catalogue.get(uuid)
        assert product.id == uuid
        assert product.name == name
        assert product.collection == "SENTINEL-2"
        assert product.size == 1207959552
        assert product.cloud_cover == 12.4
        assert product.datetime is not None and product.datetime.year == 2026
        assert product.checksum is not None and product.checksum.algorithm == "md5"
        assert product.s3_path is not None and product.s3_path.startswith("/eodata/")
        assert product.raw["Id"] == uuid

    @respx.mock
    def test_miss(self, catalogue: ODataCatalogue) -> None:
        respx.get(url__startswith=f"{BASE}/odata/v1/Products(").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(ProductNotFound, match="nope"):
            catalogue.get("nope")


class TestQueryRaw:
    @respx.mock
    def test_raw_passthrough(self, catalogue: ODataCatalogue) -> None:
        mock = respx.get(PRODUCTS).mock(
            return_value=httpx.Response(200, json=page([odata_entry("u1", "P1")]))
        )
        raw = catalogue.query_raw("Products?$filter=contains(Name,'S1A')&$top=3")
        assert raw["value"][0]["Name"] == "P1"
        request_params = dict(mock.calls.last.request.url.params)
        assert request_params["$filter"] == "contains(Name,'S1A')"
        assert request_params["$top"] == "3"


class TestParity:
    def test_stac_and_odata_normalize_to_equal_products(self) -> None:
        """Same logical product through both normalizers -> equal shared fields."""
        uuid = "f9d8a1c2-3b4e-5f60-7a8b-9c0d1e2f3a4b"
        name = "S2B_MSIL2A_20260615T095029_N0511_R079_T34UEE_20260615T105512.SAFE"
        from_stac = _item_to_product(stac_item(uuid, name))
        from_odata = __import__(
            "eosdk.catalogue.odata", fromlist=["_entry_to_product"]
        )._entry_to_product(odata_entry(uuid, name))

        assert from_stac.id == from_odata.id
        assert from_stac.name == from_odata.name
        assert from_stac.collection == from_odata.collection
        assert from_stac.size == from_odata.size
        assert from_stac.cloud_cover == from_odata.cloud_cover
        assert from_stac.datetime == from_odata.datetime
