"""Thin STAC API ItemSearch client over the shared transport (SPEC §6.5).

No pystac-client: the landing page is fetched lazily (once), conformance is
verified, and the search endpoint is taken from the ``rel="search"`` link
rather than hard-coded. Pagination follows STAC ``rel="next"`` links,
including the POST body-merge style.
"""

from __future__ import annotations

import difflib
import os.path
import re
from typing import TYPE_CHECKING, Any

from eosdk.catalogue.query import PROPERTY_ALIASES, parse_filters
from eosdk.exceptions import (
    CollectionNotFound,
    EosdkError,
    ProductNotFound,
    UnsupportedApiVersion,
    UnsupportedQueryFeature,
)
from eosdk.models import Checksum, Collection, Page, Product, Query, Queryable, SearchResult
from eosdk.transport import route

if TYPE_CHECKING:
    from eosdk.auth.base import CredentialsProvider
    from eosdk.transport import Transport

ITEM_SEARCH_CONFORMANCE = re.compile(r"https://api\.stacspec\.org/v1\.[\w.-]+/item-search")
QUERY_EXT_CONFORMANCE = re.compile(r"https://api\.stacspec\.org/v1\.[\w.-]+/item-search#query")

# Multihash codes -> checksum algorithm. CDSE uses md5 (0xd5, varint-encoded
# as d5 01) for product zips and sha3-256 (0x16) for individual assets.
_MULTIHASH_ALGORITHMS = {0xD5: "md5", 0x11: "sha1", 0x12: "sha256", 0x16: "sha3-256"}


def _read_uvarint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode an unsigned varint; returns (value, bytes consumed)."""
    value = shift = 0
    for consumed, byte in enumerate(data[offset:], start=1):
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, consumed
        shift += 7
        if shift > 28:
            break
    raise ValueError("invalid varint")


def decode_multihash(value: str) -> Checksum | None:
    """Decode a hex multihash (STAC ``file:checksum``) into a :class:`Checksum`.

    Real CDSE values use varint-encoded codes (e.g. ``d5 01 10 <16 bytes>`` for
    md5). Unknown hash codes degrade to ``None`` rather than failing a search.
    """
    try:
        data = bytes.fromhex(value)
        code, consumed = _read_uvarint(data)
        length, length_bytes = _read_uvarint(data, consumed)
    except ValueError:
        return None
    digest = data[consumed + length_bytes :]
    algorithm = _MULTIHASH_ALGORITHMS.get(code)
    if algorithm is None or len(digest) != length or not digest:
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
                    search_method = str(link.get("method", "POST")).upper()
                    if search_method == "POST":
                        break  # prefer POST when both advertised (CDSE lists GET first)
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
            body["datetime"] = _normalize_interval(query.datetime)
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
        remaining = query.limit

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
            if token is None and not products and query.collection is not None:
                self._raise_if_unknown_collection(query.collection)
            nonlocal remaining
            if remaining is not None:
                products = products[:remaining]
                remaining -= len(products)
                if remaining <= 0:
                    return Page(products, next_token=None)
            return Page(products, next_token=_next_token(page_doc, body))

        result = SearchResult(fetch_page)
        return result

    def raw_search(self, body: dict[str, Any]) -> Any:
        """Escape hatch: POST an arbitrary ItemSearch body, return raw JSON."""
        return self._post(self.capabilities().search_url, body)

    def _raise_if_unknown_collection(self, collection: str) -> None:
        """Distinguish "no products matched" from "no such collection".

        STAC servers (CDSE included) answer a search over an unknown
        collection with an empty FeatureCollection, not an error — a classic
        silent-zero trap. An empty first page costs one extra GET to tell the
        two apart; verification failures degrade to the plain empty result.
        """
        url = route(self._base, "collections/{id}", id=collection)
        try:
            response = self._transport.request(
                "GET",
                url,
                service="catalogue_stac",
                auth=self._auth.httpx_auth() if self._auth else None,
            )
        except EosdkError:
            return
        if response.status_code != 404:
            return
        hint = None
        if collection == collection.upper():
            hint = (
                "mission-level names like this are OData vocabulary — "
                "use the odata protocol, or a STAC collection id"
            )
        raise CollectionNotFound(
            collection=collection,
            backend=self.backend,
            suggestions=self._similar_collections(collection),
            hint=hint,
        )

    def _similar_collections(self, name: str) -> list[str]:
        try:
            ids = [c.id for c in self.collections()]
        except EosdkError:
            return []
        lowered = name.lower()
        prefixed = [i for i in ids if i.lower().startswith(lowered)]
        if prefixed:
            return prefixed[:8]
        return difflib.get_close_matches(lowered, ids, n=5, cutoff=0.6)

    def get(self, product_id: str) -> Product:
        body = {"ids": [product_id], "limit": 1}
        document = self._post(self.capabilities().search_url, body)
        features = document.get("features", [])
        if not features:
            raise ProductNotFound(product_id=product_id, backend=self.backend)
        return _item_to_product(features[0])

    def collections(self) -> list[Collection]:
        # /collections may paginate via rel="next" links (CDSE serves 10/page);
        # follow them all, guarding against a server echoing the same href.
        out: list[Collection] = []
        url: str | None = route(self._base, "collections")
        visited: set[str] = set()
        while url is not None and url not in visited:
            visited.add(url)
            document = self._get(url)
            for entry in document.get("collections", []):
                extent = entry.get("extent", {})
                out.append(
                    Collection(
                        id=entry["id"],
                        title=entry.get("title"),
                        description=entry.get("description"),
                        extent_spatial=extent.get("spatial", {}).get("bbox"),
                        extent_temporal=extent.get("temporal", {}).get("interval"),
                        raw=entry,
                    )
                )
            url = next(
                (
                    link.get("href")
                    for link in document.get("links", [])
                    if link.get("rel") == "next"
                ),
                None,
            )
        return out

    def queryables(self, collection: str) -> list[Queryable]:
        """Filterable attributes from the OGC ``/queryables`` endpoint (a JSON Schema)."""
        url = route(self._base, "collections/{id}/queryables", id=collection)
        response = self._transport.request(
            "GET",
            url,
            service="catalogue_stac",
            auth=self._auth.httpx_auth() if self._auth else None,
        )
        if response.status_code == 404:  # optional STAC extension; not every server has it
            raise UnsupportedQueryFeature(
                backend=self.backend, feature=f"queryables for collection {collection!r}"
            )
        response.raise_for_status()
        document = response.json()
        return [
            Queryable(name=name, type=_queryable_type(schema), raw=schema)
            for name, schema in document.get("properties", {}).items()
        ]


def _queryable_type(schema: Any) -> str | None:
    """Normalize a queryable's JSON-Schema fragment to a simple type name."""
    if not isinstance(schema, dict):
        return None
    if schema.get("format") == "date-time":
        return "datetime"
    declared = schema.get("type")
    return declared if isinstance(declared, str) else None


class _StacCapabilities:
    def __init__(self, *, search_url: str, search_method: str, query_extension: bool) -> None:
        self.search_url = search_url
        self.search_method = search_method
        self.query_extension = query_extension


def _normalize_interval(value: str) -> str:
    """Expand bare dates to RFC 3339 instants (CDSE rejects date-only bounds)."""

    def bound(ts: str, *, end: bool) -> str:
        ts = ts.strip()
        if not ts or ts == "..":
            return ".."
        if "T" not in ts:
            return f"{ts}T23:59:59Z" if end else f"{ts}T00:00:00Z"
        return ts if ts.endswith("Z") or "+" in ts[10:] else f"{ts}Z"

    if "/" in value:
        start, _, end = value.partition("/")
        return f"{bound(start, end=False)}/{bound(end, end=True)}"
    return f"{bound(value, end=False)}/{bound(value, end=True)}"


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


_PRODUCTS_UUID = re.compile(r"/Products\(([^)]+)\)")


def _download_id(item: dict[str, Any]) -> str:
    """Extract the identifier the HTTP download route needs.

    On CDSE the product UUID lives in the ``Product`` asset's href
    (``https://download.../odata/v1/Products(<uuid>)/$value``) — verified
    against the live STAC API. Property-based fallbacks keep other
    deployments working; the item id is the last resort.
    """
    for asset in _candidate_assets(item):
        match = _PRODUCTS_UUID.search(str(asset.get("href", "")))
        if match:
            return match.group(1)
        alternate = asset.get("alternate") or {}
        for alt in alternate.values():
            match = _PRODUCTS_UUID.search(str(alt.get("href", "")))
            if match:
                return match.group(1)
    properties = item.get("properties", {})
    for key in ("eodata:uuid", "odata:id", "uuid", "id"):
        value = properties.get(key)
        if value:
            return str(value)
    return str(item["id"])


def _candidate_assets(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Assets most likely to describe the whole product, most specific first."""
    assets = item.get("assets", {})
    ordered = [assets[name] for name in ("Product", "PRODUCT", "product") if name in assets]
    ordered.extend(a for a in assets.values() if a not in ordered)
    return ordered


def _product_s3_path(item: dict[str, Any], primary: dict[str, Any]) -> str | None:
    """Whole-product S3 root.

    Derived from the common prefix of the item's ``s3://`` asset hrefs (on
    CDSE every asset lives under ``s3://eodata/.../<product>.SAFE/``); a
    path-like ``file:local_path`` is used when no s3 hrefs exist. Verified
    against the live CDSE STAC API — its Product ``file:local_path`` is just
    the zip filename, not a path.
    """
    s3_hrefs = [
        str(asset.get("href", ""))
        for asset in item.get("assets", {}).values()
        if str(asset.get("href", "")).startswith("s3://")
    ]
    if s3_hrefs:
        prefix = os.path.commonprefix(s3_hrefs)
        if len(s3_hrefs) == 1:
            prefix = prefix.rsplit("/", 1)[0]  # a single file: its directory
        return prefix.rstrip("/") or None
    local_path = str(primary.get("file:local_path") or "")
    if local_path.startswith(("s3://", "/")):
        return local_path
    for asset in item.get("assets", {}).values():
        alternate = (asset.get("alternate") or {}).get("s3", {}).get("href", "")
        if alternate.startswith(("s3://", "/eodata")):
            return str(alternate)
    return None


def _item_to_product(item: dict[str, Any]) -> Product:
    properties = item.get("properties", {})
    primary: dict[str, Any] = next(iter(_candidate_assets(item)), {})

    checksum = None
    multihash = primary.get("file:checksum") or properties.get("file:checksum")
    if multihash:
        checksum = decode_multihash(multihash)

    s3_path = _product_s3_path(item, primary)

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
