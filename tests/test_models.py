from typing import Any

import pytest
from pydantic import ValidationError

from eosdk.models import Checksum, Page, Product, Query, SearchResult


class TestChecksum:
    def test_normalizes_case(self) -> None:
        assert Checksum(algorithm="md5", value="ABCDEF01").value == "abcdef01"

    def test_rejects_non_hex(self) -> None:
        with pytest.raises(ValidationError):
            Checksum(algorithm="md5", value="not-hex!")


class TestQuery:
    def test_valid(self) -> None:
        q = Query(collection="SENTINEL-2", bbox=(22.5, 52.9, 24.0, 53.5), limit=50)
        assert q.bbox == (22.5, 52.9, 24.0, 53.5)

    @pytest.mark.parametrize(
        "bbox",
        [
            (24.0, 52.9, 22.5, 53.5),  # minx > maxx
            (22.5, 53.5, 24.0, 52.9),  # miny > maxy
            (22.5, -95.0, 24.0, 53.5),  # latitude out of range
        ],
    )
    def test_bad_bbox(self, bbox: tuple[float, float, float, float]) -> None:
        with pytest.raises(ValidationError):
            Query(bbox=bbox)

    def test_bad_limit(self) -> None:
        with pytest.raises(ValidationError):
            Query(limit=0)


class TestProduct:
    def test_raw_excluded_from_repr(self) -> None:
        p = Product(id="x", name="X", raw={"secret_ish": "payload"})
        assert "payload" not in repr(p)


def _product(i: int) -> Product:
    return Product(id=f"p{i}", name=f"P{i}")


class ScriptedFetcher:
    """fetch_page stub serving fixed pages, counting calls."""

    def __init__(self, pages: list[list[Product]]) -> None:
        self._pages = pages
        self.calls = 0

    def __call__(self, token: Any | None) -> Page:
        self.calls += 1
        index = 0 if token is None else int(token)
        is_last = index == len(self._pages) - 1
        return Page(self._pages[index], next_token=None if is_last else index + 1)


class TestSearchResult:
    def test_lazy_no_fetch_before_iteration(self) -> None:
        fetcher = ScriptedFetcher([[_product(1)]])
        SearchResult(fetcher)
        assert fetcher.calls == 0

    def test_iterates_all_pages_in_order(self) -> None:
        fetcher = ScriptedFetcher([[_product(1), _product(2)], [_product(3)]])
        result = SearchResult(fetcher)
        assert [p.id for p in result] == ["p1", "p2", "p3"]
        assert fetcher.calls == 2

    def test_reiteration_fetches_nothing(self) -> None:
        fetcher = ScriptedFetcher([[_product(1)], [_product(2)]])
        result = SearchResult(fetcher)
        first = [p.id for p in result]
        second = [p.id for p in result]
        assert first == second == ["p1", "p2"]
        assert fetcher.calls == 2

    def test_partial_then_full_iteration_fetches_each_page_once(self) -> None:
        fetcher = ScriptedFetcher([[_product(1)], [_product(2)], [_product(3)]])
        result = SearchResult(fetcher)
        it = iter(result)
        next(it)  # only page 1 needed
        assert fetcher.calls == 1
        assert [p.id for p in result] == ["p1", "p2", "p3"]
        assert fetcher.calls == 3

    def test_len_with_and_without_count(self) -> None:
        fetcher = ScriptedFetcher([[_product(1)]])
        assert len(SearchResult(fetcher, matched=42)) == 42
        with pytest.raises(TypeError):
            len(SearchResult(fetcher))

    def test_pages(self) -> None:
        fetcher = ScriptedFetcher([[_product(1), _product(2)], [_product(3)]])
        result = SearchResult(fetcher)
        sizes = [len(page) for page in result.pages()]
        assert sizes == [2, 1]
