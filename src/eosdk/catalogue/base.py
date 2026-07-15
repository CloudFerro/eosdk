"""Catalogue protocol (SPEC §6.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from eosdk.models import Collection, Product, Query, Queryable, SearchResult


class Catalogue(Protocol):
    def search(self, query: Query) -> SearchResult: ...

    def get(self, product_id: str) -> Product: ...

    def collections(self) -> list[Collection]: ...

    def queryables(self, collection: str) -> list[Queryable]: ...
