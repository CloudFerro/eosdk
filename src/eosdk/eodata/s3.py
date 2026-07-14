"""S3 download backend (SPEC §6.6): download + list + open.

- ``fetch``: ranged multipart GETs with resume (sidecar state, ETag-validated;
  a changed object always restarts — ranges are never spliced across versions).
- ``list``: single-level via ``ListObjectsV2`` with ``Delimiter="/"``;
  ``recursive=True`` is a native prefix walk. S3 has no real directories:
  ``is_dir`` is derived from ``CommonPrefixes`` and zero-byte folder markers.
- ``open``: ranged reads of one file, ``IfMatch``-pinned to the ETag captured
  at open time so a mid-read object change fails loudly.

Credentials come from :class:`~eosdk.auth.s3_keys.S3KeysProvider`; this module
never sees Keycloak tokens.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from eosdk.eodata._transfer import (
    TransferState,
    clear_state,
    load_state,
    plan_ranges,
    save_state,
)
from eosdk.eodata.base import DownloadReport, EventKind, ProgressEvent
from eosdk.exceptions import DownloadError, ProductNotFound
from eosdk.models import Node

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from eosdk.auth.s3_keys import S3Credentials, S3KeysProvider
    from eosdk.models import Product

DEFAULT_PART_SIZE = 16 * 2**20  # 16 MiB
KEY_LABEL = "eosdk"


class S3Downloader:
    backend: ClassVar[str] = "s3"
    supports_resume: ClassVar[bool] = True

    def __init__(
        self,
        endpoint: str,
        *,
        region: str = "default",
        credentials: S3KeysProvider | S3Credentials,
        part_size: int = DEFAULT_PART_SIZE,
        max_ranges_per_file: int = 4,
        path_style: bool = True,
        verify: bool = True,
    ) -> None:
        self._endpoint = endpoint
        self._region = region
        self._credentials = credentials
        self._part_size = part_size
        self._max_ranges_per_file = max_ranges_per_file
        self._path_style = path_style
        self._verify = verify
        self._client: Any = None
        self._client_lock = threading.Lock()

    # -- client construction (lazy: keys are not minted until first use) -------

    def _s3(self) -> Any:
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config

                credentials = self._credentials
                if hasattr(credentials, "get_or_create"):  # S3KeysProvider
                    credentials = credentials.get_or_create(label=KEY_LABEL)
                self._client = boto3.client(
                    "s3",
                    endpoint_url=self._endpoint,
                    region_name=self._region,
                    aws_access_key_id=credentials.access_key,
                    aws_secret_access_key=credentials.require_secret(),
                    verify=self._verify,
                    config=Config(
                        s3={"addressing_style": "path" if self._path_style else "virtual"},
                        retries={"max_attempts": 5, "mode": "adaptive"},
                    ),
                )
            return self._client

    # -- addressing -------------------------------------------------------------

    @staticmethod
    def _bucket_and_prefix(product: Product) -> tuple[str, str]:
        """Split ``Product.s3_path`` (s3://bucket/key or /bucket/key) into parts."""
        s3_path = product.s3_path
        if not s3_path:
            raise DownloadError(
                "product has no S3 path; was it found via the catalogue?",
                product_id=product.id,
                backend="s3",
            )
        trimmed = s3_path.removeprefix("s3://").lstrip("/")
        bucket, _, prefix = trimmed.partition("/")
        return bucket, prefix.rstrip("/")

    def _head(self, bucket: str, key: str, product_id: str) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        try:
            head: dict[str, Any] = self._s3().head_object(Bucket=bucket, Key=key)
            return head
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise ProductNotFound(product_id=product_id, backend=self.backend) from exc
            raise

    # -- download (SPEC §6.6 resume protocol) ------------------------------------

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
        errors: list[Exception] = []
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
                for future in futures:
                    try:
                        reports.append(future.result())
                    except Exception as exc:  # finish in-flight, then surface first error
                        errors.append(exc)
            except KeyboardInterrupt:
                # Ctrl+C: drop queued products; in-flight workers stop at the
                # next range boundary with resume state already on disk.
                cancel.set()
                for future in futures:
                    future.cancel()
                raise
        if errors:
            raise errors[0]
        return reports

    def _object_keys(self, product: Product) -> list[tuple[str, str, int]]:
        """(bucket, key, size) for every object making up the product."""
        bucket, prefix = self._bucket_and_prefix(product)
        paginator = self._s3().get_paginator("list_objects_v2")
        objects: list[tuple[str, str, int]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for entry in page.get("Contents", []):
                if not entry["Key"].endswith("/"):  # skip folder markers
                    objects.append((bucket, entry["Key"], entry["Size"]))
        if not objects:
            # a single-object product: the prefix may be the key itself
            head = self._head(bucket, prefix, product.id)
            objects.append((bucket, prefix, head["ContentLength"]))
        return objects

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

        objects = self._object_keys(product)
        total = sum(size for _, _, size in objects)
        emit("start", 0, total)
        done = 0
        product_root = target_dir / product.name
        _, product_prefix = self._bucket_and_prefix(product)
        written: list[Path] = []
        for bucket, key, _size in objects:
            if cancel.is_set():  # batch interrupted: sidecar state resumes the rest
                raise DownloadError("interrupted", product_id=product.id, backend=self.backend)
            relative = (
                key[len(product_prefix) :].lstrip("/") if key != product_prefix else Path(key).name
            )
            file_target = product_root / relative
            file_target.parent.mkdir(parents=True, exist_ok=True)
            done = self._download_object(
                bucket,
                key,
                file_target,
                product,
                resume=resume,
                emit=emit,
                done_offset=done,
                cancel=cancel,
            )
            written.append(file_target)

        verified: bool | None = None
        # Product-level checksums cover exactly one delivered object; multi-file
        # products carry per-file checksums in the catalogue (verified via list()).
        if checksum and product.checksum is not None and len(written) == 1:
            digest = _file_digest(written[0], product.checksum.algorithm)
            if digest != product.checksum.value:
                raise DownloadError(
                    f"checksum mismatch ({product.checksum.algorithm})",
                    product_id=product.id,
                    backend=self.backend,
                )
            verified = True

        emit("done", done, total)
        return DownloadReport(
            product_id=product.id,
            path=product_root,
            bytes=done,
            checksum_verified=verified,
            attempts=1,
        )

    def _download_object(
        self,
        bucket: str,
        key: str,
        target: Path,
        product: Product,
        *,
        resume: bool,
        emit: Callable[..., None],
        done_offset: int,
        cancel: threading.Event,
    ) -> int:
        head = self._head(bucket, key, product.id)
        total_size: int = head["ContentLength"]
        etag: str = head.get("ETag", "")
        part_path = target.with_name(target.name + ".part")

        completed: list[tuple[int, int]] = []
        state = load_state(target) if resume else None
        if (
            state is not None
            and part_path.exists()
            and state.matches(total_size=total_size, validator=etag)
        ):
            completed = [tuple(pair) for pair in state.completed]  # type: ignore[misc]
        else:
            state = None

        plan = plan_ranges(total_size, self._part_size, completed, validator=etag)
        if state is None:
            state = TransferState(
                key=key, total_size=total_size, part_size=self._part_size, validator=etag
            )
            part_path.unlink(missing_ok=True)

        # pre-allocate so offset writes land inside the file
        if not part_path.exists() or part_path.stat().st_size != total_size:
            with part_path.open("wb") as fh:
                fh.truncate(total_size)

        done = done_offset + (total_size - plan.bytes_remaining)
        state_lock = threading.Lock()

        def fetch_range(byte_range: tuple[int, int]) -> None:
            nonlocal done
            if cancel.is_set():  # stop at a range boundary; completed ranges resume
                raise DownloadError("interrupted", product_id=product.id, backend=self.backend)
            start, end = byte_range
            response = self._s3().get_object(
                Bucket=bucket,
                Key=key,
                Range=f"bytes={start}-{end}",
                **({"IfMatch": etag} if etag else {}),
            )
            body = response["Body"].read()
            # one handle per range: seek+write is portable where os.pwrite is not (Windows)
            with part_path.open("r+b") as fh:
                fh.seek(start)
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())  # bytes durable before the state file admits them
            with state_lock:
                assert state is not None
                state.mark_complete(start, end)
                save_state(target, state)
                done += len(body)
                emit("chunk", done, None)

        if plan.ranges:
            if self._max_ranges_per_file > 1 and len(plan.ranges) > 1:
                with ThreadPoolExecutor(max_workers=self._max_ranges_per_file) as pool:
                    list(pool.map(fetch_range, plan.ranges))  # consume to raise errors
            else:
                for byte_range in plan.ranges:
                    fetch_range(byte_range)

        os.replace(part_path, target)
        clear_state(target)
        return done

    # -- list (SPEC §6.6 Listable) ------------------------------------------------

    def list(self, product: Product, path: str = "", *, recursive: bool = False) -> list[Node]:
        bucket, prefix = self._bucket_and_prefix(product)
        base = f"{prefix}/{path.strip('/')}".rstrip("/") if path.strip("/") else prefix
        paginator = self._s3().get_paginator("list_objects_v2")
        nodes: dict[str, Node] = {}

        if recursive:
            for page in paginator.paginate(Bucket=bucket, Prefix=base + "/"):
                for entry in page.get("Contents", []):
                    logical = entry["Key"][len(prefix) :].lstrip("/")
                    if not logical or entry["Key"].endswith("/"):
                        continue
                    nodes[logical] = Node(
                        name=logical.rsplit("/", 1)[-1],
                        path=logical,
                        size=entry["Size"],
                        is_dir=False,
                        raw=dict(entry),
                    )
                    # synthesize intermediate directories from key structure
                    parts = logical.split("/")[:-1]
                    for depth in range(1, len(parts) + 1):
                        dir_path = "/".join(parts[:depth])
                        nodes.setdefault(
                            dir_path,
                            Node(name=parts[depth - 1], path=dir_path, is_dir=True),
                        )
        else:
            for page in paginator.paginate(Bucket=bucket, Prefix=base + "/", Delimiter="/"):
                for common in page.get("CommonPrefixes", []):
                    logical = common["Prefix"][len(prefix) :].strip("/")
                    nodes[logical] = Node(
                        name=logical.rsplit("/", 1)[-1], path=logical, is_dir=True
                    )
                for entry in page.get("Contents", []):
                    logical = entry["Key"][len(prefix) :].lstrip("/")
                    if not logical or entry["Key"].endswith("/"):
                        continue  # zero-byte folder marker == the prefix itself
                    # name both file and prefix: directory (from CommonPrefixes) wins
                    nodes.setdefault(
                        logical,
                        Node(
                            name=logical.rsplit("/", 1)[-1],
                            path=logical,
                            size=entry["Size"],
                            is_dir=False,
                            raw=dict(entry),
                        ),
                    )
        return sorted(nodes.values(), key=lambda n: n.path)

    # -- open (SPEC §6.6 RandomAccess) ---------------------------------------------

    def open(self, product: Product, path: str) -> RangedS3File:
        from botocore.exceptions import ClientError

        bucket, prefix = self._bucket_and_prefix(product)
        key = f"{prefix}/{path.strip('/')}"
        try:
            head = self._s3().head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise DownloadError(
                    f"no such file inside product: {path!r}",
                    product_id=product.id,
                    backend=self.backend,
                ) from exc
            raise
        return RangedS3File(
            self._s3(), bucket, key, size=head["ContentLength"], etag=head.get("ETag", "")
        )


class RangedS3File(io.RawIOBase):
    """Seekable read-only file over ranged S3 GETs, ETag-pinned via IfMatch."""

    def __init__(
        self, client: Any, bucket: str, key: str, *, size: int, etag: str, readahead: int = 65536
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._key = key
        self.size = size
        self._etag = etag
        self._readahead = readahead
        self._position = 0
        self._buffer = b""
        self._buffer_start = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._position = offset
        elif whence == io.SEEK_CUR:
            self._position += offset
        elif whence == io.SEEK_END:
            self._position = self.size + offset
        else:  # pragma: no cover
            raise ValueError(f"invalid whence: {whence}")
        self._position = max(0, self._position)
        return self._position

    def _fetch(self, start: int, end: int) -> bytes:
        response = self._client.get_object(
            Bucket=self._bucket,
            Key=self._key,
            Range=f"bytes={start}-{end}",
            **({"IfMatch": self._etag} if self._etag else {}),
        )
        data: bytes = response["Body"].read()
        return data

    def read(self, size: int = -1) -> bytes:
        if self._position >= self.size:
            return b""
        if size is None or size < 0:
            size = self.size - self._position
        end_wanted = min(self._position + size, self.size) - 1

        buffer_end = self._buffer_start + len(self._buffer) - 1
        if not (self._buffer and self._buffer_start <= self._position and end_wanted <= buffer_end):
            fetch_end = min(max(end_wanted, self._position + self._readahead - 1), self.size - 1)
            self._buffer = self._fetch(self._position, fetch_end)
            self._buffer_start = self._position
        offset = self._position - self._buffer_start
        data = self._buffer[offset : offset + size]
        self._position += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def _file_digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm.replace("-", "_"))
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
