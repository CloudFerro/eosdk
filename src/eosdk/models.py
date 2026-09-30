"""Protocol-agnostic domain models.

All models are returned identically by every catalogue backend. ``SearchResult``
is a lazy, re-iterable sequence of :class:`Product`: pages are fetched on demand
through an injected callable and cached, so the result can be traversed more
than once without re-querying.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

ChecksumAlgorithm = Literal["md5", "sha1", "sha256", "sha3-256"]


class Checksum(BaseModel):
    algorithm: ChecksumAlgorithm
    value: str

    @field_validator("value")
    @classmethod
    def _normalize_hex(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("checksum value must be a hex string")
        return v


class Query(BaseModel):
    """Backend-neutral search query; translated per catalogue backend."""

    collection: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    datetime: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    limit: int | None = None
    sort: str | None = None

    @field_validator("bbox")
    @classmethod
    def _check_bbox(
        cls, v: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if v is None:
            return v
        minx, miny, maxx, maxy = v
        if minx >= maxx or miny >= maxy:
            raise ValueError("bbox must be (minx, miny, maxx, maxy) with min < max")
        if not (-90 <= miny <= 90 and -90 <= maxy <= 90):
            raise ValueError("bbox latitudes must be within [-90, 90]")
        return v

    @field_validator("limit")
    @classmethod
    def _check_limit(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit must be positive")
        return v


class Product(BaseModel):
    """Normalized catalogue item, carrying the identifiers every backend needs."""

    id: str
    name: str
    collection: str | None = None
    size: int | None = None
    geometry: dict[str, Any] | None = None
    datetime: dt.datetime | None = None
    cloud_cover: float | None = None
    checksum: Checksum | None = None
    s3_path: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class Node(BaseModel):
    """A file or directory inside a product's internal tree.

    ``path`` is logical and backend-agnostic (relative to the product root);
    each backend translates it to its own addressing.
    """

    name: str
    path: str
    size: int | None = None
    is_dir: bool = False
    checksum: Checksum | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class Collection(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None
    extent_spatial: list[list[float]] | None = None
    extent_temporal: list[list[dt.datetime | None]] | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class Queryable(BaseModel):
    """A filterable attribute advertised by a catalogue backend.

    ``name`` is backend-native (e.g. ``eo:cloud_cover`` on STAC, ``cloudCover``
    on OData) and is accepted as-is in ``Query.filters`` for that backend.
    ``type`` is normalized to ``string | number | integer | boolean | datetime``
    when the backend declares one; ``raw`` keeps the untouched declaration.
    """

    name: str
    type: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


@dataclass
class Page:
    """One page of search results plus the token addressing the next page."""

    products: list[Product]
    next_token: Any | None = None


@dataclass
class _PageCache:
    pages: list[list[Product]] = field(default_factory=list)
    next_token: Any | None = None
    exhausted: bool = False


class SearchResult:
    """Lazy, re-iterable, thread-safe sequence of :class:`Product`.

    ``fetch_page`` is called with ``None`` for the first page and with the
    previous page's ``next_token`` afterwards; a page with ``next_token=None``
    ends the sequence. Fetched pages are cached, so iterating twice performs
    no additional requests.
    """

    def __init__(
        self,
        fetch_page: Callable[[Any | None], Page],
        *,
        matched: int | None = None,
    ) -> None:
        self._fetch_page = fetch_page
        self._matched = matched
        self._cache = _PageCache()
        self._lock = threading.Lock()

    @property
    def matched(self) -> int | None:
        """Total match count as reported by the backend, when available."""
        return self._matched

    def __len__(self) -> int:
        if self._matched is None:
            self._page_at(0)  # the count usually arrives with the first page
        if self._matched is None:
            raise TypeError("this backend did not provide a result count")
        return self._matched

    def _page_at(self, index: int) -> list[Product] | None:
        """Return cached page ``index``, fetching forward as needed."""
        with self._lock:
            while len(self._cache.pages) <= index and not self._cache.exhausted:
                page = self._fetch_page(self._cache.next_token)
                self._cache.pages.append(page.products)
                self._cache.next_token = page.next_token
                if page.next_token is None:
                    self._cache.exhausted = True
            if index < len(self._cache.pages):
                return self._cache.pages[index]
            return None

    def pages(self) -> Iterator[list[Product]]:
        index = 0
        while (page := self._page_at(index)) is not None:
            yield page
            index += 1

    def __iter__(self) -> Iterator[Product]:
        for page in self.pages():
            yield from page
