import hashlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from pydantic import SecretStr

from eosdk.auth.s3_keys import S3Credentials
from eosdk.eodata._transfer import state_path
from eosdk.eodata.base import ProgressEvent
from eosdk.eodata.s3 import RangedS3File, S3Downloader
from eosdk.exceptions import DownloadError, ProductNotFound
from eosdk.models import Checksum, Product
from tests.eodata import safe_tree

CREDENTIALS = S3Credentials(key_id="k", access_key="AK", secret_key=SecretStr("SK"))
SINGLE_KEY = "Sentinel-1/GRD/2026/07/product_A.zip"
SINGLE_PAYLOAD = os.urandom(300_000)  # > 2 parts at part_size=100_000


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=safe_tree.BUCKET)
        # SAFE tree product
        for logical, content in safe_tree.FILES.items():
            client.put_object(
                Bucket=safe_tree.BUCKET, Key=f"{safe_tree.PREFIX}/{logical}", Body=content
            )
        # single-object product
        client.put_object(Bucket=safe_tree.BUCKET, Key=SINGLE_KEY, Body=SINGLE_PAYLOAD)
        yield client


@pytest.fixture
def downloader(s3: Any) -> S3Downloader:
    return S3Downloader(
        # moto only intercepts AWS endpoints, not arbitrary custom hosts
        "https://s3.us-east-1.amazonaws.com",
        region="us-east-1",
        credentials=CREDENTIALS,
        part_size=100_000,
        max_ranges_per_file=2,
    )


def single_product(*, with_checksum: bool = True) -> Product:
    checksum = (
        Checksum(algorithm="md5", value=hashlib.md5(SINGLE_PAYLOAD).hexdigest())
        if with_checksum
        else None
    )
    return Product(
        id="uuid-single",
        name="product_A",
        checksum=checksum,
        s3_path=f"/{safe_tree.BUCKET}/{SINGLE_KEY}",
    )


def safe_product() -> Product:
    return Product(id="uuid-safe", name=safe_tree.PRODUCT_NAME, s3_path=safe_tree.S3_PATH)


class TestDownload:
    def test_single_object_ranged_download_and_checksum(
        self, downloader: S3Downloader, tmp_path: Path
    ) -> None:
        (report,) = downloader.fetch(single_product(), tmp_path)
        file_path = report.path / "product_A.zip"
        assert file_path.read_bytes() == SINGLE_PAYLOAD
        assert report.checksum_verified is True
        assert report.bytes == len(SINGLE_PAYLOAD)
        assert not state_path(file_path).exists()  # sidecar cleaned up

    def test_multi_file_product_tree_recreated(
        self, downloader: S3Downloader, tmp_path: Path
    ) -> None:
        (report,) = downloader.fetch(safe_product(), tmp_path)
        for logical, content in safe_tree.FILES.items():
            assert (report.path / logical).read_bytes() == content

    def test_checksum_mismatch_raises(self, downloader: S3Downloader, tmp_path: Path) -> None:
        product = single_product().model_copy(
            update={"checksum": Checksum(algorithm="md5", value="0" * 32)}
        )
        with pytest.raises(DownloadError, match="checksum mismatch"):
            downloader.fetch(product, tmp_path)


class TestAddressingAndErrors:
    def test_product_without_s3_path_rejected(
        self, downloader: S3Downloader, tmp_path: Path
    ) -> None:
        product = Product(id="uuid-nopath", name="NOPATH")  # e.g. built by hand, not from search
        with pytest.raises(DownloadError, match="no S3 path"):
            downloader.fetch(product, tmp_path)

    def test_missing_object_maps_to_product_not_found(
        self, downloader: S3Downloader, tmp_path: Path
    ) -> None:
        product = Product(
            id="uuid-miss",
            name="missing",
            s3_path=f"/{safe_tree.BUCKET}/Sentinel-1/GRD/2026/07/nope.zip",
        )
        with pytest.raises(ProductNotFound, match="uuid-miss"):
            downloader.fetch(product, tmp_path)

    def test_non_404_head_error_propagates(self, downloader: S3Downloader, tmp_path: Path) -> None:
        """Only 404-shaped errors become ProductNotFound; 403 etc. surface raw."""
        from botocore.exceptions import ClientError

        def denied(**kwargs: Any) -> Any:
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

        downloader._s3()
        downloader._client.head_object = denied  # type: ignore[union-attr]
        product = Product(
            id="uuid-denied",
            name="denied",
            s3_path=f"/{safe_tree.BUCKET}/Sentinel-1/GRD/2026/07/nope.zip",
        )
        with pytest.raises(ClientError, match="403"):
            downloader.fetch(product, tmp_path)

    def test_list_denied_object_falls_back_to_head(
        self, downloader: S3Downloader, tmp_path: Path
    ) -> None:
        """An object invisible to ListObjectsV2 is still fetched via HeadObject."""

        class _NoListPaginator:
            def paginate(self, **kwargs: Any) -> Any:
                return iter([{}])  # no Contents at all

        downloader._s3()
        downloader._client.get_paginator = lambda name: _NoListPaginator()  # type: ignore[union-attr]
        (report,) = downloader.fetch(single_product(), tmp_path)
        assert (report.path / "product_A.zip").read_bytes() == SINGLE_PAYLOAD
        assert report.checksum_verified is True
        assert report.bytes == len(SINGLE_PAYLOAD)


class TestProgress:
    def test_event_sequence_monotonic(self, downloader: S3Downloader, tmp_path: Path) -> None:
        events: list[ProgressEvent] = []
        downloader.fetch(single_product(), tmp_path, progress=events.append)
        kinds = [e.kind for e in events]
        assert kinds[0] == "start"
        assert kinds[-1] == "done"
        assert set(kinds[1:-1]) == {"chunk"}
        assert events[0].bytes_total == len(SINGLE_PAYLOAD)
        chunk_bytes = [e.bytes_done for e in events if e.kind == "chunk"]
        assert chunk_bytes == sorted(chunk_bytes)  # done counter never goes backwards
        assert events[-1].bytes_done == len(SINGLE_PAYLOAD)
        assert all(e.product_id == "uuid-single" for e in events)


class TestResume:
    def test_resume_fetches_only_missing_ranges(
        self, downloader: S3Downloader, tmp_path: Path, s3: Any
    ) -> None:
        """Interrupt after the first range; resume must not refetch it."""
        product = single_product()
        calls: list[str] = []
        original = downloader._s3().get_object

        def counting_get_object(**kwargs: Any) -> Any:
            calls.append(kwargs.get("Range", ""))
            if len(calls) == 2:  # die during the second ranged GET
                raise ConnectionError("interrupted")
            return original(**kwargs)

        downloader._client.get_object = counting_get_object  # type: ignore[union-attr]
        with pytest.raises(ConnectionError):
            downloader.fetch(product, tmp_path, concurrency=1)
        interrupted_ranges = list(calls)

        downloader._client.get_object = original  # type: ignore[union-attr]
        second_calls: list[str] = []

        def recording_get_object(**kwargs: Any) -> Any:
            second_calls.append(kwargs.get("Range", ""))
            return original(**kwargs)

        downloader._client.get_object = recording_get_object  # type: ignore[union-attr]
        (report,) = downloader.fetch(product, tmp_path, resume=True)

        file_path = report.path / "product_A.zip"
        assert file_path.read_bytes() == SINGLE_PAYLOAD
        assert report.checksum_verified is True
        # ranges completed before the interrupt are not refetched
        completed_before = set(interrupted_ranges[:1])
        assert completed_before.isdisjoint(set(second_calls))
        assert (
            len(second_calls)
            < len(plan_all := ["bytes=0-99999", "bytes=100000-199999", "bytes=200000-299999"])
            or second_calls != plan_all
        )

    def test_object_changed_under_resume_restarts_clean(
        self, downloader: S3Downloader, tmp_path: Path, s3: Any
    ) -> None:
        product = single_product(with_checksum=False)
        # download once to learn the layout, then fake a stale sidecar + part
        (report,) = downloader.fetch(product, tmp_path)
        file_path = report.path / "product_A.zip"

        new_payload = os.urandom(300_000)
        s3.put_object(Bucket=safe_tree.BUCKET, Key=SINGLE_KEY, Body=new_payload)

        # stale part + sidecar claiming everything but with the OLD etag
        part = file_path.with_name(file_path.name + ".part")
        part.write_bytes(b"x" * 300_000)
        from eosdk.eodata._transfer import TransferState, save_state

        save_state(
            file_path,
            TransferState(
                key=SINGLE_KEY,
                total_size=300_000,
                part_size=100_000,
                validator='"stale-etag"',
                completed=[(0, 299_999)],
            ),
        )

        (report2,) = downloader.fetch(product, tmp_path, resume=True)
        assert (report2.path / "product_A.zip").read_bytes() == new_payload  # full restart


class TestInterrupt:
    def test_cancel_event_stops_at_range_boundary_and_keeps_resume_state(
        self, downloader: S3Downloader, tmp_path: Path, s3: Any
    ) -> None:
        """A cancelled transfer keeps its part file + sidecar; a re-run resumes."""
        cancel = threading.Event()
        product = single_product()
        original = downloader._s3().get_object

        def cancelling_get_object(**kwargs: Any) -> Any:
            result = original(**kwargs)
            cancel.set()  # interrupt after the first completed range
            return result

        downloader._client.get_object = cancelling_get_object  # type: ignore[union-attr]
        with pytest.raises(DownloadError, match="interrupted"):
            downloader._fetch_one(
                product, tmp_path, resume=True, checksum=True, progress=None, cancel=cancel
            )

        file_path = tmp_path / "product_A" / "product_A.zip"
        part = file_path.with_name(file_path.name + ".part")
        assert part.exists()  # partial bytes kept for resume
        assert state_path(file_path).exists()  # sidecar records completed ranges

        downloader._client.get_object = original  # type: ignore[union-attr]
        (report,) = downloader.fetch(product, tmp_path, resume=True)
        assert file_path.read_bytes() == SINGLE_PAYLOAD
        assert report.checksum_verified is True
        assert not state_path(file_path).exists()

    def test_cancel_before_first_object_writes_nothing(
        self, downloader: S3Downloader, tmp_path: Path
    ) -> None:
        """A product cancelled while queued aborts before touching the disk."""
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(DownloadError, match="interrupted"):
            downloader._fetch_one(
                safe_product(), tmp_path, resume=True, checksum=True, progress=None, cancel=cancel
            )
        assert not (tmp_path / safe_tree.PRODUCT_NAME).exists()

    def test_keyboard_interrupt_cancels_queued_products(
        self, downloader: S3Downloader, tmp_path: Path, s3: Any
    ) -> None:
        """Ctrl+C during a ranged GET re-raises and drops the queued product."""
        product_a = single_product(with_checksum=False)
        product_b = product_a.model_copy(update={"id": "uuid-b", "name": "product_B"})
        original = downloader._s3().get_object
        calls: list[int] = []
        gate = threading.Lock()

        def interrupting_get_object(**kwargs: Any) -> Any:
            with gate:
                calls.append(1)
                first = len(calls) == 1
            if first:
                raise KeyboardInterrupt  # stands in for Ctrl+C reaching a worker
            return original(**kwargs)

        downloader._client.get_object = interrupting_get_object  # type: ignore[union-attr]
        with pytest.raises(KeyboardInterrupt):
            downloader.fetch([product_a, product_b], tmp_path, concurrency=1)
        assert not (tmp_path / "product_B").exists()  # queued product never started
        assert not (tmp_path / "product_A" / "product_A.zip").exists()  # no final file


class TestList:
    def test_single_level_root(self, downloader: S3Downloader) -> None:
        nodes = downloader.list(safe_product())
        assert [n.path for n in nodes] == sorted(safe_tree.ROOT_CHILDREN)
        by_path = {n.path: n for n in nodes}
        assert by_path["GRANULE"].is_dir is True
        assert by_path["manifest.safe"].is_dir is False
        assert by_path["manifest.safe"].size == len(safe_tree.FILES["manifest.safe"])

    def test_single_level_subdirectory(self, downloader: S3Downloader) -> None:
        nodes = downloader.list(
            safe_product(), path="GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m"
        )
        names = sorted(n.name for n in nodes)
        assert names == ["T34UEE_B04_10m.jp2", "T34UEE_B08_10m.jp2"]
        assert all(not n.is_dir for n in nodes)
        assert all(n.path.startswith("GRANULE/") for n in nodes)  # logical paths

    def test_recursive_full_tree(self, downloader: S3Downloader) -> None:
        nodes = downloader.list(safe_product(), recursive=True)
        files = {n.path for n in nodes if not n.is_dir}
        directories = {n.path for n in nodes if n.is_dir}
        assert files == set(safe_tree.FILES)
        assert directories == set(safe_tree.DIRECTORIES)

    def test_recursive_equals_bfs_union_of_single_levels(self, downloader: S3Downloader) -> None:
        product = safe_product()
        collected: set[str] = set()
        queue = [""]
        while queue:
            current = queue.pop()
            for node in downloader.list(product, path=current):
                if node.is_dir:
                    queue.append(node.path)
                else:
                    collected.add(node.path)
        recursive_files = {n.path for n in downloader.list(product, recursive=True) if not n.is_dir}
        assert collected == recursive_files

    MARKER_PREFIX = "Sentinel-3/OLCI/2026/07/PRODUCT_M.SEN3"

    def marker_product(self, s3: Any) -> Product:
        """A product whose prefix carries zero-byte folder-marker objects."""
        s3.put_object(Bucket=safe_tree.BUCKET, Key=f"{self.MARKER_PREFIX}/", Body=b"")
        s3.put_object(Bucket=safe_tree.BUCKET, Key=f"{self.MARKER_PREFIX}/EMPTY_DIR/", Body=b"")
        s3.put_object(Bucket=safe_tree.BUCKET, Key=f"{self.MARKER_PREFIX}/data.nc", Body=b"netcdf")
        return Product(
            id="uuid-marker",
            name="PRODUCT_M.SEN3",
            s3_path=f"/{safe_tree.BUCKET}/{self.MARKER_PREFIX}",
        )

    def test_folder_markers_never_listed_as_files(self, downloader: S3Downloader, s3: Any) -> None:
        product = self.marker_product(s3)
        by_path = {n.path: n for n in downloader.list(product)}
        assert set(by_path) == {"EMPTY_DIR", "data.nc"}  # the root marker itself is hidden
        assert by_path["EMPTY_DIR"].is_dir is True  # zero-byte marker derives a directory
        assert by_path["data.nc"].is_dir is False

    def test_folder_markers_skipped_in_recursive_walk(
        self, downloader: S3Downloader, s3: Any
    ) -> None:
        product = self.marker_product(s3)
        nodes = downloader.list(product, recursive=True)
        assert {n.path for n in nodes if not n.is_dir} == {"data.nc"}
        assert all(not n.path.endswith("/") for n in nodes)  # no marker leaks into paths


class TestOpen:
    B04 = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"

    def test_sequential_read(self, downloader: S3Downloader) -> None:
        with downloader.open(safe_product(), self.B04) as fh:
            assert fh.read() == safe_tree.FILES[self.B04]

    def test_seek_and_slice(self, downloader: S3Downloader) -> None:
        content = safe_tree.FILES[self.B04]
        with downloader.open(safe_product(), self.B04) as fh:
            fh.seek(4)
            assert fh.read(3) == content[4:7]
            fh.seek(-6, os.SEEK_END)
            assert fh.read() == content[-6:]

    def test_read_past_eof(self, downloader: S3Downloader) -> None:
        with downloader.open(safe_product(), self.B04) as fh:
            fh.seek(0, os.SEEK_END)
            assert fh.read(10) == b""

    def test_readahead_bounds_request_count(self, downloader: S3Downloader) -> None:
        fh = downloader.open(safe_product(), self.B04)
        fetches: list[tuple[int, int]] = []
        original_fetch = fh._fetch

        def counting_fetch(start: int, end: int) -> bytes:
            fetches.append((start, end))
            return original_fetch(start, end)

        fh._fetch = counting_fetch  # type: ignore[method-assign]
        for _ in range(100):  # tiny sequential reads
            fh.read(16)
        assert len(fetches) == 1  # served from one readahead buffer

    def test_missing_path_raises(self, downloader: S3Downloader) -> None:
        with pytest.raises(DownloadError, match="no such file"):
            downloader.open(safe_product(), "GRANULE/nope.xml")

    def test_non_404_error_on_open_propagates(self, downloader: S3Downloader) -> None:
        from botocore.exceptions import ClientError

        def denied(**kwargs: Any) -> Any:
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

        downloader._s3()
        downloader._client.head_object = denied  # type: ignore[union-attr]
        with pytest.raises(ClientError, match="403"):
            downloader.open(safe_product(), self.B04)

    def test_seek_cur_and_capability_flags(self, downloader: S3Downloader) -> None:
        content = safe_tree.FILES[self.B04]
        with downloader.open(safe_product(), self.B04) as fh:
            assert fh.readable() is True
            assert fh.seekable() is True
            fh.seek(10)
            fh.seek(-4, os.SEEK_CUR)
            assert fh.tell() == 6
            assert fh.read(2) == content[6:8]
            fh.seek(-100, os.SEEK_CUR)  # relative seek before byte 0 clamps to 0
            assert fh.tell() == 0

    def test_is_raw_io_base(self, downloader: S3Downloader) -> None:
        import io

        fh = downloader.open(safe_product(), self.B04)
        assert isinstance(fh, io.RawIOBase)
        assert isinstance(fh, RangedS3File)
        buffered = io.BufferedReader(fh)  # duck-types as a file for libraries
        assert buffered.read(4) == safe_tree.FILES[self.B04][:4]


class _FlakyGateway:
    """boto3 client double: refuses a key N times before accepting it."""

    def __init__(self, failures: int, code: str = "InvalidAccessKeyId") -> None:
        self.failures = failures
        self.code = code
        self.calls = 0

    def list_buckets(self) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        self.calls += 1
        if self.calls <= self.failures:
            raise ClientError({"Error": {"Code": self.code}}, "ListBuckets")
        return {}


class _StubProvider:
    """S3KeysProvider double: hands out fixed credentials, records labels."""

    def __init__(self, credentials: S3Credentials) -> None:
        self.credentials = credentials
        self.labels: list[str] = []

    def get_or_create(self, label: str) -> S3Credentials:
        self.labels.append(label)
        return self.credentials


class TestKeyActivation:
    """Fresh keys propagate to the S3 gateway asynchronously (normally
    seconds); first use is gated on the gateway accepting the key."""

    def _downloader(self, timeout: float = 60.0) -> S3Downloader:
        return S3Downloader(
            "https://s3.example.eu", credentials=CREDENTIALS, key_activation_timeout=timeout
        )

    def test_retries_with_backoff_until_key_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("eosdk.eodata.s3.time.sleep", sleeps.append)
        gateway = _FlakyGateway(failures=2)
        self._downloader()._await_key_activation(gateway, "AK")
        assert gateway.calls == 3
        assert sleeps == [1.0, 2.0]  # doubling backoff between probes

    def test_timeout_raises_diagnostic_error(self) -> None:
        from eosdk.exceptions import S3KeyNotActive

        gateway = _FlakyGateway(failures=10)
        with pytest.raises(S3KeyNotActive, match="too many keys"):
            self._downloader(timeout=0.0)._await_key_activation(gateway, "AK")
        assert gateway.calls == 1  # deadline already passed after the first refusal

    def test_unrelated_client_error_propagates(self) -> None:
        # AccessDenied and friends are permanent, not propagation lag: re-raise.
        from botocore.exceptions import ClientError

        gateway = _FlakyGateway(failures=1, code="NoSuchBucket")
        with pytest.raises(ClientError, match="NoSuchBucket"):
            self._downloader()._await_key_activation(gateway, "AK")

    def test_fresh_key_gates_first_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _StubProvider(
            S3Credentials(access_key="AK", secret_key=SecretStr("SK"), created=True)
        )
        probed: list[str] = []
        monkeypatch.setattr(
            S3Downloader,
            "_await_key_activation",
            lambda self, client, access_key: probed.append(access_key),
        )
        S3Downloader("https://s3.example.eu", credentials=provider)._s3()
        assert probed == ["AK"]
        assert provider.labels == ["eosdk"]

    def test_reused_key_skips_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _StubProvider(S3Credentials(access_key="AK", secret_key=SecretStr("SK")))
        probed: list[str] = []
        monkeypatch.setattr(
            S3Downloader,
            "_await_key_activation",
            lambda self, client, access_key: probed.append(access_key),
        )
        S3Downloader("https://s3.example.eu", credentials=provider)._s3()
        assert probed == []  # a key served from the store was accepted before
