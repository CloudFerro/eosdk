"""Downloader protocol and the transfer machinery shared by all backends (SPEC §6.6).

Retries, checksum verification, progress events, and bounded concurrency are
implemented once here; backends provide ``_open_stream`` and declare whether
they support resume (the HTTP backend does not — its transfers restart from zero).

A ``KeyboardInterrupt`` in the caller (Ctrl+C) cancels queued products and
makes in-flight workers abort at the next chunk boundary before re-raising.
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from eosdk.exceptions import DownloadError, EosdkError
from eosdk.transport import RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from contextlib import AbstractContextManager
    from typing import IO

    from eosdk.models import Node, Product


EventKind = Literal["start", "chunk", "retry", "done", "error"]


@dataclass(frozen=True)
class ProgressEvent:
    product_id: str
    kind: EventKind
    bytes_done: int = 0
    bytes_total: int | None = None


@dataclass(frozen=True)
class DownloadReport:
    product_id: str
    path: Path
    bytes: int
    checksum_verified: bool | None  # None: no catalogue checksum to verify against
    attempts: int


class Stream(Protocol):
    """What a backend's ``_open_stream`` yields."""

    @property
    def filename(self) -> str: ...

    @property
    def size(self) -> int | None: ...

    def iter_bytes(self) -> Iterator[bytes]: ...


class Downloader(Protocol):
    def fetch(
        self,
        products: Product | Iterable[Product],
        target: Path | str,
        *,
        concurrency: int = 4,
        resume: bool = True,
        checksum: bool = True,
        progress: Callable[[ProgressEvent], None] | None = None,
    ) -> list[DownloadReport]: ...


class Listable(Protocol):
    """Optional `list` capability: a product's internal file tree (SPEC §6.6)."""

    def list(self, product: Product, path: str = "", *, recursive: bool = False) -> list[Node]: ...


class RandomAccess(Protocol):
    """Optional `open` capability: ranged reads of one file inside a product."""

    def open(self, product: Product, path: str) -> IO[bytes]: ...


class BaseDownloader:
    """Common transfer machinery; subclasses implement ``_open_stream``."""

    backend: ClassVar[str] = "base"
    supports_resume: ClassVar[bool] = False

    def __init__(self, *, retry: RetryPolicy | None = None) -> None:
        self._retry = retry or RetryPolicy()

    # -- backend hook ----------------------------------------------------------

    def _open_stream(self, product: Product) -> AbstractContextManager[Stream]:
        raise NotImplementedError

    # -- shared machinery ------------------------------------------------------

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
        from eosdk.models import Product as ProductModel

        items = [products] if isinstance(products, ProductModel) else list(products)
        target_dir = Path(target)
        target_dir.mkdir(parents=True, exist_ok=True)

        reports: list[DownloadReport] = []
        errors: list[EosdkError] = []
        cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [
                pool.submit(
                    self._fetch_one,
                    product,
                    target_dir,
                    resume=resume,
                    checksum=checksum,
                    progress=progress,
                    cancel=cancel,
                )
                for product in items
            ]
            try:
                for future in futures:  # finish in-flight work even after a failure
                    try:
                        reports.append(future.result())
                    except EosdkError as exc:
                        errors.append(exc)
            except KeyboardInterrupt:
                # Ctrl+C: drop queued products; in-flight workers notice the
                # event within one chunk, so the pool joins promptly on exit.
                cancel.set()
                for future in futures:
                    future.cancel()
                raise
        if errors:
            raise errors[0]
        return reports

    def _fetch_one(
        self,
        product: Product,
        target_dir: Path,
        *,
        resume: bool,
        checksum: bool,
        progress: Callable[[ProgressEvent], None] | None,
        cancel: threading.Event,
    ) -> DownloadReport:
        def emit(kind: EventKind, done: int, total: int | None) -> None:
            if progress is not None:
                progress(
                    ProgressEvent(
                        product_id=product.id, kind=kind, bytes_done=done, bytes_total=total
                    )
                )

        last_error: BaseException | None = None
        for attempt in range(1, self._retry.attempts + 1):
            if cancel.is_set():  # batch interrupted while this product was queued
                raise DownloadError("interrupted", product_id=product.id, backend=self.backend)
            if attempt > 1:
                emit("retry", 0, None)
            try:
                return self._attempt(
                    product, target_dir, attempt=attempt, verify=checksum, emit=emit, cancel=cancel
                )
            except EosdkError:
                emit("error", 0, None)
                raise  # already mapped (ProductNotFound, QuotaExceeded, DownloadError...)
            except Exception as exc:  # transport hiccup mid-stream: retry
                last_error = exc
        emit("error", 0, None)
        raise DownloadError(
            "transfer failed after retries",
            product_id=product.id,
            backend=self.backend,
            cause=last_error,
        )

    def _attempt(
        self,
        product: Product,
        target_dir: Path,
        *,
        attempt: int,
        verify: bool,
        emit: Callable[..., None],
        cancel: threading.Event,
    ) -> DownloadReport:
        with self._open_stream(product) as stream:
            final_path = target_dir / stream.filename
            part_path = final_path.with_suffix(final_path.suffix + ".part")
            total = stream.size
            emit("start", 0, total)

            hasher = None
            expected = product.checksum if verify else None
            if expected is not None:
                hasher = hashlib.new(expected.algorithm.replace("-", "_"))

            done = 0
            interrupted = False
            # No resume support in this base path: truncate on every (re)attempt.
            with part_path.open("wb") as fh:
                for chunk in stream.iter_bytes():
                    if cancel.is_set():  # batch interrupted: abort between chunks
                        interrupted = True
                        break
                    fh.write(chunk)
                    if hasher is not None:
                        hasher.update(chunk)
                    done += len(chunk)
                    emit("chunk", done, total)

        if interrupted:
            # No resume here: an aborted partial file is unusable — drop it.
            part_path.unlink(missing_ok=True)
            raise DownloadError("interrupted", product_id=product.id, backend=self.backend)

        verified: bool | None = None
        if hasher is not None and expected is not None:
            if hasher.hexdigest() != expected.value:
                part_path.unlink(missing_ok=True)
                raise DownloadError(
                    f"checksum mismatch ({expected.algorithm})",
                    product_id=product.id,
                    backend=self.backend,
                )
            verified = True

        part_path.replace(final_path)
        emit("done", done, total)
        return DownloadReport(
            product_id=product.id,
            path=final_path,
            bytes=done,
            checksum_verified=verified,
            attempts=attempt,
        )
