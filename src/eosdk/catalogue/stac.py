"""Thin STAC API ItemSearch client over the shared transport (SPEC §6.5).

No pystac-client: the landing page is fetched lazily (once), conformance is
verified, and the search endpoint is taken from the ``rel="search"`` link
rather than hard-coded. Pagination follows STAC ``rel="next"`` links,
including the POST body-merge style.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from eosdk.catalogue.query import PROPERTY_ALIASES, parse_filters
from eosdk.exceptions import ProductNotFound, UnsupportedApiVersion, UnsupportedQueryFeature
from eosdk.models import Checksum, Collection, Page, Product, Query, SearchResult
from eosdk.transport import route

if TYPE_CHECKING:
    from eosdk.auth.base import CredentialsProvider
    from eosdk.transport import Transport

ITEM_SEARCH_CONFORMANCE = re.compile(r"https://api\.stacspec\.org/v1\.[\w.-]+/item-search")
QUERY_EXT_CONFORMANCE = re.compile(r"https://api\.stacspec\.org/v1\.[\w.-]+/item-search#query")

# Multihash prefix (code, length byte) -> checksum algorithm (plan risk R7).
_MULTIHASH_ALGORITHMS = {0xD5: "md5", 0x11: "sha1", 0x12: "sha256"}


def decode_multihash(value: str) -> Checksum | None:
    """Decode a hex multihash (STAC ``file:checksum``) into a :class:`Checksum`.

    Unknown hash codes degrade to ``None`` rather than failing the search.
    """
    try:
        data = bytes.fromhex(value)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    algorithm = _MULTIHASH_ALGORITHMS.get(data[0])
    digest = data[2:]
    if algorithm is None or len(digest) != data[1]:
        return None
    return Checksum(algorithm=algorithm, value=digest.hex())  # type: ignore[arg-type]


class StacCatalogue:
    """STAC API backend implementing the :class:`~eosdk.catalogue.base.Catalogue` protocol."""

    backend = "stac"

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport,
        auth: CredentialsProvider | None = None,
    ) -> None:
        self._base = base_url
        self._transport = transport
        self._auth = auth
        self._capabilities: _StacCapabilities | None = None

    # -- landing page ---------------------------------------------------------

    def _get(self, url: str, **kwargs: Any) -> Any:
        response = self._transport.request(
            "GET",
            url,
            service="catalogue_stac",
            auth=self._auth.httpx_auth() if self._auth else None,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def _post(self, url: str, body: dict[str, Any]) -> Any:
        response = self._transport.request(
            "POST",
            url,
            service="catalogue_stac",
            auth=self._auth.httpx_auth() if self._auth else None,
            json=body,
        )
        response.raise_for_status()
        return response.json()

    def capabilities(self) -> _StacCapabilities:
        """Fetch + memoize the landing page; verify item-search conformance."""
        if self._capabilities is None:
            landing = self._get(self._base)
            conforms = landing.get("conformsTo", [])
            if not any(ITEM_SEARCH_CONFORMANCE.match(c) for c in conforms):
                raise UnsupportedApiVersion(
                    service="catalogue_stac",
                    advertised=", ".join(conforms[:3]) or "unknown",
                    supported="STAC API 1.x item-search",
                    remediation="check that the catalogue_stac URL points at a STAC API root",
                )
            search_url = None
            search_method = "POST"
            for link in landing.get("links", []):
                if link.get("rel") == "search":
                    search_url = link["href"]
                    method = str(link.get("method", "POST")).upper()
                    if method == "POST":
                        break  # prefer POST when both advertised
                    search_method = method
            if search_url is None:
                search_url = route(self._base, "search")
            self._capabilities = _StacCapabilities(
                search_url=search_url,
                search_method=search_method,
                query_extension=any(QUERY_EXT_CONFORMANCE.match(c) for c in conforms),
            )
        return self._capabilities

    # -- query translation ----------------------------------------------------

    def _build_body(self, query: Query) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if query.collection is not None:
            body["collections"] = [query.collection]
        if query.bbox is not None:
            body["bbox"] = list(query.bbox)
        if query.datetime is not None:
            body["datetime"] = query.datetime
        if query.limit is not None:
            body["limit"] = query.limit
        if query.sort is not None:
            direction = "desc" if query.sort.startswith("-") else "asc"
            fieldname = query.sort.lstrip("+-")
            body["sortby"] = [
                {"field": PROPERTY_ALIASES.get(fieldname, fieldname), "direction": direction}
            ]
        if query.filters:
            if not self.capabilities().query_extension:
                raise UnsupportedQueryFeature(
                    backend=self.backend,
                    feature="attribute filters (the service does not advertise the "
                    "item-search query extension)",
                )
            query_obj: dict[str, dict[str, Any]] = {}
            for clause in parse_filters(query.filters, backend=self.backend):
                prop = PROPERTY_ALIASES.get(clause.prop, clause.prop)
                query_obj.setdefault(prop, {})[clause.op] = clause.value
            body["query"] = query_obj
        return body

    # -- public surface -------------------------------------------------------

    def search(self, query: Query) -> SearchResult:
        body = self._build_body(query)  # translate (and fail) before any search request
        capabilities = self.capabilities()

        def fetch_page(token: Any | None) -> Page:
            if token is None:
                if capabilities.search_method == "GET":
                    page_doc = self._get(capabilities.search_url, params=_body_to_params(body))
                else:
                    page_doc = self._post(capabilities.search_url, body)
            else:
                href, method, merged_body = token
                page_doc = self._get(href) if method == "GET" else self._post(href, merged_body)
            matched = page_doc.get("numberMatched") or page_doc.get("context", {}).get("matched")
            if matched is not None:
                result._matched = int(matched)  # backend count feeds len() lazily
            products = [_item_to_product(item) for item in page_doc.get("features", [])]
            return Page(products, next_token=_next_token(page_doc, body))

        result = SearchResult(fetch_page)
        return result

    def raw_search(self, body: dict[str, Any]) -> Any:
        """Escape hatch: POST an arbitrary ItemSearch body, return raw JSON."""
        return self._post(self.capabilities().search_url, body)

    def get(self, product_id: str) -> Product:
        body = {"ids": [product_id], "limit": 1}
        document = self._post(self.capabilities().search_url, body)
        features = document.get("features", [])
        if not features:
            raise ProductNotFound(product_id=product_id, backend=self.backend)
        return _item_to_product(features[0])

    def collections(self) -> list[Collection]:
        document = self._get(route(self._base, "collections"))
        out: list[Collection] = []
        for entry in document.get("collections", []):
            extent = entry.get("extent", {})
            out.append(
                Collection(
                    id=entry["id"],
                    title=entry.get("title"),
                    description=entry.get("description"),
                    extent_spatial=extent.get("spatial", {}).get("bbox"),
                    extent_temporal=extent.get("temporal", {}).get("interval"),
                )
            )
        return out


class _StacCapabilities:
    def __init__(self, *, search_url: str, search_method: str, query_extension: bool) -> None:
        self.search_url = search_url
        self.search_method = search_method
        self.query_extension = query_extension


def _body_to_params(body: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in body.items():
        if key in ("bbox", "collections", "ids"):
            params[key] = ",".join(str(v) for v in value)
        elif key == "sortby":
            params[key] = ",".join(
                ("-" if s["direction"] == "desc" else "") + s["field"] for s in value
            )
        elif key == "query":
            raise UnsupportedQueryFeature(
                backend="stac",
                feature="attribute filters over a GET-only search endpoint",
            )
        else:
            params[key] = str(value)
    return params


def _next_token(page_doc: dict[str, Any], original_body: dict[str, Any]) -> Any | None:
    """Resolve the STAC paging ``next`` link into an opaque token."""
    for link in page_doc.get("links", []):
        if link.get("rel") == "next":
            method = str(link.get("method", "GET")).upper()
            body = dict(original_body)
            if link.get("merge", False):
                body.update(link.get("body", {}))
            elif "body" in link:
                body = dict(link["body"])
            return (link["href"], method, body)
    return None


def _download_id(item: dict[str, Any]) -> str:
    """Extract the identifier the Zipper download route needs.

    Deployment-sensitive (plan risk R2): STAC item ids are often product names
    while Zipper's OData route expects a UUID. Fallback chain, most explicit
    first; adjust here once verified against the real service.
    """
    properties = item.get("properties", {})
    for key in ("eodata:uuid", "odata:id", "uuid", "id"):
        value = properties.get(key)
        if value:
            return str(value)
    return str(item["id"])


def _item_to_product(item: dict[str, Any]) -> Product:
    properties = item.get("properties", {})
    assets = item.get("assets", {})
    primary: dict[str, Any] = (
        assets.get("PRODUCT") or assets.get("product") or next(iter(assets.values()), {})
    )

    checksum = None
    multihash = primary.get("file:checksum") or properties.get("file:checksum")
    if multihash:
        checksum = decode_multihash(multihash)

    s3_path = None
    for asset in assets.values():
        href = asset.get("href", "")
        if href.startswith("s3://"):
            s3_path = href
            break
        alternate = asset.get("alternate", {}).get("s3", {}).get("href", "")
        if alternate.startswith("s3://") or alternate.startswith("/eodata"):
            s3_path = alternate
            break

    return Product(
        id=_download_id(item),
        name=str(item["id"]),
        collection=item.get("collection"),
        size=primary.get("file:size") or properties.get("file:size"),
        geometry=item.get("geometry"),
        datetime=properties.get("datetime") or properties.get("start_datetime"),
        cloud_cover=properties.get("eo:cloud_cover"),
        checksum=checksum,
        s3_path=s3_path,
        raw=item,
    )
