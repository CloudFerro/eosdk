"""FakePlatform: one in-memory description of a platform, projected into every wire format.

The point of the harness is that STAC items, OData entries, the download
service's ``Nodes`` tree and the S3 keys all derive from the *same*
:class:`FakeProduct` objects. A parity assertion between two protocols or two
backends therefore compares two independent projections of one source of truth,
instead of comparing two hand-written fixtures that were made to agree.

Everything is served by one pure router — :meth:`FakePlatform.handle` takes
``(method, url, headers, body)`` and returns a :class:`FakeResponse`. Two
adapters drive it:

* :func:`respx_router` — in-process, for the hermetic ``e2e`` suite;
* ``tests.e2e.httpstub`` — a real socket server, for the out-of-process CLI
  tests where respx cannot reach (it patches httpx inside *this* process).

S3 is the one service the router does not own: it is spoken by botocore, not
httpx, so it is served by moto (:func:`seed_s3`) in-process and by
``moto.server`` out-of-process.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import threading
import zipfile
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, unquote, urlencode

from tests.eodata import safe_tree

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

# -- hosts ---------------------------------------------------------------------
# The example.eu family the repository's fixtures already use, so a FakePlatform
# journey and a hand-written respx test address the same names.

PLATFORM = "https://platform.example.eu"
KEYCLOAK = "https://auth.example.eu"
REALM = "eodata"
CLIENT_ID = "example-public"
STAC = "https://catalogue.example.eu/stac"
ODATA = "https://catalogue.example.eu/odata"
EODATA_HTTP = "https://download.example.eu"
KEYS = "https://keys.example.eu/api"
# moto intercepts botocore at the AWS endpoints; the region is part of the name.
S3_ENDPOINT = "https://s3.us-east-1.amazonaws.com"
S3_REGION = "us-east-1"
BUCKET = "eodata"

PLATFORM_NAME = "example-eu"
PLATFORM_DESCRIPTION = "Example Earth-observation data platform (example.eu)"
WELL_KNOWN_PATH = "/.well-known/eo-services.json"

USERNAME = "alice"
PASSWORD = "s3cret"

# Services the router knows about; also the keys accepted by fault injection.
Service = Literal["platform", "keycloak", "stac", "odata", "eodata_http", "keys", "s3"]


@dataclass(frozen=True)
class ServiceUrls:
    """Where each service lives. Swapped wholesale for the subprocess stub."""

    platform: str = PLATFORM
    keycloak: str = KEYCLOAK
    stac: str = STAC
    odata: str = ODATA
    eodata_http: str = EODATA_HTTP
    keys: str = KEYS
    s3_endpoint: str = S3_ENDPOINT
    s3_region: str = S3_REGION

    def as_pairs(self) -> list[tuple[str, str]]:
        """(service, base url), longest base first — prefix matching needs that."""
        pairs = [
            ("keycloak", self.keycloak),
            ("stac", self.stac),
            ("odata", self.odata),
            ("eodata_http", self.eodata_http),
            ("keys", self.keys),
            ("s3", self.s3_endpoint),
            ("platform", self.platform),
        ]
        return sorted(pairs, key=lambda pair: len(pair[1]), reverse=True)


# -- the canonical attribute vocabulary ----------------------------------------
# Both catalogues describe the same attributes under their own names; the
# harness stores one canonical set per product and translates on projection.

CANONICAL_ATTRIBUTES = {
    "cloudCover": "cloudCover",
    "cloud_cover": "cloudCover",
    "eo:cloud_cover": "cloudCover",
    "productType": "productType",
    "product:type": "productType",
    "platformShortName": "platformShortName",
    "platform": "platformShortName",
}

STAC_PROPERTY_NAMES = {
    "cloudCover": "eo:cloud_cover",
    "productType": "product:type",
    "platformShortName": "platform",
}

ODATA_VALUE_TYPES = {float: "Double", int: "Integer", str: "String", bool: "Boolean"}


def canonical(name: str) -> str:
    return CANONICAL_ATTRIBUTES.get(name, name)


# -- products ------------------------------------------------------------------

MINI_TREE: dict[str, bytes] = {
    "manifest.safe": b"<manifest/>" * 8,
    "MTD.xml": b"<metadata/>" * 12,
    "measurement/band.tif": b"\x49\x49\x2a\x00" + b"TIFF" * 256,
}


@dataclass
class FakeProduct:
    """One product, projected into STAC, OData, Nodes and S3 by the platform."""

    uuid: str
    name: str
    collection: str = "SENTINEL-2"
    # STAC and OData vocabularies coincide by default so parity assertions are
    # meaningful; the examples fixture overrides this to mirror CDSE, where they
    # genuinely differ (`sentinel-2-l2a` vs `SENTINEL-2`).
    stac_collection: str | None = None
    datetime: str = "2026-06-15T09:50:29Z"
    bbox: tuple[float, float, float, float] = (22.5, 52.9, 24.0, 53.5)
    attributes: dict[str, float | str] = field(
        default_factory=lambda: {"cloudCover": 12.4, "productType": "S2MSI2A"}
    )
    tree: dict[str, bytes] = field(default_factory=lambda: dict(MINI_TREE))
    prefix: str | None = None
    bucket: str = BUCKET
    #: how the STAC item advertises the product's S3 root. ``assets`` derives it
    #: from ``s3://`` asset hrefs (the CDSE shape), ``local_path`` from the
    #: Product asset's ``file:local_path``, ``none`` advertises no S3 root.
    stac_s3_style: Literal["assets", "local_path", "none"] = "assets"
    #: seed this product into the S3 bucket
    in_s3: bool = True
    #: whether ``$value`` delivers a zip of the tree. Products that really are a
    #: single file are served as-is, which is the only shape where the HTTP and
    #: the S3 backend deliver the *same* bytes — and therefore the only shape
    #: where one product-level checksum can be honest about both (SPEC §6.6).
    payload_is_archive: bool = True
    _zip: bytes | None = field(default=None, repr=False)

    # -- derived ---------------------------------------------------------------

    @property
    def stac_collection_id(self) -> str:
        return self.stac_collection or self.collection

    @property
    def s3_prefix(self) -> str:
        return self.prefix or f"Sentinel-2/MSI/L2A/2026/06/15/{self.name}"

    @property
    def s3_path(self) -> str:
        """OData vocabulary: an absolute ``/bucket/key`` path."""
        return f"/{self.bucket}/{self.s3_prefix}"

    @property
    def s3_uri(self) -> str:
        """STAC vocabulary: an ``s3://`` URI."""
        return f"s3://{self.bucket}/{self.s3_prefix}"

    @property
    def zip_bytes(self) -> bytes:
        """The ``$value`` body: a real (deterministic) zip of the product tree."""
        if not self.payload_is_archive:
            if len(self.tree) != 1:
                raise ValueError("only a single-file product can be delivered unarchived")
            return next(iter(self.tree.values()))
        if self._zip is None:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
                for logical in sorted(self.tree):
                    info = zipfile.ZipInfo(
                        f"{self.name}/{logical}", date_time=(2026, 6, 15, 9, 50, 28)
                    )
                    archive.writestr(info, self.tree[logical])
            self._zip = buffer.getvalue()
        return self._zip

    @property
    def md5(self) -> str:
        return hashlib.md5(self.zip_bytes).hexdigest()

    @property
    def size(self) -> int:
        return len(self.zip_bytes)

    @property
    def zip_name(self) -> str:
        """The filename ``Content-Disposition`` advertises for ``$value``."""
        if not self.payload_is_archive:
            return next(iter(self.tree)).rsplit("/", 1)[-1]
        return f"{self.name}.zip"

    @property
    def cloud_cover(self) -> float | None:
        value = self.attributes.get("cloudCover")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def geometry(self) -> dict[str, Any]:
        minx, miny, maxx, maxy = self.bbox
        return {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
        }

    @property
    def instant(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.datetime.replace("Z", "+00:00"))

    def directories(self) -> list[str]:
        """Logical directory paths implied by :attr:`tree`."""
        found: set[str] = set()
        for logical in self.tree:
            parts = logical.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                found.add("/".join(parts[:depth]))
        return sorted(found)

    def children(self, path: str = "") -> list[tuple[str, bool, int | None]]:
        """(name, is_dir, size) of the immediate children of ``path``."""
        base = path.strip("/")
        prefix = f"{base}/" if base else ""
        entries: dict[str, tuple[bool, int | None]] = {}
        for logical, content in self.tree.items():
            if not logical.startswith(prefix):
                continue
            remainder = logical[len(prefix) :]
            if not remainder:
                continue
            head, _, rest = remainder.partition("/")
            if rest:
                entries.setdefault(head, (True, None))
            else:
                entries[head] = (False, len(content))
        return sorted((name, is_dir, size) for name, (is_dir, size) in entries.items())

    def child_count(self, path: str) -> int:
        return len(self.children(path))

    # -- projections -----------------------------------------------------------

    def as_stac_item(self, *, eodata_http: str, stac: str) -> dict[str, Any]:
        properties: dict[str, Any] = {"datetime": self.datetime}
        for name, value in self.attributes.items():
            properties[STAC_PROPERTY_NAMES.get(name, name)] = value

        product_asset: dict[str, Any] = {
            "href": f"{eodata_http}/odata/v1/Products({self.uuid})/$value",
            "type": "application/zip",
            "title": "Product",
            "roles": ["data", "metadata", "archive"],
            "file:size": self.size,
            # varint multihash: md5 = code d5 (varint d5 01), length 0x10
            "file:checksum": "d50110" + self.md5,
            "auth:refs": ["oidc"],
        }
        assets: dict[str, Any] = {"Product": product_asset}
        if self.stac_s3_style == "local_path":
            product_asset["file:local_path"] = self.s3_path
        elif self.stac_s3_style == "assets":
            for logical, content in sorted(self.tree.items()):
                assets[logical] = {
                    "href": f"{self.s3_uri}/{logical}",
                    "type": "application/octet-stream",
                    "file:size": len(content),
                    "file:checksum": "1220" + hashlib.sha256(content).hexdigest(),
                    "auth:refs": ["s3"],
                }
        return {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": self.name,
            "collection": self.stac_collection_id,
            "geometry": self.geometry,
            "bbox": list(self.bbox),
            "properties": properties,
            "assets": assets,
            "links": [
                {
                    "rel": "self",
                    "href": f"{stac}/collections/{self.stac_collection_id}/items/{self.name}",
                }
            ],
        }

    def as_odata_entry(self) -> dict[str, Any]:
        return {
            "@odata.mediaContentType": "application/octet-stream",
            "Id": self.uuid,
            "Name": self.name,
            "ContentType": "application/octet-stream",
            "ContentLength": self.size,
            "OriginDate": self.datetime,
            "PublicationDate": self.datetime,
            "ModificationDate": self.datetime,
            "Online": True,
            "EvictionDate": "",
            "S3Path": self.s3_path,
            "Checksum": [
                {
                    "Value": self.md5,
                    "Algorithm": "MD5",
                    "ChecksumDate": self.datetime,
                }
            ],
            "ContentDate": {"Start": self.datetime, "End": self.datetime},
            "Footprint": f"geography'SRID=4326;{_wkt_polygon(self.bbox)}'",
            "GeoFootprint": self.geometry,
            "Collection": {"Name": self.collection},
            "Attributes": [
                {
                    "@odata.type": f"#OData.CSC.{ODATA_VALUE_TYPES[type(value)]}Attribute",
                    "Name": name,
                    "Value": value,
                    "ValueType": ODATA_VALUE_TYPES[type(value)],
                }
                for name, value in self.attributes.items()
            ],
        }


def _wkt_polygon(bbox: tuple[float, float, float, float]) -> str:
    minx, miny, maxx, maxy = bbox
    ring = f"{minx} {miny},{maxx} {miny},{maxx} {maxy},{minx} {maxy},{minx} {miny}"
    return f"POLYGON(({ring}))"


@dataclass
class FakeCollection:
    id: str
    title: str
    description: str = ""
    bbox: list[float] = field(default_factory=lambda: [-180.0, -90.0, 180.0, 90.0])
    interval: list[str | None] = field(default_factory=lambda: ["2015-06-23T00:00:00Z", None])
    #: canonical attribute name -> JSON-Schema-ish type; projected per protocol
    queryables: dict[str, str] = field(
        default_factory=lambda: {
            "cloudCover": "number",
            "productType": "string",
            "platformShortName": "string",
        }
    )
    #: when False both ``/queryables`` (STAC) and ``Attributes()`` (OData) 404
    advertises_queryables: bool = True

    def as_stac(self) -> dict[str, Any]:
        return {
            "type": "Collection",
            "id": self.id,
            "stac_version": "1.0.0",
            "title": self.title,
            "description": self.description or self.title,
            "license": "proprietary",
            "extent": {
                "spatial": {"bbox": [self.bbox]},
                "temporal": {"interval": [self.interval]},
            },
            "links": [],
        }

    def as_stac_queryables(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "datetime": {"type": "string", "format": "date-time", "title": "Acquisition time"}
        }
        for name, type_name in self.queryables.items():
            properties[STAC_PROPERTY_NAMES.get(name, name)] = {"type": type_name, "title": name}
        return {
            "$schema": "https://json-schema.org/draft/2019-09/schema",
            "$id": f"queryables/{self.id}",
            "type": "object",
            "title": f"Queryables for {self.id}",
            "properties": properties,
            "additionalProperties": True,
        }

    def as_odata_attributes(self) -> list[dict[str, Any]]:
        types = {"number": "Double", "string": "String", "integer": "Integer"}
        return [
            {"Name": name, "ValueType": types.get(type_name, "String"), "Id": f"{self.id}-{name}"}
            for name, type_name in self.queryables.items()
        ]


# -- responses -----------------------------------------------------------------


@dataclass
class FakeResponse:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    #: set instead of ``content`` when the body must be produced lazily (so a
    #: stream can fail halfway through, which a static body cannot)
    stream_factory: Callable[[], Iterator[bytes]] | None = None


def json_response(payload: Any, status: int = 200, **headers: str) -> FakeResponse:
    return FakeResponse(
        status,
        {"Content-Type": "application/json", **headers},
        json.dumps(payload).encode(),
    )


def error_response(status: int, message: str, **headers: str) -> FakeResponse:
    return FakeResponse(
        status,
        {"Content-Type": "application/json", **headers},
        json.dumps({"detail": message}).encode(),
    )


@dataclass
class Fault:
    status: int
    retry_after: float | None = None
    body: str = "injected fault"
    product: str | None = None


@dataclass(frozen=True)
class RecordedCall:
    service: str
    method: str
    url: str
    path: str
    status: int


# -- the platform --------------------------------------------------------------


class FakePlatform:
    """respx/http routes plus a moto bucket for a whole platform."""

    def __init__(
        self,
        products: Sequence[FakeProduct] | None = None,
        collections: Sequence[FakeCollection] | None = None,
        *,
        urls: ServiceUrls | None = None,
        page_size: int = 2,
        key_limit: int | None = None,
    ) -> None:
        self.urls = urls or ServiceUrls()
        self.products: list[FakeProduct] = list(
            products if products is not None else default_products()
        )
        self.collections: list[FakeCollection] = list(
            collections if collections is not None else default_collections()
        )
        self.page_size = page_size

        # -- auth state
        self.username = USERNAME
        self.password = PASSWORD
        self.access_tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.rejected_tokens: set[str] = set()
        self.refresh_token_valid = True
        self.token_ttl = 300.0
        self.refresh_ttl = 1800.0
        self.device_polls_pending = 1
        self.advertise_device_flow = True
        #: services that answer 401 without a valid bearer token
        self.requires_auth: set[str] = {"eodata_http", "keys"}
        #: when set, *every* bearer is rejected, including ones minted later
        self.reject_every_token = False

        # -- keys manager state
        self.key_limit = key_limit
        self.keys: dict[str, dict[str, Any]] = {}

        # -- knobs
        self.faults: dict[str, deque[Fault]] = {}
        #: services whose socket refuses to connect at all (no HTTP status)
        self.unreachable: set[str] = set()
        self.ready: dict[str, bool] = {"eodata_http": True, "s3": True}
        self.truncations: dict[str, int] = {}
        self.discovery_document: dict[str, Any] = self.build_discovery_document()

        # -- observation
        self.calls: list[RecordedCall] = []
        self.unrouted: list[str] = []
        self.sleeps: list[float] = []
        self._counter = 0
        self._lock = threading.Lock()
        self._search_tokens: dict[str, dict[str, Any]] = {}

    # -- helpers ---------------------------------------------------------------

    def _next(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter}"

    def product(self, ref: str) -> FakeProduct | None:
        for product in self.products:
            if ref in (product.uuid, product.name):
                return product
        return None

    def collection(self, collection_id: str) -> FakeCollection | None:
        for collection in self.collections:
            if collection.id == collection_id:
                return collection
        return None

    # -- discovery document ----------------------------------------------------

    def build_discovery_document(self) -> dict[str, Any]:
        return {
            "version": "1.0",
            "platform": {"name": PLATFORM_NAME, "description": PLATFORM_DESCRIPTION},
            "services": {
                "catalogue": {
                    "stac": {"url": self.urls.stac},
                    "odata": {"url": self.urls.odata, "api_version": "v1"},
                },
                "data_access": {
                    "http": {
                        "odata": {
                            "url": self.urls.eodata_http,
                            "api_version": "v1",
                            "capabilities": ["download", "list"],
                        },
                        "resto": {
                            "url": f"{self.urls.eodata_http}/download",
                            "capabilities": ["download"],
                            "deprecated": True,
                            "sunset": "2027-01-01",
                            "replacement": "odata",
                        },
                    },
                    "s3": {
                        "endpoint": self.urls.s3_endpoint,
                        "region": self.urls.s3_region,
                        "credentials": {"url": self.urls.keys, "api_version": "v1"},
                    },
                },
                "auth": {
                    "issuer": f"{self.urls.keycloak}/realms/{REALM}",
                    "client_id": CLIENT_ID,
                },
            },
        }

    # -- knobs -----------------------------------------------------------------

    def fail_next(
        self,
        service: str,
        status: int,
        *,
        times: int = 1,
        retry_after: float | None = None,
        product: str | None = None,
        body: str = "injected fault",
    ) -> None:
        """Make the next ``times`` requests to ``service`` fail with ``status``."""
        queue = self.faults.setdefault(service, deque())
        for _ in range(times):
            queue.append(Fault(status, retry_after, body, product))

    def clear_faults(self, service: str | None = None) -> None:
        if service is None:
            self.faults.clear()
        else:
            self.faults.pop(service, None)

    def expire_access_token(self) -> None:
        """Every currently valid access token starts answering 401."""
        self.rejected_tokens |= self.access_tokens
        self.access_tokens = set()

    def reject_all_tokens(self) -> None:
        """Any bearer token — including ones minted later — answers 401."""
        self.expire_access_token()
        self.requires_auth |= {"eodata_http", "keys"}
        self.reject_every_token = True

    def revoke_refresh_token(self) -> None:
        self.refresh_token_valid = False

    def truncate_next_stream(self, product: str, after: int) -> None:
        """The next ``$value`` body for ``product`` breaks after ``after`` bytes."""
        self.truncations[product] = after

    def take_offline(self, service: str) -> None:
        """Nothing answers on this service's socket: connections are refused.

        Distinct from :meth:`fail_next` — an HTTP status means a server did
        reply, which is what separates ``EndpointUnreachable`` from the rest of
        the taxonomy (SPEC §8).
        """
        self.unreachable.add(service)

    def bring_online(self, service: str) -> None:
        self.unreachable.discard(service)

    def not_ready(self, service: str) -> None:
        self.ready[service] = False

    def set_ready(self, service: str) -> None:
        self.ready[service] = True

    def set_api_version(self, service_key: str, version: str | None) -> None:
        """Set (or delete, with ``None``) an advertised api_version by document path."""
        node: Any = self.discovery_document["services"]
        parts = service_key.split("/")
        for part in parts:
            node = node[part]
        if version is None:
            node.pop("api_version", None)
        else:
            node["api_version"] = version

    def drop_http_strategy(self, name: str) -> None:
        self.discovery_document["services"]["data_access"]["http"].pop(name, None)

    def set_http_capabilities(self, strategy: str, capabilities: list[str]) -> None:
        self.discovery_document["services"]["data_access"]["http"][strategy]["capabilities"] = (
            capabilities
        )

    # -- observation -----------------------------------------------------------

    def call_count(self, service: str, method: str | None = None, *, path: str = "") -> int:
        return len(
            [
                call
                for call in self.calls
                if call.service == service
                and (method is None or call.method == method.upper())
                and (not path or path in call.path)
            ]
        )

    def requests_to(self, service: str) -> list[RecordedCall]:
        return [call for call in self.calls if call.service == service]

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- the router ------------------------------------------------------------

    def _resolve(self, url: str) -> tuple[str, str, dict[str, str]] | None:
        """(service, path, query) for ``url``; None when nothing serves it."""
        split = url.split("?", 1)
        without_query = split[0]
        query = dict(parse_qsl(split[1])) if len(split) == 2 else {}
        for service, base in self.urls.as_pairs():
            trimmed = base.rstrip("/")
            if without_query == trimmed:
                return service, "", query
            if without_query.startswith(trimmed + "/"):
                return service, without_query[len(trimmed) :], query
        return None

    def handle(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> FakeResponse:
        method = method.upper()
        lowered = {key.lower(): value for key, value in (headers or {}).items()}
        resolved = self._resolve(url)
        if resolved is None:
            self.unrouted.append(f"{method} {url}")
            return error_response(404, f"no fake service serves {url}")
        service, path, query = resolved

        response = self._dispatch(service, method, path, query, lowered, body or b"")
        self.calls.append(RecordedCall(service, method, url, path, response.status))
        return response

    def _dispatch(
        self,
        service: str,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        # /ready is public on every service: readiness must be probeable
        # without a session, and it is never a fault-injection target by
        # accident (use not_ready() for that).
        if path == "/ready":
            return self._ready(service)

        fault = self._pop_fault(service, path)
        if fault is not None:
            headers_out = (
                {"Retry-After": _format_retry_after(fault.retry_after)}
                if fault.retry_after is not None
                else {}
            )
            return error_response(fault.status, fault.body, **headers_out)

        denied = self._authorize(service, headers)
        if denied is not None:
            return denied

        handler = {
            "platform": self._handle_platform,
            "keycloak": self._handle_keycloak,
            "stac": self._handle_stac,
            "odata": self._handle_odata,
            "eodata_http": self._handle_eodata,
            "keys": self._handle_keys,
            "s3": self._handle_s3,
        }[service]
        return handler(method, path, query, headers, body)

    def _pop_fault(self, service: str, path: str) -> Fault | None:
        """The first queued fault this request is eligible for.

        Product-scoped faults are matched by scan rather than by head, because
        a concurrent batch has no deterministic arrival order: a fault aimed at
        one product must not be consumed — or blocked — by another's request.
        """
        queue = self.faults.get(service)
        if not queue:
            return None
        with self._lock:
            for index, fault in enumerate(queue):
                if fault.product is None or fault.product in path:
                    del queue[index]
                    return fault
        return None

    def _authorize(self, service: str, headers: Mapping[str, str]) -> FakeResponse | None:
        if service not in self.requires_auth:
            return None
        authorization = headers.get("authorization", "")
        token = authorization[len("Bearer ") :] if authorization.startswith("Bearer ") else ""
        if self.reject_every_token or token not in self.access_tokens:
            return error_response(401, "invalid or missing bearer token")
        return None

    def _ready(self, service: str) -> FakeResponse:
        fault = self._pop_fault(f"{service}_ready", "/ready")
        if fault is not None:
            return error_response(fault.status, fault.body)
        if self.ready.get(service, True):
            return json_response({"status": "ready"})
        return error_response(503, "eodata store unavailable")

    # -- platform / discovery --------------------------------------------------

    def _handle_platform(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        if path == WELL_KNOWN_PATH and method == "GET":
            return json_response(self.discovery_document)
        return error_response(404, f"platform has no {path}")

    # -- keycloak --------------------------------------------------------------

    @property
    def token_endpoint(self) -> str:
        return f"{self.urls.keycloak}/realms/{REALM}/protocol/openid-connect/token"

    def _handle_keycloak(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        prefix = f"/realms/{REALM}/protocol/openid-connect"
        if path == f"/realms/{REALM}/.well-known/openid-configuration" and method == "GET":
            document = {
                "issuer": f"{self.urls.keycloak}/realms/{REALM}",
                "authorization_endpoint": f"{self.urls.keycloak}{prefix}/auth",
                "token_endpoint": f"{self.urls.keycloak}{prefix}/token",
                "userinfo_endpoint": f"{self.urls.keycloak}{prefix}/userinfo",
                "end_session_endpoint": f"{self.urls.keycloak}{prefix}/logout",
                "jwks_uri": f"{self.urls.keycloak}{prefix}/certs",
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                    "password",
                    "urn:ietf:params:oauth:grant-type:device_code",
                ],
            }
            if self.advertise_device_flow:
                document["device_authorization_endpoint"] = (
                    f"{self.urls.keycloak}{prefix}/auth/device"
                )
            return json_response(document)

        if path == f"{prefix}/auth/device" and method == "POST":
            if not self.advertise_device_flow:
                return json_response(
                    {"error": "unauthorized_client", "error_description": "grant disabled"}, 400
                )
            code = self._next("device")
            return json_response(
                {
                    "device_code": code,
                    "user_code": "WXYZ-1234",
                    "verification_uri": f"{self.urls.keycloak}{prefix}/auth/device",
                    "verification_uri_complete": (
                        f"{self.urls.keycloak}{prefix}/auth/device?user_code=WXYZ-1234"
                    ),
                    "interval": 1,
                    "expires_in": 600,
                }
            )

        if path == f"{prefix}/token" and method == "POST":
            return self._token(dict(parse_qsl(body.decode() or "")))

        if path == f"{prefix}/logout" and method == "POST":
            return FakeResponse(204)

        return error_response(404, f"keycloak has no {path}")

    def _token(self, form: dict[str, str]) -> FakeResponse:
        grant = form.get("grant_type", "")
        if grant == "password":
            if form.get("username") != self.username or form.get("password") != self.password:
                return json_response(
                    {"error": "invalid_grant", "error_description": "Invalid user credentials"},
                    401,
                )
            return self._issue_tokens()
        if grant == "refresh_token":
            token = form.get("refresh_token", "")
            if not self.refresh_token_valid or token not in self.refresh_tokens:
                return json_response(
                    {"error": "invalid_grant", "error_description": "Token is not active"}, 400
                )
            return self._issue_tokens()
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            if self.device_polls_pending > 0:
                self.device_polls_pending -= 1
                return json_response(
                    {"error": "authorization_pending", "error_description": "pending"}, 400
                )
            return self._issue_tokens()
        return json_response({"error": "unsupported_grant_type", "error_description": grant}, 400)

    def _issue_tokens(self) -> FakeResponse:
        access = self._next("AT")
        refresh = self._next("RT")
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        return json_response(
            {
                "access_token": access,
                "expires_in": self.token_ttl,
                "refresh_token": refresh,
                "refresh_expires_in": self.refresh_ttl,
                "token_type": "Bearer",
            }
        )

    # -- STAC ------------------------------------------------------------------

    def _handle_stac(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        if path in ("", "/") and method == "GET":
            return json_response(
                {
                    "type": "Catalog",
                    "id": "root",
                    "stac_version": "1.0.0",
                    "description": "Fake STAC API",
                    "conformsTo": [
                        "https://api.stacspec.org/v1.0.0/core",
                        "https://api.stacspec.org/v1.0.0/collections",
                        "https://api.stacspec.org/v1.0.0/item-search",
                        "https://api.stacspec.org/v1.0.0/item-search#query",
                        "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
                    ],
                    "links": [
                        {"rel": "self", "href": self.urls.stac},
                        {"rel": "search", "href": f"{self.urls.stac}/search", "method": "GET"},
                        {"rel": "search", "href": f"{self.urls.stac}/search", "method": "POST"},
                    ],
                }
            )

        if path == "/search":
            if method == "POST":
                request = json.loads(body.decode() or "{}")
            else:
                request = _stac_params_to_body(query)
            if "token" in query:
                stored = self._search_tokens.get(query["token"])
                if stored is None:
                    return error_response(400, "unknown paging token")
                request = stored["request"]
                offset = stored["offset"]
            else:
                offset = 0
            return self._stac_search(request, offset)

        if path == "/collections" and method == "GET":
            return self._stac_collections(int(query.get("page", "1")))

        match = re.fullmatch(r"/collections/([^/]+)", path)
        if match and method == "GET":
            collection = self.collection(unquote(match.group(1)))
            if collection is None:
                return error_response(404, "collection not found")
            return json_response(collection.as_stac())

        match = re.fullmatch(r"/collections/([^/]+)/queryables", path)
        if match and method == "GET":
            collection = self.collection(unquote(match.group(1)))
            if collection is None or not collection.advertises_queryables:
                return error_response(404, "no queryables for this collection")
            return json_response(collection.as_stac_queryables())

        return error_response(404, f"stac has no {path}")

    def _stac_search(self, request: dict[str, Any], offset: int) -> FakeResponse:
        ids = request.get("ids")
        if ids:
            # A permissive server: it resolves both the catalogue item id and
            # the download uuid, because ``StacCatalogue.get()`` is handed
            # ``Product.id`` (the uuid extracted from the Product asset href).
            selected = [p for p in self.products if p.uuid in ids or p.name in ids]
        else:
            selected = self._match_stac(request)
        selected = _sort_products(selected, _stac_sort(request.get("sortby")))

        per_page = min(int(request.get("limit") or self.page_size), self.page_size)
        page = selected[offset : offset + per_page]
        links: list[dict[str, Any]] = []
        if offset + per_page < len(selected):
            token = self._next("page")
            self._search_tokens[token] = {"request": request, "offset": offset + per_page}
            links.append(
                {
                    "rel": "next",
                    "href": f"{self.urls.stac}/search?token={token}",
                    "method": "GET",
                }
            )
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": len(selected),
                "numberReturned": len(page),
                "features": [
                    product.as_stac_item(eodata_http=self.urls.eodata_http, stac=self.urls.stac)
                    for product in page
                ],
                "links": links,
            }
        )

    def _match_stac(self, request: dict[str, Any]) -> list[FakeProduct]:
        collections = request.get("collections") or []
        bbox = tuple(request["bbox"]) if request.get("bbox") else None
        interval = request.get("datetime")
        conditions = request.get("query") or {}

        selected = []
        for product in self.products:
            if collections and product.stac_collection_id not in collections:
                continue
            if bbox is not None and not _bbox_intersects(bbox, product.bbox):  # type: ignore[arg-type]
                continue
            if interval and not _within_interval(product.instant, interval):
                continue
            if not all(
                _attribute_matches(product, canonical(prop), op, value)
                for prop, comparisons in conditions.items()
                for op, value in comparisons.items()
            ):
                continue
            selected.append(product)
        return selected

    def _stac_collections(self, page: int) -> FakeResponse:
        per_page = max(1, self.page_size)
        start = (page - 1) * per_page
        chunk = self.collections[start : start + per_page]
        links: list[dict[str, Any]] = []
        if start + per_page < len(self.collections):
            links.append({"rel": "next", "href": f"{self.urls.stac}/collections?page={page + 1}"})
        return json_response({"collections": [c.as_stac() for c in chunk], "links": links})

    # -- OData -----------------------------------------------------------------

    def _handle_odata(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        if path == "/odata/v1/Products" and method == "GET":
            return self._odata_search(query)

        match = re.fullmatch(r"/odata/v1/Products\(([^)]+)\)", path)
        if match and method == "GET":
            product = self.product(unquote(match.group(1)))
            if product is None:
                return error_response(404, "product not found")
            return json_response(product.as_odata_entry())

        match = re.fullmatch(r"/odata/v1/Attributes\(([^)]+)\)", path)
        if match and method == "GET":
            collection = self.collection(unquote(match.group(1)))
            if collection is None or not collection.advertises_queryables:
                return error_response(404, "no attributes for this collection")
            return json_response(collection.as_odata_attributes())

        return error_response(404, f"odata has no {path}")

    def _odata_search(self, query: dict[str, str]) -> FakeResponse:
        expression = query.get("$filter", "")
        try:
            selected = self._match_odata(expression)
        except ValueError as exc:
            return error_response(400, f"cannot evaluate $filter: {exc}")
        selected = _sort_products(selected, _odata_sort(query.get("$orderby")))

        top = int(query.get("$top") or self.page_size)
        per_page = min(top, self.page_size)
        skip = int(query.get("$skip") or 0)
        page = selected[skip : skip + per_page]

        payload: dict[str, Any] = {
            "@odata.context": "$metadata#Products",
            "value": [product.as_odata_entry() for product in page],
        }
        if query.get("$count", "").lower() == "true":
            payload["@odata.count"] = len(selected)
        if skip + per_page < len(selected):
            following = dict(query)
            following["$skip"] = str(skip + per_page)
            payload["@odata.nextLink"] = (
                f"{self.urls.odata}/odata/v1/Products?{urlencode(following)}"
            )
        return json_response(payload)

    def _match_odata(self, expression: str) -> list[FakeProduct]:
        # A filter-less Products query is legal on the real service and is what
        # `eo doctor` probes with; the SDK refuses unbounded *searches* on the
        # client side, before any request is built.
        if not expression:
            return list(self.products)
        predicates = [_odata_clause(clause) for clause in _split_and(expression)]
        return [p for p in self.products if all(predicate(p) for predicate in predicates)]

    # -- data access over HTTP -------------------------------------------------

    def _handle_eodata(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        match = re.fullmatch(r"/odata/v1/Products\(([^)]+)\)/\$value", path)
        if match and method == "GET":
            return self._value(unquote(match.group(1)))

        match = re.fullmatch(r"/odata/v1/Products\(([^)]+)\)/Nodes(.*)", path)
        if match and method == "GET":
            return self._nodes(unquote(match.group(1)), match.group(2))

        return error_response(404, f"eodata http has no {path}")

    def _value(self, product_id: str) -> FakeResponse:
        product = self.product(product_id)
        if product is None:
            return error_response(404, "product not found")
        payload = product.zip_bytes
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
            "Content-Disposition": f'attachment; filename="{product.zip_name}"',
        }
        cut = self.truncations.pop(product.uuid, None)
        if cut is None:
            cut = self.truncations.pop(product.name, None)
        if cut is not None:
            return FakeResponse(200, headers, stream_factory=_truncated(payload, cut))
        return FakeResponse(200, headers, payload)

    def _nodes(self, product_id: str, remainder: str) -> FakeResponse:
        product = self.product(product_id)
        if product is None:
            return error_response(404, "product not found")
        # remainder is "" for the root listing, else "(a)/Nodes(b)/Nodes" —
        # every name is percent-encoded by HttpDownloader._quote_segment.
        segments = [unquote(name) for name in re.findall(r"\(([^)]*)\)", remainder)]
        logical = "/".join(segments)
        if logical and logical not in product.directories():
            return error_response(404, f"no such directory: {logical}")
        entries = []
        for name, is_dir, size in product.children(logical):
            child = f"{logical}/{name}" if logical else name
            entry: dict[str, Any] = {
                "Id": f"{product.uuid}:{child}",
                "Name": name,
                "ContentLength": size if size is not None else 0,
                "ChildrenNumber": product.child_count(child) if is_dir else 0,
            }
            if is_dir:
                entry["ContentType"] = "application/directory"
            entries.append(entry)
        return json_response({"result": entries})

    # -- keys manager ----------------------------------------------------------

    def _handle_keys(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        if path == "/credentials" and method == "POST":
            if self.key_limit is not None and len(self.keys) >= self.key_limit:
                return FakeResponse(
                    403,
                    {"Content-Type": "application/json"},
                    json.dumps({"detail": "Max number of credentials reached."}).encode(),
                )
            access_id = self._next("AK")
            record = {
                "access_id": access_id,
                "secret": self._next("SK"),
                "user_name": self.username,
                "organization": "example-org",
                "expiration_date": "2027-01-01T00:00:00Z",
            }
            self.keys[access_id] = record
            return json_response(
                {
                    "access_id": record["access_id"],
                    "secret": record["secret"],
                    "expiration_date": record["expiration_date"],
                }
            )

        if path == "/credentials" and method == "GET":
            offset = int(query.get("offset") or 0)
            limit = int(query.get("limit") or 100)
            entries = list(self.keys.values())
            page = entries[offset : offset + limit]
            return json_response(
                {
                    "credentials": [
                        {
                            "access_id": entry["access_id"],
                            "user_name": entry["user_name"],
                            "organization": entry["organization"],
                            "expiration_date": entry["expiration_date"],
                        }
                        for entry in page
                    ],
                    "count": len(entries),
                    "offset": offset,
                    "limit": limit,
                }
            )

        match = re.fullmatch(r"/credentials/access_id/([^/]+)", path)
        if match and method == "DELETE":
            access_id = unquote(match.group(1))
            if access_id not in self.keys:
                return error_response(404, "no such key")
            del self.keys[access_id]
            return FakeResponse(204)

        return error_response(404, f"keys manager has no {path}")

    # -- S3 (only /ready travels over httpx; objects go through moto) ----------

    def _handle_s3(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> FakeResponse:
        return error_response(404, f"s3 objects are served by moto, not the router ({path})")


# -- filter evaluation ---------------------------------------------------------


def _bbox_intersects(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> bool:
    return not (
        left[2] < right[0] or right[2] < left[0] or left[3] < right[1] or right[3] < left[1]
    )


def _parse_instant(value: str) -> dt.datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _within_interval(instant: dt.datetime, interval: str) -> bool:
    start, _, end = interval.partition("/")
    if start and start != ".." and instant < _parse_instant(start):
        return False
    return not (end and end != ".." and instant > _parse_instant(end))


_COMPARISONS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "neq": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "ge": lambda a, b: a >= b,
}


def _attribute_matches(product: FakeProduct, name: str, op: str, value: Any) -> bool:
    actual = product.attributes.get(name)
    if actual is None:
        return False
    compare = _COMPARISONS.get(op)
    if compare is None:
        raise ValueError(f"unsupported comparison {op!r}")
    if isinstance(actual, (int, float)) and isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return False
    return bool(compare(actual, value))


def _split_and(expression: str) -> list[str]:
    """Split an OData ``$filter`` on top-level ``and`` (quotes/parens aware)."""
    clauses: list[str] = []
    depth = 0
    quoted = False
    current: list[str] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "'":
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and expression.startswith(" and ", index) and not quoted:
                clauses.append("".join(current).strip())
                current = []
                index += len(" and ")
                continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        clauses.append(tail)
    return clauses


_COLLECTION_CLAUSE = re.compile(r"^Collection/Name eq '(?P<name>(?:[^']|'')*)'$")
_DATE_CLAUSE = re.compile(r"^ContentDate/Start (?P<op>eq|ne|lt|le|gt|ge) (?P<value>\S+)$")
_INTERSECTS_CLAUSE = re.compile(
    r"^OData\.CSC\.Intersects\(area=geography'SRID=4326;POLYGON\(\((?P<ring>.*)\)\)'\)$"
)
# Not emitted by the SDK's translator, but reachable through the `query_raw`
# escape hatch — examples/python/07_raw_queries.py hand-writes both.
_NAME_CONTAINS_CLAUSE = re.compile(r"^contains\(Name,'(?P<needle>(?:[^']|'')*)'\)$")
_LENGTH_CLAUSE = re.compile(r"^ContentLength (?P<op>eq|ne|lt|le|gt|ge) (?P<value>\d+)$")
_ATTRIBUTE_CLAUSE = re.compile(
    r"^Attributes/OData\.CSC\.(?P<type>Double|String|Integer|Boolean)Attribute/any\("
    r"att:att/Name eq '(?P<name>(?:[^']|'')*)' and "
    r"att/OData\.CSC\.(?P=type)Attribute/Value (?P<op>eq|ne|lt|le|gt|ge) (?P<value>.+)\)$"
)


def _odata_clause(clause: str) -> Callable[[FakeProduct], bool]:
    """Compile one generated ``$filter`` clause into a predicate.

    Anything the SDK's translator can emit must be recognized here: an
    unparsed clause is answered with HTTP 400, so a change in translation
    surfaces as a failing journey rather than as a silently empty result.
    """
    match = _COLLECTION_CLAUSE.match(clause)
    if match:
        wanted = match.group("name").replace("''", "'")
        return lambda product: product.collection == wanted

    match = _DATE_CLAUSE.match(clause)
    if match:
        op = match.group("op")
        bound = _parse_instant(match.group("value"))
        compare = _COMPARISONS[op]
        return lambda product: bool(compare(product.instant, bound))

    match = _NAME_CONTAINS_CLAUSE.match(clause)
    if match:
        needle = match.group("needle").replace("''", "'")
        return lambda product: needle in product.name

    match = _LENGTH_CLAUSE.match(clause)
    if match:
        compare = _COMPARISONS[match.group("op")]
        bound = int(match.group("value"))
        return lambda product: bool(compare(product.size, bound))

    match = _INTERSECTS_CLAUSE.match(clause)
    if match:
        points = [
            tuple(float(part) for part in pair.strip().split(" "))
            for pair in match.group("ring").split(",")
        ]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        area = (min(xs), min(ys), max(xs), max(ys))
        return lambda product: _bbox_intersects(area, product.bbox)  # type: ignore[arg-type]

    match = _ATTRIBUTE_CLAUSE.match(clause)
    if match:
        name = canonical(match.group("name").replace("''", "'"))
        op = match.group("op")
        raw = match.group("value").strip()
        value: Any
        if raw.startswith("'") and raw.endswith("'"):
            value = raw[1:-1].replace("''", "'")
        else:
            value = float(raw)
        return lambda product: _attribute_matches(product, name, op, value)

    raise ValueError(clause)


def _stac_params_to_body(query: Mapping[str, str]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if "collections" in query:
        body["collections"] = query["collections"].split(",")
    if "ids" in query:
        body["ids"] = query["ids"].split(",")
    if "bbox" in query:
        body["bbox"] = [float(part) for part in query["bbox"].split(",")]
    if "datetime" in query:
        body["datetime"] = query["datetime"]
    if "limit" in query:
        body["limit"] = int(query["limit"])
    return body


_SORT_KEYS: dict[str, Callable[[FakeProduct], Any]] = {
    "datetime": lambda product: product.instant,
    "name": lambda product: product.name,
    "size": lambda product: product.size,
    "cloudCover": lambda product: product.cloud_cover or 0.0,
}


def _stac_sort(sortby: Any) -> tuple[str, bool] | None:
    if not sortby:
        return None
    entry = sortby[0]
    return canonical(entry["field"]), entry.get("direction", "asc") == "desc"


def _odata_sort(orderby: str | None) -> tuple[str, bool] | None:
    if not orderby:
        return None
    field_name, _, direction = orderby.partition(" ")
    mapping = {"ContentDate/Start": "datetime", "Name": "name", "ContentLength": "size"}
    return mapping.get(field_name, field_name), direction.strip() == "desc"


def _sort_products(products: list[FakeProduct], sort: tuple[str, bool] | None) -> list[FakeProduct]:
    if sort is None:
        return products
    key_name, reverse = sort
    key = _SORT_KEYS.get(key_name)
    if key is None:
        return products
    return sorted(products, key=key, reverse=reverse)


def _format_retry_after(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _truncated(payload: bytes, cut: int) -> Callable[[], Iterator[bytes]]:
    def factory() -> Iterator[bytes]:
        yield payload[:cut]
        raise ConnectionError("connection reset mid-stream")

    return factory


# -- default fixtures ----------------------------------------------------------


def default_products() -> list[FakeProduct]:
    """Five products: one full SAFE tree, three small ones, one single-file."""
    return [
        FakeProduct(
            uuid="11111111-1111-4111-8111-111111111111",
            name=safe_tree.PRODUCT_NAME,
            datetime="2026-06-15T09:50:29Z",
            attributes={"cloudCover": 12.4, "productType": "S2MSI2A"},
            tree=dict(safe_tree.FILES),
            prefix=safe_tree.PREFIX,
        ),
        FakeProduct(
            uuid="22222222-2222-4222-8222-222222222222",
            name="S2A_MSIL2A_20260602T095031_N0511_R079_T34UEE_20260602T105514.SAFE",
            datetime="2026-06-02T09:50:31Z",
            bbox=(22.5, 52.9, 24.0, 53.5),
            attributes={"cloudCover": 5.0, "productType": "S2MSI2A"},
        ),
        FakeProduct(
            uuid="33333333-3333-4333-8333-333333333333",
            name="S2B_MSIL1C_20260608T095029_N0511_R079_T34UEF_20260608T105512.SAFE",
            datetime="2026-06-08T09:50:29Z",
            bbox=(23.0, 53.0, 24.5, 54.0),
            attributes={"cloudCover": 42.0, "productType": "S2MSI1C"},
        ),
        FakeProduct(
            uuid="44444444-4444-4444-8444-444444444444",
            name="S2A_MSIL2A_20260620T095031_N0511_R079_T34UEG_20260620T105514.SAFE",
            datetime="2026-06-20T09:50:31Z",
            bbox=(21.0, 51.0, 22.4, 52.0),
            attributes={"cloudCover": 61.5, "productType": "S2MSI2A"},
        ),
        # A single-object product: the only shape where the S3 backend can
        # verify a product-level checksum (it covers exactly one delivered file).
        FakeProduct(
            uuid="55555555-5555-4555-8555-555555555555",
            name="S1A_IW_GRDH_1SDV_20260610T161522_20260610T161547_012345_016ABC_1234.SAFE",
            collection="SENTINEL-1",
            datetime="2026-06-10T16:15:22Z",
            bbox=(20.0, 50.0, 21.5, 51.5),
            attributes={"cloudCover": 0.0, "productType": "IW_GRDH_1S"},
            tree={"product.dat": b"S1-GRDH-payload" * 512},
            prefix="Sentinel-1/SAR/GRD/2026/06/10/"
            "S1A_IW_GRDH_1SDV_20260610T161522_20260610T161547_012345_016ABC_1234.SAFE",
            # deliberately archived: `$value` wraps the single file, while S3
            # stores it bare. That is the shape where S3Downloader's
            # "one object written == the product checksum covers it" proxy is
            # unsound, and tests/e2e/test_parity_backends.py pins the fallout.
        ),
    ]


def default_collections() -> list[FakeCollection]:
    return [
        FakeCollection(
            id="SENTINEL-2",
            title="Sentinel-2 MSI",
            description="Multispectral imagery from the Sentinel-2 constellation.",
            bbox=[-180.0, -85.0, 180.0, 85.0],
        ),
        FakeCollection(
            id="SENTINEL-1",
            title="Sentinel-1 SAR",
            description="C-band SAR from the Sentinel-1 constellation.",
            queryables={"productType": "string", "platformShortName": "string"},
        ),
        FakeCollection(
            id="SENTINEL-3",
            title="Sentinel-3 OLCI/SLSTR",
            description="A collection that advertises no queryables.",
            advertises_queryables=False,
        ),
    ]


# -- adapters ------------------------------------------------------------------


def install_respx_routes(platform: FakePlatform, router: Any) -> None:
    """Point every request the process makes at :meth:`FakePlatform.handle`."""
    import httpx

    def side_effect(request: httpx.Request) -> httpx.Response:
        if platform.unreachable:
            resolved = platform._resolve(str(request.url))
            if resolved is not None and resolved[0] in platform.unreachable:
                raise httpx.ConnectError("connection refused", request=request)
        body = b"" if request.method in ("GET", "HEAD", "DELETE") else request.content
        response = platform.handle(
            request.method, str(request.url), headers=request.headers, body=body
        )
        if response.stream_factory is not None:
            return httpx.Response(
                response.status,
                headers=response.headers,
                content=response.stream_factory(),
            )
        return httpx.Response(response.status, headers=response.headers, content=response.content)

    router.route().mock(side_effect=side_effect)


def seed_s3(platform: FakePlatform, *, products: Iterable[FakeProduct] | None = None) -> None:
    """Create the bucket(s) and put every product's tree, inside ``mock_aws``."""
    import boto3

    client = boto3.client("s3", region_name=platform.urls.s3_region)
    buckets = {p.bucket for p in platform.products if p.in_s3}
    existing = {entry["Name"] for entry in client.list_buckets().get("Buckets", [])}
    for bucket in buckets:
        if bucket not in existing:
            client.create_bucket(Bucket=bucket)
    for product in products if products is not None else platform.products:
        if not product.in_s3:
            continue
        for logical, content in product.tree.items():
            client.put_object(
                Bucket=product.bucket,
                Key=f"{product.s3_prefix}/{logical}",
                Body=content,
            )


class S3CallRecorder:
    """Record every S3 API call boto3 makes while the recorder is installed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def operations(self, name: str) -> list[dict[str, Any]]:
        return [params for operation, params in self.calls if operation == name]

    def install(self, monkeypatch: Any) -> S3CallRecorder:
        import boto3

        original = boto3.client

        def patched(*args: Any, **kwargs: Any) -> Any:
            client = original(*args, **kwargs)
            if args and args[0] == "s3":
                client.meta.events.register(
                    "provide-client-params.s3.*", self._record, unique_id=str(id(self))
                )
            return client

        monkeypatch.setattr(boto3, "client", patched)
        return self

    def _record(self, params: dict[str, Any], model: Any, **_: Any) -> None:
        self.calls.append((model.name, dict(params)))


def s3_uri_for(product: FakeProduct) -> str:
    return product.s3_uri


def parse_multihash_md5(value: str) -> str:
    """The digest carried by a STAC ``file:checksum`` md5 multihash."""
    return value[len("d50110") :]


def stac_item_of(platform: FakePlatform, product: FakeProduct) -> dict[str, Any]:
    return product.as_stac_item(eodata_http=platform.urls.eodata_http, stac=platform.urls.stac)


def urls_for_port(port: int, *, host: str = "127.0.0.1") -> ServiceUrls:
    """Service URLs for the out-of-process stub, all behind one socket."""
    base = f"http://{host}:{port}"
    return ServiceUrls(
        platform=base,
        keycloak=f"{base}/auth",
        stac=f"{base}/stac",
        odata=f"{base}/odata",
        eodata_http=f"{base}/eodata",
        keys=f"{base}/keys",
    )


__all__ = [
    "BUCKET",
    "CLIENT_ID",
    "EODATA_HTTP",
    "KEYCLOAK",
    "KEYS",
    "ODATA",
    "PASSWORD",
    "PLATFORM",
    "PLATFORM_NAME",
    "REALM",
    "S3_ENDPOINT",
    "S3_REGION",
    "STAC",
    "USERNAME",
    "WELL_KNOWN_PATH",
    "FakeCollection",
    "FakePlatform",
    "FakeProduct",
    "FakeResponse",
    "RecordedCall",
    "S3CallRecorder",
    "ServiceUrls",
    "default_collections",
    "default_products",
    "install_respx_routes",
    "parse_multihash_md5",
    "seed_s3",
    "stac_item_of",
    "urls_for_port",
]
