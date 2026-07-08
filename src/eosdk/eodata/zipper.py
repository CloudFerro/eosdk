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
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar

from eosdk.eodata.base import BaseDownloader
from eosdk.exceptions import ProductNotFound, QuotaExceeded
from eosdk.transport import RetryPolicy, route

if TYPE_CHECKING:
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
