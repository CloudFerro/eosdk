import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from pydantic import SecretStr

from eosdk.auth.s3_keys import S3Credentials
from eosdk.eodata._transfer import state_path
from eosdk.eodata.exos import ExosDownloader, RangedS3File
from eosdk.exceptions import DownloadError
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
def downloader(s3: Any) -> ExosDownloader:
    return ExosDownloader(
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
        self, downloader: ExosDownloader, tmp_path: Path
    ) -> None:
        (report,) = downloader.fetch(single_product(), tmp_path)
        file_path = report.path / "product_A.zip"
        assert file_path.read_bytes() == SINGLE_PAYLOAD
        assert report.checksum_verified is True
        assert report.bytes == len(SINGLE_PAYLOAD)
        assert not state_path(file_path).exists()  # sidecar cleaned up

    def test_multi_file_product_tree_recreated(
        self, downloader: ExosDownloader, tmp_path: Path
    ) -> None:
        (report,) = downloader.fetch(safe_product(), tmp_path)
        for logical, content in safe_tree.FILES.items():
            assert (report.path / logical).read_bytes() == content

    def test_checksum_mismatch_raises(self, downloader: ExosDownloader, tmp_path: Path) -> None:
        product = single_product().model_copy(
            update={"checksum": Checksum(algorithm="md5", value="0" * 32)}
        )
        with pytest.raises(DownloadError, match="checksum mismatch"):
            downloader.fetch(product, tmp_path)


class TestResume:
    def test_resume_fetches_only_missing_ranges(
        self, downloader: ExosDownloader, tmp_path: Path, s3: Any
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
        completed_before = {r for r in interrupted_ranges[:1]}
        assert completed_before.isdisjoint(set(second_calls))
        assert (
            len(second_calls)
            < len(plan_all := ["bytes=0-99999", "bytes=100000-199999", "bytes=200000-299999"])
            or second_calls != plan_all
        )

    def test_object_changed_under_resume_restarts_clean(
        self, downloader: ExosDownloader, tmp_path: Path, s3: Any
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


class TestList:
    def test_single_level_root(self, downloader: ExosDownloader) -> None:
        nodes = downloader.list(safe_product())
        assert [n.path for n in nodes] == sorted(safe_tree.ROOT_CHILDREN)
        by_path = {n.path: n for n in nodes}
        assert by_path["GRANULE"].is_dir is True
        assert by_path["manifest.safe"].is_dir is False
        assert by_path["manifest.safe"].size == len(safe_tree.FILES["manifest.safe"])

    def test_single_level_subdirectory(self, downloader: ExosDownloader) -> None:
        nodes = downloader.list(
            safe_product(), path="GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m"
        )
        names = sorted(n.name for n in nodes)
        assert names == ["T34UEE_B04_10m.jp2", "T34UEE_B08_10m.jp2"]
        assert all(not n.is_dir for n in nodes)
        assert all(n.path.startswith("GRANULE/") for n in nodes)  # logical paths

    def test_recursive_full_tree(self, downloader: ExosDownloader) -> None:
        nodes = downloader.list(safe_product(), recursive=True)
        files = {n.path for n in nodes if not n.is_dir}
        directories = {n.path for n in nodes if n.is_dir}
        assert files == set(safe_tree.FILES)
        assert directories == set(safe_tree.DIRECTORIES)

    def test_recursive_equals_bfs_union_of_single_levels(self, downloader: ExosDownloader) -> None:
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


class TestOpen:
    B04 = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"

    def test_sequential_read(self, downloader: ExosDownloader) -> None:
        with downloader.open(safe_product(), self.B04) as fh:
            assert fh.read() == safe_tree.FILES[self.B04]

    def test_seek_and_slice(self, downloader: ExosDownloader) -> None:
        content = safe_tree.FILES[self.B04]
        with downloader.open(safe_product(), self.B04) as fh:
            fh.seek(4)
            assert fh.read(3) == content[4:7]
            fh.seek(-6, os.SEEK_END)
            assert fh.read() == content[-6:]

    def test_read_past_eof(self, downloader: ExosDownloader) -> None:
        with downloader.open(safe_product(), self.B04) as fh:
            fh.seek(0, os.SEEK_END)
            assert fh.read(10) == b""

    def test_readahead_bounds_request_count(self, downloader: ExosDownloader) -> None:
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

    def test_missing_path_raises(self, downloader: ExosDownloader) -> None:
        with pytest.raises(DownloadError, match="no such file"):
            downloader.open(safe_product(), "GRANULE/nope.xml")

    def test_is_raw_io_base(self, downloader: ExosDownloader) -> None:
        import io

        fh = downloader.open(safe_product(), self.B04)
        assert isinstance(fh, io.RawIOBase)
        assert isinstance(fh, RangedS3File)
        buffered = io.BufferedReader(fh)  # duck-types as a file for libraries
        assert buffered.read(4) == safe_tree.FILES[self.B04][:4]
