"""Golden-case table for Query -> $filter translation (the contract artifact).

Grow this table whenever a translation bug is found.
"""

import pytest

from eosdk.catalogue._odata_filter import build_filter, build_query_params
from eosdk.exceptions import UnsupportedQueryFeature
from eosdk.models import Query

GOLDEN: list[tuple[Query, str]] = [
    (
        Query(collection="SENTINEL-2"),
        "Collection/Name eq 'SENTINEL-2'",
    ),
    (
        Query(collection="O'Brien Collection"),
        "Collection/Name eq 'O''Brien Collection'",
    ),
    (
        Query(datetime="2026-06-01/2026-06-30"),
        "ContentDate/Start ge 2026-06-01T00:00:00.000Z"
        " and ContentDate/Start le 2026-06-30T00:00:00.000Z",
    ),
    (
        Query(datetime="2026-06-01/.."),
        "ContentDate/Start ge 2026-06-01T00:00:00.000Z",
    ),
    (
        Query(datetime="../2026-06-30T12:00:00"),
        "ContentDate/Start le 2026-06-30T12:00:00Z",
    ),
    (
        Query(bbox=(22.5, 52.9, 24.0, 53.5)),
        "OData.CSC.Intersects(area=geography'SRID=4326;POLYGON("
        "(22.5 52.9,24.0 52.9,24.0 53.5,22.5 53.5,22.5 52.9))')",
    ),
    (
        Query(filters={"cloudCover": "<20"}),
        "Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover'"
        " and att/OData.CSC.DoubleAttribute/Value lt 20)",
    ),
    (
        Query(filters={"productType": "GRD"}),
        "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType'"
        " and att/OData.CSC.StringAttribute/Value eq 'GRD')",
    ),
    (
        Query(filters={"orbitDirection": "!=ASCENDING"}),
        "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'orbitDirection'"
        " and att/OData.CSC.StringAttribute/Value ne 'ASCENDING')",
    ),
    (
        Query(filters={"weird'Name": "x'y"}),
        "Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'weird''Name'"
        " and att/OData.CSC.StringAttribute/Value eq 'x''y')",
    ),
    (
        Query(collection="SENTINEL-1", filters={"cloudCover": ">=0.5"}),
        "Collection/Name eq 'SENTINEL-1'"
        " and Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover'"
        " and att/OData.CSC.DoubleAttribute/Value ge 0.5)",
    ),
]


@pytest.mark.parametrize(("query", "expected"), GOLDEN, ids=range(len(GOLDEN)))
def test_golden_filters(query: Query, expected: str) -> None:
    assert build_filter(query) == expected


class TestQueryParams:
    def test_full_params(self) -> None:
        params = build_query_params(Query(collection="SENTINEL-2", limit=50, sort="-datetime"))
        assert params["$filter"] == "Collection/Name eq 'SENTINEL-2'"
        assert params["$top"] == "50"
        assert params["$orderby"] == "ContentDate/Start desc"
        assert params["$expand"] == "Attributes"
        assert params["$count"] == "true"

    def test_sort_aliases(self) -> None:
        assert build_query_params(Query(collection="X", sort="+name"))["$orderby"] == "Name asc"
        assert (
            build_query_params(Query(collection="X", sort="-size"))["$orderby"]
            == "ContentLength desc"
        )


class TestUnsupported:
    def test_unknown_sort_field(self) -> None:
        with pytest.raises(UnsupportedQueryFeature, match="odata"):
            build_query_params(Query(collection="X", sort="-obscureField"))

    def test_empty_query(self) -> None:
        with pytest.raises(UnsupportedQueryFeature, match="unbounded"):
            build_filter(Query())

    def test_fully_open_interval(self) -> None:
        with pytest.raises(UnsupportedQueryFeature):
            build_filter(Query(datetime="../.."))

    def test_bad_filter_operator_names_odata(self) -> None:
        with pytest.raises(UnsupportedQueryFeature, match="odata"):
            build_filter(Query(filters={"x": "~fuzzy"}))
