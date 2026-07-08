import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk.catalogue.stac import StacCatalogue, decode_multihash
from eosdk.exceptions import ProductNotFound, UnsupportedApiVersion, UnsupportedQueryFeature
from eosdk.models import Query
from eosdk.transport import RetryPolicy, Transport

FIXTURES = Path(__file__).parent.parent / "fixtures"
BASE = "https://catalogue.example.eu/stac"
SEARCH = f"{BASE}/search"

CONFORMANCE = [
    "https://api.stacspec.org/v1.0.0/core",
    "https://api.stacspec.org/v1.0.0/item-search",
    "https://api.stacspec.org/v1.0.0/item-search#query",
]


def landing_page(conforms: list[str] | None = None, search_href: str = SEARCH) -> dict[str, Any]:
    return {
        "type": "Catalog",
        "id": "eodata",
        "conformsTo": CONFORMANCE if conforms is None else conforms,
        "links": [
            {"rel": "self", "href": BASE},
            {"rel": "search", "href": search_href, "method": "POST"},
            {"rel": "search", "href": search_href, "method": "GET"},
        ],
    }


def item() -> dict[str, Any]:
    return json.loads((FIXTURES / "stac_item_s2.json").read_text())


def feature_page(
    features: list[dict[str, Any]],
    *,
    matched: int | None = None,
    next_link: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {"type": "FeatureCollection", "features": features, "links": []}
    if matched is not None:
        doc["numberMatched"] = matched
    if next_link is not None:
        doc["links"].append({"rel": "next", **next_link})
    return doc


@pytest.fixture
def transport() -> Transport:
    with Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None) as t:
        yield t


@pytest.fixture
def catalogue(transport: Transport) -> StacCatalogue:
    return StacCatalogue(BASE, transport=transport)


class TestConformance:
    @respx.mock
    def test_missing_item_search_raises(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(
            return_value=httpx.Response(
                200, json=landing_page(conforms=["https://api.stacspec.org/v1.0.0/core"])
            )
        )
        with pytest.raises(UnsupportedApiVersion, match="item-search"):
            catalogue.search(Query(collection="SENTINEL-2"))

    @respx.mock
    def test_filters_without_query_ext_fail_before_search(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(
            return_value=httpx.Response(
                200,
                json=landing_page(
                    conforms=[
                        "https://api.stacspec.org/v1.0.0/core",
                        "https://api.stacspec.org/v1.0.0/item-search",
                    ]
                ),
            )
        )
        search_mock = respx.post(SEARCH)
        with pytest.raises(UnsupportedQueryFeature, match="stac"):
            catalogue.search(Query(filters={"cloudCover": "<20"}))
        assert search_mock.call_count == 0

    @respx.mock
    def test_landing_page_fetched_once_across_searches(self, catalogue: StacCatalogue) -> None:
        landing_mock = respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.post(SEARCH).mock(return_value=httpx.Response(200, json=feature_page([])))
        list(catalogue.search(Query(collection="A")))
        list(catalogue.search(Query(collection="B")))
        assert landing_mock.call_count == 1

    @respx.mock
    def test_search_href_from_landing_page_is_honored(self, transport: Transport) -> None:
        other = "https://searcher.example.eu/api/search"
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page(search_href=other)))
        search_mock = respx.post(other).mock(
            return_value=httpx.Response(200, json=feature_page([]))
        )
        list(StacCatalogue(BASE, transport=transport).search(Query()))
        assert search_mock.call_count == 1


class TestTranslation:
    @respx.mock
    def test_full_query_body(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        search_mock = respx.post(SEARCH).mock(
            return_value=httpx.Response(200, json=feature_page([]))
        )
        list(
            catalogue.search(
                Query(
                    collection="SENTINEL-2",
                    bbox=(22.5, 52.9, 24.0, 53.5),
                    datetime="2026-06-01/2026-06-30",
                    filters={"cloudCover": "<20", "productType": "L2A"},
                    limit=50,
                    sort="-datetime",
                )
            )
        )
        body = json.loads(search_mock.calls.last.request.content)
        assert body == {
            "collections": ["SENTINEL-2"],
            "bbox": [22.5, 52.9, 24.0, 53.5],
            "datetime": "2026-06-01/2026-06-30",
            "limit": 50,
            "sortby": [{"field": "datetime", "direction": "desc"}],
            "query": {
                "eo:cloud_cover": {"lt": 20.0},
                "product:type": {"eq": "L2A"},
            },
        }


class TestPagination:
    @respx.mock
    def test_follows_next_links_and_reiterates_from_cache(self, catalogue: StacCatalogue) -> None:
        item1, item2, item3 = item(), item(), item()
        item2["id"], item3["id"] = "second", "third"
        page2_url = f"{SEARCH}?token=p2"
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        first = respx.post(SEARCH).mock(
            return_value=httpx.Response(
                200,
                json=feature_page(
                    [item1, item2],
                    matched=3,
                    next_link={"href": page2_url, "method": "GET"},
                ),
            )
        )
        second = respx.get(page2_url).mock(
            return_value=httpx.Response(200, json=feature_page([item3]))
        )
        result = catalogue.search(Query(collection="SENTINEL-2"))
        names = [p.name for p in result]
        assert len(names) == 3
        assert names[1:] == ["second", "third"]
        assert len(result) == 3  # numberMatched
        # re-iteration: no new HTTP calls
        assert [p.name for p in result] == names
        assert first.call_count == 1
        assert second.call_count == 1

    @respx.mock
    def test_post_merge_next_link(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        pages = [
            feature_page(
                [item()],
                next_link={"href": SEARCH, "method": "POST", "merge": True, "body": {"token": "x"}},
            ),
            feature_page([]),
        ]
        search_mock = respx.post(SEARCH).mock(
            side_effect=[httpx.Response(200, json=p) for p in pages]
        )
        list(catalogue.search(Query(collection="SENTINEL-2")))
        follow_up = json.loads(search_mock.calls.last.request.content)
        assert follow_up["token"] == "x"
        assert follow_up["collections"] == ["SENTINEL-2"]  # merged, not replaced


class TestGetAndNormalization:
    @respx.mock
    def test_get_hit(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.post(SEARCH).mock(return_value=httpx.Response(200, json=feature_page([item()])))
        product = catalogue.get(item()["id"])
        assert product.id == "f9d8a1c2-3b4e-5f60-7a8b-9c0d1e2f3a4b"  # eodata:uuid wins
        assert product.name.startswith("S2B_MSIL2A_20260615")
        assert product.collection == "SENTINEL-2"
        assert product.size == 1207959552
        assert product.cloud_cover == 12.4
        assert product.datetime is not None and product.datetime.year == 2026
        assert product.checksum is not None
        assert product.checksum.algorithm == "md5"
        assert product.checksum.value == "9e107d9d372bb6826bd81d3542a419d6"
        assert product.s3_path is not None and "Sentinel-2" in product.s3_path
        assert product.raw["id"] == item()["id"]

    @respx.mock
    def test_get_miss(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.post(SEARCH).mock(return_value=httpx.Response(200, json=feature_page([])))
        with pytest.raises(ProductNotFound, match="nope"):
            catalogue.get("nope")

    @respx.mock
    def test_auth_header_attached(self, transport: Transport) -> None:
        import httpx as _httpx

        class FakeAuth:
            def httpx_auth(self) -> _httpx.Auth:
                class _A(_httpx.Auth):
                    def auth_flow(self, request):  # type: ignore[no-untyped-def]
                        request.headers["Authorization"] = "Bearer FAKE"
                        yield request

                return _A()

        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        search_mock = respx.post(SEARCH).mock(
            return_value=httpx.Response(200, json=feature_page([]))
        )
        catalogue = StacCatalogue(BASE, transport=transport, auth=FakeAuth())
        list(catalogue.search(Query()))
        assert search_mock.calls.last.request.headers["Authorization"] == "Bearer FAKE"


class TestMultihash:
    def test_md5(self) -> None:
        checksum = decode_multihash("d5109e107d9d372bb6826bd81d3542a419d6")
        assert checksum is not None
        assert checksum.algorithm == "md5"

    def test_sha256(self) -> None:
        digest = "a" * 64
        checksum = decode_multihash("1220" + digest)
        assert checksum is not None
        assert checksum.algorithm == "sha256"
        assert checksum.value == digest

    @pytest.mark.parametrize("bad", ["zz", "ff10" + "a" * 32, "d510abcd", ""])
    def test_unknown_or_malformed_degrade_to_none(self, bad: str) -> None:
        assert decode_multihash(bad) is None
