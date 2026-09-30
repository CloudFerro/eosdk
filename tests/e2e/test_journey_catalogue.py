"""Catalogue journeys (plan §4.2, K1-K5), parametrized over the protocol axis.

No data access here: every test runs identically for ``stac`` and ``odata``,
so a failure names the catalogue backend and nothing else. Until now only STAC
had a journey — OData was covered one translation unit at a time in
``tests/catalogue/test_odata_backend.py``.

The harness evaluates the ``$filter`` the SDK generates rather than replaying a
canned response, so a search that returns the wrong set here is a translation
bug, not a fixture that drifted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from eosdk.exceptions import ConfigError, ProductNotFound, UnsupportedQueryFeature

if TYPE_CHECKING:
    from eosdk.client import Client
    from eosdk.models import Product
    from tests.e2e.platform import FakePlatform

pytestmark = pytest.mark.e2e

PROTOCOLS = ("stac", "odata")

#: which service a protocol's requests are recorded under
SERVICE = {"stac": "stac", "odata": "odata"}

#: the queryable name each backend advertises for the same underlying attribute
CLOUD_COVER = {"stac": "eo:cloud_cover", "odata": "cloudCover"}
PRODUCT_TYPE = {"stac": "product:type", "odata": "productType"}

# A bounded query the harness can answer identically on both protocols: the
# bbox excludes product 3 (it lies to the south-west), the interval excludes
# nothing in June, and the cloud filter keeps products 0, 1 and 2.
BBOX = (22.5, 52.9, 24.0, 53.5)
INTERVAL = "2026-06-01/2026-06-30"


def matching_names(platform: FakePlatform) -> list[str]:
    """The products the shared K1 query genuinely selects, in server order."""
    return [
        product.name
        for product in platform.products
        if product.collection == "SENTINEL-2"
        and (product.cloud_cover or 0.0) < 50
        and not (BBOX[2] < product.bbox[0] or product.bbox[2] < BBOX[0])
        and not (BBOX[3] < product.bbox[1] or product.bbox[3] < BBOX[1])
    ]


def bounded_search(client: Client, protocol: str, **overrides: Any) -> Any:
    options: dict[str, Any] = {
        "collection": "SENTINEL-2",
        "bbox": BBOX,
        "datetime": INTERVAL,
        "filters": {"cloudCover": "<50"},
        "protocol": protocol,
    }
    options.update(overrides)
    return client.search(**options)


class TestPagingAndLaziness:
    """K1 — paging, the advertised total, and re-iteration without re-querying."""

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_three_pages_total_and_free_reiteration(
        self, library_client: Client, platform: FakePlatform, protocol: str
    ) -> None:
        platform.page_size = 1  # one product per page: three real pages
        expected = matching_names(platform)
        assert len(expected) == 3, "the fixture must span more than one page"

        results = bounded_search(library_client, protocol)
        service = SERVICE[protocol]
        platform.reset_calls()

        # len() comes from the backend's advertised count (numberMatched /
        # @odata.count), which arrives with the first page — one request.
        assert len(results) == 3
        assert results.matched == 3
        assert platform.call_count(service) == 1

        assert [product.name for product in results] == expected
        after_first_pass = platform.call_count(service)
        assert after_first_pass == 3  # exactly one request per page, no re-fetch

        # SPEC §5: the result is re-iterable and pages are cached.
        assert [product.name for product in results] == expected
        assert [len(page) for page in results.pages()] == [1, 1, 1]
        assert platform.call_count(service) == after_first_pass

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_limit_stops_paging_early(
        self, library_client: Client, platform: FakePlatform, protocol: str
    ) -> None:
        platform.page_size = 1
        results = bounded_search(library_client, protocol, limit=2)
        platform.reset_calls()
        assert len(list(results)) == 2
        # the third page is never requested: the limit is exhausted first
        assert platform.call_count(SERVICE[protocol]) == 2


class TestVocabularyDiscovery:
    """K2 — queryable names are usable verbatim as filter keys, per protocol."""

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_queryable_name_is_a_working_filter_key(
        self, library_client: Client, platform: FakePlatform, protocol: str
    ) -> None:
        # OData exposes no collection-enumeration endpoint, so its collection
        # ids are discovered through STAC (src/eosdk/catalogue/odata.py).
        collection = next(c.id for c in library_client.collections() if c.id == "SENTINEL-2")

        queryables = library_client.queryables(collection, protocol=protocol)
        names = {queryable.name: queryable.type for queryable in queryables}
        assert names[CLOUD_COVER[protocol]] == "number"
        assert names[PRODUCT_TYPE[protocol]] == "string"

        # the advertised name, used unchanged as a filters key
        results = library_client.search(
            collection=collection,
            datetime=INTERVAL,
            filters={CLOUD_COVER[protocol]: "<20", PRODUCT_TYPE[protocol]: "S2MSI2A"},
            protocol=protocol,
        )
        selected = {product.name for product in results}
        assert selected == {
            product.name
            for product in platform.products
            if product.collection == "SENTINEL-2"
            and (product.cloud_cover or 0.0) < 20
            and product.attributes.get("productType") == "S2MSI2A"
        }
        assert selected, "the filter must select something, or the test proves nothing"

    def test_odata_refuses_collection_enumeration(self, library_client: Client) -> None:
        """The refusal is part of the contract, not an outage (SPEC §6.5)."""
        with pytest.raises(UnsupportedQueryFeature, match="collections") as exc_info:
            library_client.collections(protocol="odata")
        assert exc_info.value.backend == "odata"

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_collection_without_queryables_is_refused(
        self, library_client: Client, protocol: str
    ) -> None:
        with pytest.raises(UnsupportedQueryFeature, match="SENTINEL-3"):
            library_client.queryables("SENTINEL-3", protocol=protocol)


class TestSearchGetConsistency:
    """K3 — a product found by search is the same product fetched by id."""

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_get_returns_the_searched_product(self, library_client: Client, protocol: str) -> None:
        found = next(iter(bounded_search(library_client, protocol)))
        fetched = library_client.get(found.id, protocol=protocol)
        assert _comparable(fetched) == _comparable(found)

    def test_stac_also_resolves_the_catalogue_item_id(self, library_client: Client) -> None:
        """``Product.id`` is the download uuid on STAC, not the item id.

        ``_download_id`` takes the uuid out of the Product asset's href, so a
        STAC round-trip goes out under a different identifier than the one the
        item advertises. Both must resolve, or ``search -> get`` is only usable
        on deployments that index the uuid.
        """
        found = next(iter(bounded_search(library_client, "stac")))
        assert found.id != found.name
        assert _comparable(library_client.get(found.name, protocol="stac")) == _comparable(found)

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_unknown_id_raises_product_not_found(
        self, library_client: Client, protocol: str
    ) -> None:
        with pytest.raises(ProductNotFound) as exc_info:
            library_client.get("00000000-0000-4000-8000-000000000000", protocol=protocol)
        assert exc_info.value.backend == protocol


class TestRefusals:
    """K4 — what the catalogue declines, and how early it declines it."""

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_inexpressible_filter_operator_names_backend_and_feature(
        self, library_client: Client, protocol: str
    ) -> None:
        with pytest.raises(UnsupportedQueryFeature) as exc_info:
            list(
                library_client.search(
                    collection="SENTINEL-2", filters={"cloudCover": "~20"}, protocol=protocol
                )
            )
        error = exc_info.value
        assert error.backend == protocol
        assert "~" in error.feature
        assert protocol in str(error)

    def test_odata_refuses_a_sort_field_it_cannot_express(self, library_client: Client) -> None:
        # STAC passes any sortby through; OData only knows three orderable
        # columns, so this refusal is protocol-specific by construction.
        with pytest.raises(UnsupportedQueryFeature, match="cloudCover"):
            library_client.search(collection="SENTINEL-2", sort="-cloudCover", protocol="odata")

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_unbounded_search_is_refused_before_any_request(
        self, library_client: Client, platform: FakePlatform, protocol: str
    ) -> None:
        platform.reset_calls()
        with pytest.raises(ConfigError, match="unbounded"):
            library_client.search(protocol=protocol)
        # the guard lives in Client.search, above the backend: no catalogue is
        # even constructed, so neither protocol issues a request (SPEC §6.5)
        assert platform.calls == []

    def test_stac_translates_before_the_search_but_after_the_landing_page(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        """A refusal must never leave a half-issued search behind.

        STAC has to read the landing page first — the query extension is a
        conformance claim — so it costs exactly one GET and no /search call.
        OData translates purely and costs nothing; both are asserted here so
        the difference is documented rather than discovered.
        """
        platform.reset_calls()
        with pytest.raises(UnsupportedQueryFeature):
            library_client.search(
                collection="SENTINEL-2", filters={"cloudCover": "~20"}, protocol="stac"
            )
        stac_calls = platform.requests_to("stac")
        assert [call.path for call in stac_calls] == [""]  # the landing page only

        platform.reset_calls()
        with pytest.raises(UnsupportedQueryFeature):
            library_client.search(
                collection="SENTINEL-2", filters={"cloudCover": "~20"}, protocol="odata"
            )
        assert platform.requests_to("odata") == []


class TestEscapeHatches:
    """K5 — raw queries still produce a Product the downloader can use."""

    def test_odata_query_raw_normalizes_into_a_usable_product(
        self, library_client: Client, platform: FakePlatform, tmp_path: Any
    ) -> None:
        # `query_raw` deliberately returns untouched JSON, so normalization is
        # the caller's job; the backend's own helper is what the SDK would use.
        from eosdk.catalogue.odata import _entry_to_product

        document = library_client._odata_catalogue().query_raw(
            "Products?$filter=Collection/Name eq 'SENTINEL-1'"
        )
        entries = document["value"]
        assert len(entries) == 1

        product = _entry_to_product(entries[0])
        expected = platform.products[4]
        assert product.id == expected.uuid
        assert product.s3_path == expected.s3_path

        reports = library_client.download(product, target=tmp_path / "raw-odata", via="http")
        assert reports[0].path.read_bytes() == expected.zip_bytes

    def test_stac_raw_search_normalizes_into_a_usable_product(
        self, library_client: Client, platform: FakePlatform, tmp_path: Any
    ) -> None:
        from eosdk.catalogue.stac import _item_to_product

        document = library_client._stac_catalogue().raw_search(
            {"collections": ["SENTINEL-1"], "limit": 5}
        )
        features = document["features"]
        assert len(features) == 1

        product = _item_to_product(features[0])
        expected = platform.products[4]
        assert product.id == expected.uuid
        assert product.s3_path == expected.s3_uri
        assert product.raw == features[0]  # the escape hatch keeps the raw item

        reports = library_client.download(product, target=tmp_path / "raw-stac", via="http")
        assert reports[0].path.read_bytes() == expected.zip_bytes


def _comparable(product: Product) -> dict[str, Any]:
    """Everything a caller can observe, so equality here is total."""
    return {
        "id": product.id,
        "name": product.name,
        "collection": product.collection,
        "datetime": product.datetime,
        "size": product.size,
        "cloud_cover": product.cloud_cover,
        "checksum": product.checksum,
        "s3_path": product.s3_path,
        "geometry": product.geometry,
    }
