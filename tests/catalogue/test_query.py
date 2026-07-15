import pytest

from eosdk.catalogue.query import FilterClause, parse_filters
from eosdk.exceptions import UnsupportedQueryFeature


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"cloudCover": "<20"}, [FilterClause("cloudCover", "lt", 20.0)]),
        ({"cloudCover": "<=20"}, [FilterClause("cloudCover", "lte", 20.0)]),
        ({"cloudCover": ">=0.5"}, [FilterClause("cloudCover", "gte", 0.5)]),
        ({"cloudCover": ">5"}, [FilterClause("cloudCover", "gt", 5.0)]),
        ({"productType": "!=GRD"}, [FilterClause("productType", "neq", "GRD")]),
        ({"productType": "GRD"}, [FilterClause("productType", "eq", "GRD")]),
        ({"productType": "=GRD"}, [FilterClause("productType", "eq", "GRD")]),
        ({"orbit": "42"}, [FilterClause("orbit", "eq", 42.0)]),
        ({"name": "< 20"}, [FilterClause("name", "lt", 20.0)]),
    ],
)
def test_golden_cases(raw: dict[str, str], expected: list[FilterClause]) -> None:
    assert parse_filters(raw, backend="stac") == expected


@pytest.mark.parametrize("bad", ["~fuzzy", "^prefix", "*glob", "", "<"])
def test_unsupported_operators_name_backend(bad: str) -> None:
    with pytest.raises(UnsupportedQueryFeature, match="odata"):
        parse_filters({"prop": bad}, backend="odata")
