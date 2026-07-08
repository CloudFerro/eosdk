"""OData catalogue backend (SPEC §6.5), Copernicus CSC dialect.

Pagination prefers the service's ``@odata.nextLink`` and falls back to
``$skip`` arithmetic; ``len()`` is fed by ``@odata.count`` requested on the
first page. Version-keyed route constants are ready for the Phase-3 version
guard.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from eosdk.catalogue._odata_filter import build_query_params
from eosdk.exceptions import ProductNotFound
from eosdk.models import Checksum, Collection, Page, Product, SearchResult
from eosdk.transport import route

if TYPE_CHECKING:
    from eosdk.auth.base import CredentialsProvider
    from eosdk.models import Query
    from eosdk.transport import Transport

ROUTES = {
    "v1": {
        "products": "odata/v1/Products",
        "product_by_id": "odata/v1/Products({id})",
        "collections": "odata/v1/Collections",
    }
}

_CHECKSUM_ALGORITHMS = {"md5": "md5", "sha1": "sha1", "sha256": "sha256", "sha3-256": "sha3-256"}


class ODataCatalogue:
    """OData backend implementing the :class:`~eosdk.catalogue.base.Catalogue` protocol."""

    backend = "odata"
    api_version = "v1"

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport,
        auth: CredentialsProvider | None = None,
        page_size: int = 100,
    ) -> None:
        self._base = base_url
        self._transport = transport
        self._auth = auth
        self._page_size = page_size

    def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        response = self._transport.request(
            "GET",
            url,
            service="catalogue_odata",
            auth=self._auth.httpx_auth() if self._auth else None,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    # -- public surface --------------------------------------------------------

    def search(self, query: Query) -> SearchResult:
        params = build_query_params(query)  # translates (and fails) before any HTTP
        params.setdefault("$top", str(self._page_size))
        products_url = route(self._base, ROUTES[self.api_version]["products"])
        remaining = query.limit

        def fetch_page(token: Any | None) -> Page:
            # token is the absolute @odata.nextLink after the first page
            document = self._get(products_url, params) if token is None else self._get(token)
            count = document.get("@odata.count")
            if count is not None:
                result._matched = int(count)
            entries = document.get("value", [])
            products = [_entry_to_product(entry) for entry in entries]
            nonlocal remaining
            if remaining is not None:
                products = products[:remaining]
                remaining -= len(products)
                if remaining <= 0:
                    return Page(products, next_token=None)
            return Page(products, next_token=document.get("@odata.nextLink"))

        result = SearchResult(fetch_page)
        return result

    def get(self, product_id: str) -> Product:
        # CDSE addresses products by bare (unquoted) UUID: Products(<uuid>)
        url = route(self._base, ROUTES[self.api_version]["product_by_id"], id=product_id)
        response = self._transport.request(
            "GET",
            url,
            service="catalogue_odata",
            auth=self._auth.httpx_auth() if self._auth else None,
            params={"$expand": "Attributes"},
        )
        if response.status_code == 404:
            raise ProductNotFound(product_id=product_id, backend=self.backend)
        response.raise_for_status()
        return _entry_to_product(response.json())

    def collections(self) -> list[Collection]:
        document = self._get(route(self._base, ROUTES[self.api_version]["collections"]))
        return [
            Collection(
                id=entry.get("Name", entry.get("Id", "")),
                title=entry.get("DisplayName") or entry.get("Name"),
                description=entry.get("Description"),
            )
            for entry in document.get("value", [])
        ]

    def query_raw(self, odata_query: str) -> dict[str, Any]:
        """Escape hatch: run ``"Products?$filter=..."`` verbatim, return raw JSON.

        The caller-provided path/query is passed through untouched — this is
        the sanctioned exception to the route()-only rule (SPEC §6.5).
        """
        base = route(self._base, "odata/v1")
        path, _, query_string = odata_query.partition("?")
        params = dict(pair.split("=", 1) for pair in query_string.split("&") if "=" in pair)
        document = self._get(f"{base}/{path.lstrip('/')}", params or None)
        return dict(document)


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _entry_to_product(entry: dict[str, Any]) -> Product:
    checksum = None
    for entry_checksum in entry.get("Checksum") or []:
        algorithm = str(entry_checksum.get("Algorithm", "")).lower().replace("_", "-")
        if algorithm in _CHECKSUM_ALGORITHMS and entry_checksum.get("Value"):
            checksum = Checksum(algorithm=algorithm, value=entry_checksum["Value"])  # type: ignore[arg-type]
            break

    cloud_cover = None
    for attribute in entry.get("Attributes") or []:
        if attribute.get("Name") == "cloudCover":
            try:
                cloud_cover = float(attribute.get("Value"))
            except (TypeError, ValueError):
                cloud_cover = None
            break

    geometry = None
    footprint = entry.get("GeoFootprint")
    if isinstance(footprint, dict):
        geometry = footprint

    # Exos identifier: prefer S3Path, fall back to Locations (risk: field name
    # varies per deployment — normalize whichever staging returns).
    s3_path = entry.get("S3Path")
    if not s3_path:
        locations = entry.get("Locations") or []
        for location in locations:
            if str(location.get("FormatType", "")).lower() in ("", "extracted", "s3"):
                s3_path = location.get("S3Path") or location.get("Path")
                if s3_path:
                    break

    return Product(
        id=str(entry.get("Id", "")),
        name=str(entry.get("Name", entry.get("Id", ""))),
        collection=(entry.get("Collection") or {}).get("Name")
        if isinstance(entry.get("Collection"), dict)
        else None,
        size=entry.get("ContentLength"),
        geometry=geometry,
        datetime=_parse_datetime((entry.get("ContentDate") or {}).get("Start")),
        cloud_cover=cloud_cover,
        checksum=checksum,
        s3_path=s3_path,
        raw=entry,
    )
