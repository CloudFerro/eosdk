"""Zipper HTTP download backend (SPEC §6.6).

Zipper serves whole objects only — it supports neither HTTP ``Range`` requests
nor partial reads, so ``supports_resume`` is False: an interrupted transfer is
restarted from the beginning (a logged no-op, never an error).

Strategy scaffolding (SPEC §6.3): the ``odata`` strategy is current, ``resto``
is a recognized fallback name that is not implemented in Phase 1. Discovery-
driven selection replaces the static preference in Phase 3.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

from eosdk.eodata.base import BaseDownloader
from eosdk.exceptions import ProductNotFound, QuotaExceeded
from eosdk.models import Node
from eosdk.transport import RetryPolicy, route


def _quote_segment(segment: str) -> str:
    """Encode a node name for a ``Nodes({name})`` URL segment.

    CDSE's zipper addresses nodes with *unquoted* names inside the parentheses
    (verified against the ``alternate.https`` hrefs the live STAC API emits:
    ``.../Products(<uuid>)/Nodes(S2A_...SAFE)/Nodes(GRANULE)/...``), so names
    are percent-encoded rather than OData-key-quoted; parentheses and other
    reserved characters inside names are escaped. Never assemble these with
    raw f-strings (SPEC §6.6).
    """
    return quote(segment, safe="")


if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

    import httpx

    from eosdk.auth.base import CredentialsProvider
    from eosdk.eodata.base import DownloadReport, ProgressEvent, Stream
    from eosdk.models import Product
    from eosdk.transport import Transport

logger = logging.getLogger(__name__)

STRATEGY_PREFERENCE: tuple[str, ...] = ("odata", "resto")

ROUTES = {
    "odata": {
        "product": "odata/v1/Products({id})/$value",
        "product_zip": "odata/v1/Products({id})/$zip",  # reserved: compressed products (risk R3)
        "node": "odata/v1/Products({id})/Nodes({name})/$value",
        "nodes_root": "odata/v1/Products({id})/Nodes",
    }
}

CAPABILITIES = {
    "odata": frozenset({"download", "list"}),
    "resto": frozenset({"download"}),
}


class _HttpxStream:
    def __init__(self, response: httpx.Response, fallback_name: str) -> None:
        self._response = response
        self._fallback_name = fallback_name

    @property
    def filename(self) -> str:
        disposition: str = self._response.headers.get("Content-Disposition", "")
        for part in disposition.split(";"):
            part = part.strip()
            if part.startswith("filename="):
                name = part[len("filename=") :].strip('"')
                if name:
                    return name
        return self._fallback_name

    @property
    def size(self) -> int | None:
        length = self._response.headers.get("Content-Length")
        return int(length) if length else None

    def iter_bytes(self) -> Iterator[bytes]:
        return self._response.iter_bytes(chunk_size=1 << 16)


class ZipperDownloader(BaseDownloader):
    backend: ClassVar[str] = "zipper"
    supports_resume: ClassVar[bool] = False

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport,
        auth: CredentialsProvider,
        strategy: str | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        super().__init__(retry=retry)
        self._base = base_url
        self._transport = transport
        self._auth = auth
        chosen = strategy or STRATEGY_PREFERENCE[0]
        if chosen not in STRATEGY_PREFERENCE:
            raise ValueError(f"unknown zipper strategy {chosen!r}")
        if chosen != "odata":
            raise NotImplementedError(
                f"zipper strategy {chosen!r} is recognized but not implemented yet"
            )
        self._strategy = chosen

    @contextmanager
    def _open_stream(self, product: Product) -> Iterator[Stream]:
        url = route(self._base, ROUTES[self._strategy]["product"], id=product.id)
        with self._transport.stream(
            "GET", url, service="zipper", auth=self._auth.httpx_auth()
        ) as response:
            if response.status_code == 404:
                raise ProductNotFound(product_id=product.id, backend=self.backend)
            if response.status_code == 429:
                raise QuotaExceeded(service="zipper")
            response.raise_for_status()
            yield _HttpxStream(response, fallback_name=f"{product.name}.zip")

    def fetch(
        self,
        products: Product | Iterable[Product],
        target: Path | str,
        *,
        concurrency: int = 4,
        resume: bool = True,
        checksum: bool = True,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> list[DownloadReport]:
        if resume:
            logger.info("zipper does not support HTTP Range; interrupted transfers restart")
        return super().fetch(
            products,
            target,
            concurrency=concurrency,
            resume=False,
            checksum=checksum,
            progress=progress,
        )

    # -- list (SPEC §6.6 Listable): Nodes hierarchy, one request per directory ---

    def _nodes_url(self, product: Product, path: str) -> str:
        """Address a directory: Products({id})/Nodes(a)/Nodes(b)/.../Nodes.

        Every name segment goes through :func:`odata_key` — node names contain
        spaces and parentheses in real Sentinel products (SPEC §6.6).
        """
        url = route(self._base, ROUTES[self._strategy]["nodes_root"], id=product.id)
        for segment in (s for s in path.split("/") if s):
            url = f"{url}({_quote_segment(segment)})/Nodes"
        return url

    def list(
        self, product: Product, path: str = "", *, recursive: bool = False
    ) -> builtins.list[Node]:
        """Immediate children of ``path`` (root by default); BFS when recursive.

        Recursion costs one request per directory here — Exos walks a prefix in
        a single paginated request. The asymmetry is documented, not hidden.
        """
        nodes = self._list_level(product, path)
        if not recursive:
            return nodes
        collected: dict[str, Node] = {n.path: n for n in nodes}
        queue = [n.path for n in nodes if n.is_dir]
        with ThreadPoolExecutor(max_workers=4) as pool:
            while queue:
                # fetch one BFS depth level concurrently, bounded by the pool
                results = list(pool.map(lambda p: self._list_level(product, p), queue))
                queue = []
                for children in results:
                    for node in children:
                        collected[node.path] = node
                        if node.is_dir:
                            queue.append(node.path)
        return sorted(collected.values(), key=lambda n: n.path)

    def _list_level(self, product: Product, path: str) -> builtins.list[Node]:
        response = self._transport.request(
            "GET",
            self._nodes_url(product, path),
            service="zipper",
            auth=self._auth.httpx_auth(),
        )
        if response.status_code == 404:
            raise ProductNotFound(product_id=product.id, backend=self.backend)
        response.raise_for_status()
        entries = response.json().get("result", response.json().get("value", []))
        nodes = []
        for entry in entries:
            name = str(entry.get("Name", ""))
            logical = f"{path}/{name}".lstrip("/") if path else name
            children = entry.get("ChildrenNumber")
            content_type = str(entry.get("ContentType", ""))
            is_dir = (children or 0) > 0 or content_type in ("application/directory", "dir")
            nodes.append(
                Node(
                    name=name,
                    path=logical,
                    size=entry.get("ContentLength") if not is_dir else None,
                    is_dir=is_dir,
                    raw=dict(entry),
                )
            )
        return nodes
