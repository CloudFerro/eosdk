import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk.catalogue.stac import (
    StacCatalogue,
    _download_id,
    _normalize_interval,
    _product_s3_path,
    _queryable_type,
    decode_multihash,
)
from eosdk.exceptions import (
    CollectionNotFound,
    ProductNotFound,
    UnsupportedApiVersion,
    UnsupportedQueryFeature,
)
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
        respx.get(url__startswith=f"{BASE}/collections/").mock(
            return_value=httpx.Response(200, json={"id": "x"})
        )
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

    @respx.mock
    def test_missing_search_link_falls_back_to_base_search(self, catalogue: StacCatalogue) -> None:
        page = landing_page()
        page["links"] = [{"rel": "self", "href": BASE}]  # no rel="search" advertised
        respx.get(BASE).mock(return_value=httpx.Response(200, json=page))
        search_mock = respx.post(SEARCH).mock(
            return_value=httpx.Response(200, json=feature_page([]))
        )
        assert list(catalogue.search(Query())) == []
        assert search_mock.call_count == 1


def landing_page_get_only() -> dict[str, Any]:
    page = landing_page()
    page["links"] = [
        {"rel": "self", "href": BASE},
        {"rel": "search", "href": SEARCH, "method": "GET"},
    ]
    return page


class TestGetMethodSearch:
    @respx.mock
    def test_body_translated_to_query_params(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page_get_only()))
        search_mock = respx.get(url__startswith=SEARCH).mock(
            return_value=httpx.Response(200, json=feature_page([item()]))
        )
        list(
            catalogue.search(
                Query(
                    collection="SENTINEL-2",
                    bbox=(22.5, 52.9, 24.0, 53.5),
                    datetime="2026-06-01/2026-06-30",
                    limit=5,
                    sort="-datetime",
                )
            )
        )
        params = dict(search_mock.calls.last.request.url.params)
        assert params == {
            "collections": "SENTINEL-2",
            "bbox": "22.5,52.9,24.0,53.5",
            "datetime": "2026-06-01T00:00:00Z/2026-06-30T23:59:59Z",
            "limit": "5",
            "sortby": "-datetime",
        }

    @respx.mock
    def test_ascending_sort_has_no_sign_prefix(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page_get_only()))
        search_mock = respx.get(url__startswith=SEARCH).mock(
            return_value=httpx.Response(200, json=feature_page([item()]))
        )
        list(catalogue.search(Query(collection="SENTINEL-2", sort="+name")))
        assert dict(search_mock.calls.last.request.url.params)["sortby"] == "name"

    @respx.mock
    def test_filters_over_get_endpoint_unsupported(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page_get_only()))
        search_mock = respx.get(url__startswith=SEARCH)
        with pytest.raises(UnsupportedQueryFeature, match="GET-only"):
            list(catalogue.search(Query(filters={"cloudCover": "<20"})))
        assert search_mock.call_count == 0


class TestTranslation:
    @respx.mock
    def test_full_query_body(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.get(f"{BASE}/collections/SENTINEL-2").mock(
            return_value=httpx.Response(200, json={"id": "SENTINEL-2"})
        )
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
            "datetime": "2026-06-01T00:00:00Z/2026-06-30T23:59:59Z",
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

    @respx.mock
    def test_post_next_link_without_merge_replaces_body(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        pages = [
            feature_page(
                [item()],
                next_link={"href": SEARCH, "method": "POST", "body": {"token": "x"}},
            ),
            feature_page([]),
        ]
        search_mock = respx.post(SEARCH).mock(
            side_effect=[httpx.Response(200, json=p) for p in pages]
        )
        list(catalogue.search(Query(collection="SENTINEL-2")))
        follow_up = json.loads(search_mock.calls.last.request.content)
        assert follow_up == {"token": "x"}  # no merge flag: the link body replaces ours

    @respx.mock
    def test_limit_truncates_and_stops_pagination(self, catalogue: StacCatalogue) -> None:
        item1, item2 = item(), item()
        item2["id"] = "second"
        page2_url = f"{SEARCH}?token=p2"
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.post(SEARCH).mock(
            return_value=httpx.Response(
                200,
                json=feature_page([item1, item2], next_link={"href": page2_url, "method": "GET"}),
            )
        )
        second = respx.get(page2_url)
        result = catalogue.search(Query(collection="SENTINEL-2", limit=2))
        assert len(list(result)) == 2
        assert second.call_count == 0  # limit reached; next page never fetched

    @respx.mock
    def test_limit_truncates_within_a_page(self, catalogue: StacCatalogue) -> None:
        item1, item2 = item(), item()
        item2["id"] = "second"
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.post(SEARCH).mock(return_value=httpx.Response(200, json=feature_page([item1, item2])))
        result = catalogue.search(Query(collection="SENTINEL-2", limit=1))
        assert [p.name for p in result] == [item1["id"]]


class TestRawSearch:
    @respx.mock
    def test_body_posted_verbatim(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        search_mock = respx.post(SEARCH).mock(
            return_value=httpx.Response(200, json=feature_page([item()]))
        )
        document = catalogue.raw_search({"ids": ["X"], "limit": 1})
        assert json.loads(search_mock.calls.last.request.content) == {"ids": ["X"], "limit": 1}
        assert document["features"][0]["id"] == item()["id"]  # raw JSON, not Products


class TestUnknownCollection:
    """An empty first page triggers one collection-existence probe (SPEC: STAC
    answers unknown collections with an empty FeatureCollection, not an error)."""

    SENTINEL_IDS = ("sentinel-1-grd", "sentinel-1-slc", "sentinel-2-l2a")

    def _mock_search_empty(self) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.post(SEARCH).mock(return_value=httpx.Response(200, json=feature_page([])))

    @respx.mock
    def test_nonexistent_collection_raises_with_suggestions(self, catalogue: StacCatalogue) -> None:
        self._mock_search_empty()
        respx.get(f"{BASE}/collections/SENTINEL-1").mock(
            return_value=httpx.Response(404, json={"code": "NotFoundError"})
        )
        respx.get(f"{BASE}/collections").mock(
            return_value=httpx.Response(
                200, json={"collections": [{"id": i} for i in self.SENTINEL_IDS]}
            )
        )
        with pytest.raises(CollectionNotFound, match="SENTINEL-1") as excinfo:
            list(catalogue.search(Query(collection="SENTINEL-1")))
        assert excinfo.value.suggestions == ["sentinel-1-grd", "sentinel-1-slc"]
        assert "odata" in str(excinfo.value)  # all-caps name -> OData vocabulary hint

    @respx.mock
    def test_lowercase_typo_gets_close_matches_without_odata_hint(
        self, catalogue: StacCatalogue
    ) -> None:
        self._mock_search_empty()
        respx.get(f"{BASE}/collections/sentinel-1-gdr").mock(
            return_value=httpx.Response(404, json={"code": "NotFoundError"})
        )
        respx.get(f"{BASE}/collections").mock(
            return_value=httpx.Response(
                200, json={"collections": [{"id": i} for i in self.SENTINEL_IDS]}
            )
        )
        with pytest.raises(CollectionNotFound) as excinfo:
            list(catalogue.search(Query(collection="sentinel-1-gdr")))
        assert "sentinel-1-grd" in excinfo.value.suggestions
        assert "odata" not in str(excinfo.value)

    @respx.mock
    def test_existing_collection_empty_result_stays_empty(self, catalogue: StacCatalogue) -> None:
        self._mock_search_empty()
        probe = respx.get(f"{BASE}/collections/sentinel-1-grd").mock(
            return_value=httpx.Response(200, json={"id": "sentinel-1-grd"})
        )
        assert list(catalogue.search(Query(collection="sentinel-1-grd"))) == []
        assert probe.call_count == 1

    @respx.mock
    def test_non_empty_first_page_skips_the_probe(self, catalogue: StacCatalogue) -> None:
        respx.get(BASE).mock(return_value=httpx.Response(200, json=landing_page()))
        respx.post(SEARCH).mock(return_value=httpx.Response(200, json=feature_page([item()])))
        probe = respx.get(url__startswith=f"{BASE}/collections/")
        assert len(list(catalogue.search(Query(collection="sentinel-2-l2a")))) == 1
        assert probe.call_count == 0

    @respx.mock
    def test_no_collection_in_query_skips_the_probe(self, catalogue: StacCatalogue) -> None:
        self._mock_search_empty()
        probe = respx.get(url__startswith=f"{BASE}/collections/")
        assert list(catalogue.search(Query(bbox=(1.0, 2.0, 3.0, 4.0)))) == []
        assert probe.call_count == 0

    @respx.mock
    def test_unreachable_probe_degrades_to_empty_result(self, catalogue: StacCatalogue) -> None:
        self._mock_search_empty()
        respx.get(f"{BASE}/collections/sentinel-1-grd").mock(side_effect=httpx.ConnectError("boom"))
        assert list(catalogue.search(Query(collection="sentinel-1-grd"))) == []

    @respx.mock
    def test_suggestions_survive_failing_collections_listing(
        self, catalogue: StacCatalogue
    ) -> None:
        self._mock_search_empty()
        respx.get(f"{BASE}/collections/SENTINEL-1").mock(
            return_value=httpx.Response(404, json={"code": "NotFoundError"})
        )
        respx.get(f"{BASE}/collections").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(CollectionNotFound) as excinfo:
            list(catalogue.search(Query(collection="SENTINEL-1")))
        assert excinfo.value.suggestions == []


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
        assert product.datetime is not None
        assert product.datetime.year == 2026
        assert product.checksum is not None
        assert product.checksum.algorithm == "md5"
        assert product.checksum.value == "9e107d9d372bb6826bd81d3542a419d6"
        assert product.s3_path is not None
        assert "Sentinel-2" in product.s3_path
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
    def test_md5_varint(self) -> None:
        # real CDSE shape: varint code d5 01, length 10, 16-byte digest
        checksum = decode_multihash("d501109e107d9d372bb6826bd81d3542a419d6")
        assert checksum is not None
        assert checksum.algorithm == "md5"
        assert checksum.value == "9e107d9d372bb6826bd81d3542a419d6"

    def test_sha3_256(self) -> None:
        digest = "f88fa1d881d56bd4c17737bc3ff0d416c48b72959f97e98d6bfd61aaab87bdd4"
        checksum = decode_multihash("1620" + digest)
        assert checksum is not None
        assert checksum.algorithm == "sha3-256"
        assert checksum.value == digest

    def test_sha256(self) -> None:
        digest = "a" * 64
        checksum = decode_multihash("1220" + digest)
        assert checksum is not None
        assert checksum.algorithm == "sha256"
        assert checksum.value == digest

    @pytest.mark.parametrize("bad", ["zz", "ff10" + "a" * 32, "d510abcd", ""])
    def test_unknown_or_malformed_degrade_to_none(self, bad: str) -> None:
        assert decode_multihash(bad) is None

    def test_digest_length_mismatch_degrades_to_none(self) -> None:
        # md5 varint code, declared length 16, but only 15 digest bytes follow
        assert decode_multihash("d50110" + "ab" * 15) is None

    def test_empty_digest_degrades_to_none(self) -> None:
        assert decode_multihash("d50100") is None


class TestCollectionsAndQueryables:
    @respx.mock
    def test_collections_keep_raw_payload(self, catalogue: StacCatalogue) -> None:
        entry = {
            "id": "sentinel-2-l2a",
            "title": "Sentinel-2 Level-2A",
            "extent": {"spatial": {"bbox": [[-180, -90, 180, 90]]}},
            "summaries": {"product:type": ["S2MSI2A"]},
        }
        respx.get(f"{BASE}/collections").mock(
            return_value=httpx.Response(200, json={"collections": [entry]})
        )
        (collection,) = catalogue.collections()
        assert collection.id == "sentinel-2-l2a"
        assert collection.raw["summaries"]["product:type"] == ["S2MSI2A"]

    @respx.mock
    def test_collections_follow_next_links(self, catalogue: StacCatalogue) -> None:
        page_two = f"{BASE}/collections?offset=1"
        # One route serves both pages in sequence; the second page echoes an
        # already-visited next href, which must not loop.
        mock = respx.get(url__startswith=f"{BASE}/collections").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "collections": [{"id": "one"}],
                        "links": [{"rel": "next", "href": page_two}],
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "collections": [{"id": "two"}],
                        "links": [{"rel": "next", "href": page_two}],
                    },
                ),
            ]
        )
        assert [c.id for c in catalogue.collections()] == ["one", "two"]
        assert mock.call_count == 2

    @respx.mock
    def test_queryables_normalized_from_json_schema(self, catalogue: StacCatalogue) -> None:
        respx.get(f"{BASE}/collections/sentinel-2-l2a/queryables").mock(
            return_value=httpx.Response(
                200,
                json={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "eo:cloud_cover": {"type": "number", "minimum": 0, "maximum": 100},
                        "product:type": {"type": "string"},
                        "datetime": {"type": "string", "format": "date-time"},
                        "geometry": {"$ref": "https://geojson.org/schema/Geometry.json"},
                    },
                },
            )
        )
        queryables = {q.name: q for q in catalogue.queryables("sentinel-2-l2a")}
        assert queryables["eo:cloud_cover"].type == "number"
        assert queryables["product:type"].type == "string"
        assert queryables["datetime"].type == "datetime"  # format wins over "string"
        assert queryables["geometry"].type is None  # $ref only: nothing declared
        assert queryables["eo:cloud_cover"].raw["maximum"] == 100

    @respx.mock
    def test_missing_queryables_endpoint_is_unsupported(self, catalogue: StacCatalogue) -> None:
        respx.get(f"{BASE}/collections/nope/queryables").mock(
            return_value=httpx.Response(404, json={"code": "NotFound"})
        )
        with pytest.raises(UnsupportedQueryFeature, match="queryables"):
            catalogue.queryables("nope")

    def test_non_dict_schema_fragment_has_no_type(self) -> None:
        assert _queryable_type(True) is None
        assert _queryable_type(["string"]) is None


class TestNormalizeInterval:
    @pytest.mark.parametrize(
        ("raw", "normalized"),
        [
            ("2026-06-01/..", "2026-06-01T00:00:00Z/.."),
            ("../2026-06-30", "../2026-06-30T23:59:59Z"),
            ("2026-06-15", "2026-06-15T00:00:00Z/2026-06-15T23:59:59Z"),
            ("2026-06-15T12:00:00", "2026-06-15T12:00:00Z/2026-06-15T12:00:00Z"),
            (
                "2026-06-01T10:00:00Z/2026-06-02T10:00:00+02:00",
                "2026-06-01T10:00:00Z/2026-06-02T10:00:00+02:00",  # offsets kept as-is
            ),
            ("2026-06-01T10:00:00/2026-06-30", "2026-06-01T10:00:00Z/2026-06-30T23:59:59Z"),
            (
                "2026-06-01T10:00:00-02:00/2026-06-02T10:00:00-02:00",
                "2026-06-01T10:00:00-02:00/2026-06-02T10:00:00-02:00",  # negative offsets too
            ),
        ],
    )
    def test_bounds_expand_to_rfc3339_instants(self, raw: str, normalized: str) -> None:
        assert _normalize_interval(raw) == normalized


class TestDownloadIdFallbacks:
    def test_uuid_from_alternate_asset_href(self) -> None:
        item_doc = {
            "id": "ITEM",
            "assets": {
                "B04": {
                    "href": "s3://eodata/x/B04.jp2",
                    "alternate": {"https": {"href": f"{BASE}/odata/v1/Products(alt-uuid)/$value"}},
                }
            },
        }
        assert _download_id(item_doc) == "alt-uuid"

    @pytest.mark.parametrize("key", ["eodata:uuid", "odata:id", "uuid", "id"])
    def test_uuid_from_properties(self, key: str) -> None:
        item_doc = {"id": "ITEM", "assets": {}, "properties": {key: "prop-uuid"}}
        assert _download_id(item_doc) == "prop-uuid"

    def test_bare_item_id_is_last_resort(self) -> None:
        assert _download_id({"id": "ITEM", "assets": {}, "properties": {}}) == "ITEM"


class TestProductS3Path:
    def test_alternate_s3_href_when_local_path_is_bare_filename(self) -> None:
        primary = {
            "href": f"{BASE}/odata/v1/Products(u)/$value",
            "file:local_path": "product.zip",  # CDSE: just the zip name, not a path
            "alternate": {"s3": {"href": "/eodata/Sentinel-2/product.SAFE"}},
        }
        item_doc = {"id": "ITEM", "assets": {"Product": primary}}
        assert _product_s3_path(item_doc, primary) == "/eodata/Sentinel-2/product.SAFE"

    def test_path_like_local_path_used_when_no_s3_hrefs(self) -> None:
        primary = {
            "href": f"{BASE}/odata/v1/Products(u)/$value",
            "file:local_path": "/eodata/Sentinel-2/product.SAFE",
        }
        item_doc = {"id": "ITEM", "assets": {"Product": primary}}
        assert _product_s3_path(item_doc, primary) == "/eodata/Sentinel-2/product.SAFE"

    def test_no_s3_information_yields_none(self) -> None:
        primary = {
            "href": f"{BASE}/odata/v1/Products(u)/$value",
            "file:local_path": "product.zip",
        }
        item_doc = {"id": "ITEM", "assets": {"Product": primary}}
        assert _product_s3_path(item_doc, primary) is None

    def test_sibling_files_sharing_a_name_stem_keep_whole_directory(self) -> None:
        # commonprefix over these hrefs is ".../T33UUB_20260615_B0" — a cut
        # through the filenames; the product root must stay a real directory.
        item_doc = {
            "id": "ITEM",
            "assets": {
                "B04": {"href": "s3://eodata/S2/x.SAFE/R10m/T33UUB_20260615_B04_10m.jp2"},
                "B08": {"href": "s3://eodata/S2/x.SAFE/R10m/T33UUB_20260615_B08_10m.jp2"},
            },
        }
        assert _product_s3_path(item_doc, {}) == "s3://eodata/S2/x.SAFE/R10m"

    def test_single_s3_href_yields_its_directory(self) -> None:
        item_doc = {"id": "ITEM", "assets": {"Z": {"href": "s3://eodata/S2/x.SAFE/manifest.xml"}}}
        assert _product_s3_path(item_doc, {}) == "s3://eodata/S2/x.SAFE"

    def test_hrefs_in_different_buckets_yield_none(self) -> None:
        item_doc = {
            "id": "ITEM",
            "assets": {
                "A": {"href": "s3://eodata-a/x/a.jp2"},
                "B": {"href": "s3://eodata-b/x/b.jp2"},
            },
        }
        assert _product_s3_path(item_doc, {}) is None
