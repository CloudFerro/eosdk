import hashlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from eosdk.eodata.base import ProgressEvent
from eosdk.eodata.http import HttpDownloader
from eosdk.exceptions import DownloadError, ProductNotFound
from eosdk.models import Checksum, Product
from eosdk.transport import RetryPolicy, Transport

BASE = "https://download.example.eu"
PAYLOAD = b"EO product payload " * 512


def product(pid: str = "uuid-1", *, with_checksum: bool = True) -> Product:
    checksum = (
        Checksum(algorithm="md5", value=hashlib.md5(PAYLOAD).hexdigest()) if with_checksum else None
    )
    return Product(id=pid, name=f"PRODUCT_{pid}", checksum=checksum)


def value_url(pid: str) -> str:
    return f"{BASE}/odata/v1/Products({pid})/$value"


class FakeAuth:
    def httpx_auth(self) -> httpx.Auth:
        class _A(httpx.Auth):
            def auth_flow(self, request):  # type: ignore[no-untyped-def]
                request.headers["Authorization"] = "Bearer JWT"
                yield request

        return _A()


@pytest.fixture
def downloader() -> Iterator[HttpDownloader]:
    with Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None) as transport:
        yield HttpDownloader(
            BASE,
            transport=transport,
            auth=FakeAuth(),
            retry=RetryPolicy(attempts=3, jitter=False),
        )


def ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        content=PAYLOAD,
        headers={
            "Content-Length": str(len(PAYLOAD)),
            "Content-Disposition": 'attachment; filename="PRODUCT_uuid-1.zip"',
        },
    )


class TestHappyPath:
    @respx.mock
    def test_downloads_and_verifies(self, downloader: HttpDownloader, tmp_path: Path) -> None:
        mock = respx.get(value_url("uuid-1")).mock(return_value=ok_response())
        reports = downloader.fetch(product(), tmp_path)
        assert len(reports) == 1
        report = reports[0]
        assert report.path == tmp_path / "PRODUCT_uuid-1.zip"
        assert report.path.read_bytes() == PAYLOAD
        assert report.checksum_verified is True
        assert report.attempts == 1
        assert not list(tmp_path.glob("*.part"))
        assert mock.calls.last.request.headers["Authorization"] == "Bearer JWT"

    @respx.mock
    def test_filename_fallback_without_disposition(
        self, downloader: HttpDownloader, tmp_path: Path
    ) -> None:
        respx.get(value_url("uuid-1")).mock(return_value=httpx.Response(200, content=PAYLOAD))
        reports = downloader.fetch(product(with_checksum=False), tmp_path)
        assert reports[0].path.name == "PRODUCT_uuid-1.zip"
        assert reports[0].checksum_verified is None


class TestRetryRestart:
    @respx.mock
    def test_midstream_failure_restarts_without_appending(
        self, downloader: HttpDownloader, tmp_path: Path
    ) -> None:
        def broken_stream(request: httpx.Request) -> httpx.Response:
            def gen() -> Iterator[bytes]:
                yield PAYLOAD[: len(PAYLOAD) // 2]
                raise httpx.ReadError("connection reset")

            return httpx.Response(200, content=gen())

        events: list[ProgressEvent] = []
        respx.get(value_url("uuid-1")).mock(
            side_effect=[broken_stream, ok_response()]  # type: ignore[list-item]
        )
        reports = downloader.fetch(product(), tmp_path, progress=events.append)
        report = reports[0]
        assert report.attempts == 2
        assert report.path.read_bytes() == PAYLOAD  # no duplicated first half
        assert report.checksum_verified is True
        assert any(e.kind == "retry" for e in events)

    @respx.mock
    def test_exhausted_retries_raise_download_error(
        self, downloader: HttpDownloader, tmp_path: Path
    ) -> None:
        respx.get(value_url("uuid-1")).mock(side_effect=httpx.ReadError("dead"))
        with pytest.raises(DownloadError) as exc_info:
            downloader.fetch(product(), tmp_path)
        assert "uuid-1" in str(exc_info.value)
        assert "backend=http" in str(exc_info.value)
        assert not list(tmp_path.iterdir())


class TestChecksum:
    @respx.mock
    def test_corrupted_body_raises_and_cleans_up(
        self, downloader: HttpDownloader, tmp_path: Path
    ) -> None:
        respx.get(value_url("uuid-1")).mock(return_value=httpx.Response(200, content=b"corrupted"))
        with pytest.raises(DownloadError, match="checksum mismatch"):
            downloader.fetch(product(), tmp_path)
        assert not list(tmp_path.iterdir())


class TestProgress:
    @respx.mock
    def test_event_sequence_monotonic(self, downloader: HttpDownloader, tmp_path: Path) -> None:
        respx.get(value_url("uuid-1")).mock(return_value=ok_response())
        events: list[ProgressEvent] = []
        downloader.fetch(product(), tmp_path, progress=events.append)
        kinds = [e.kind for e in events]
        assert kinds[0] == "start"
        assert kinds[-1] == "done"
        assert set(kinds[1:-1]) == {"chunk"}
        chunk_bytes = [e.bytes_done for e in events if e.kind == "chunk"]
        assert chunk_bytes == sorted(chunk_bytes)
        assert events[-1].bytes_done == len(PAYLOAD)
        assert events[0].bytes_total == len(PAYLOAD)


class TestConcurrencyAndErrors:
    @respx.mock
    def test_bounded_concurrency(self, downloader: HttpDownloader, tmp_path: Path) -> None:
        in_flight = 0
        max_in_flight = 0
        gate = threading.Lock()

        def tracked(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, max_in_flight
            with gate:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                return httpx.Response(200, content=PAYLOAD)
            finally:
                with gate:
                    in_flight -= 1

        products = [product(f"uuid-{n}", with_checksum=False) for n in range(6)]
        for p in products:
            respx.get(value_url(p.id)).mock(side_effect=tracked)
        reports = downloader.fetch(products, tmp_path, concurrency=2)
        assert len(reports) == 6
        assert max_in_flight <= 2

    @respx.mock
    def test_one_missing_product_others_complete(
        self, downloader: HttpDownloader, tmp_path: Path
    ) -> None:
        good = product("uuid-good", with_checksum=False)
        bad = product("uuid-bad", with_checksum=False)
        respx.get(value_url(good.id)).mock(return_value=httpx.Response(200, content=PAYLOAD))
        respx.get(value_url(bad.id)).mock(return_value=httpx.Response(404))
        with pytest.raises(ProductNotFound, match="uuid-bad"):
            downloader.fetch([bad, good], tmp_path, concurrency=2)
        assert (tmp_path / "PRODUCT_uuid-good.zip").exists()  # in-flight work finished


class TestInterrupt:
    @respx.mock
    def test_keyboard_interrupt_cancels_queued_and_aborts_in_flight(
        self, downloader: HttpDownloader, tmp_path: Path
    ) -> None:
        """Ctrl+C (KeyboardInterrupt at future.result()) must re-raise, cancel
        queued products, and stop the in-flight transfer within one chunk."""

        def interrupt(request: httpx.Request) -> httpx.Response:
            raise KeyboardInterrupt  # stands in for Ctrl+C reaching the main thread

        def slow_stream(request: httpx.Request) -> httpx.Response:
            def gen() -> Iterator[bytes]:
                for _ in range(100):
                    yield b"x" * 1024
                    time.sleep(0.01)

            return httpx.Response(200, content=gen())

        touched: list[str] = []

        def recording(request: httpx.Request) -> httpx.Response:
            touched.append(str(request.url))
            return httpx.Response(200, content=PAYLOAD)

        respx.get(value_url("uuid-1")).mock(side_effect=interrupt)
        respx.get(value_url("uuid-2")).mock(side_effect=slow_stream)
        for pid in ("uuid-3", "uuid-4"):
            respx.get(value_url(pid)).mock(side_effect=recording)

        products = [product(f"uuid-{n}", with_checksum=False) for n in range(1, 5)]
        with pytest.raises(KeyboardInterrupt):
            downloader.fetch(products, tmp_path, concurrency=1)
        assert touched == []  # queued products never started
        assert not list(tmp_path.iterdir())  # no completed files, no .part litter

    @respx.mock
    def test_cancel_event_aborts_transfer_and_removes_part_file(
        self, downloader: HttpDownloader, tmp_path: Path
    ) -> None:
        cancel = threading.Event()

        def gen() -> Iterator[bytes]:
            yield PAYLOAD[: len(PAYLOAD) // 2]
            cancel.set()
            yield PAYLOAD[len(PAYLOAD) // 2 :]

        respx.get(value_url("uuid-1")).mock(return_value=httpx.Response(200, content=gen()))
        with pytest.raises(DownloadError, match="interrupted"):
            downloader._fetch_one(
                product(with_checksum=False),
                tmp_path,
                resume=False,
                checksum=True,
                progress=None,
                cancel=cancel,
            )
        assert not list(tmp_path.iterdir())


class TestResumeNoOp:
    @respx.mock
    def test_resume_true_logs_and_restarts(
        self,
        downloader: HttpDownloader,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        respx.get(value_url("uuid-1")).mock(return_value=ok_response())
        with caplog.at_level("INFO", logger="eosdk.eodata.http"):
            downloader.fetch(product(), tmp_path, resume=True)
        assert any("does not support" in r.message for r in caplog.records)


def test_unknown_strategy_rejected() -> None:
    with Transport() as transport:
        with pytest.raises(ValueError, match="unknown http strategy"):
            HttpDownloader(BASE, transport=transport, auth=FakeAuth(), strategy="ftp")
        with pytest.raises(NotImplementedError, match="resto"):
            HttpDownloader(BASE, transport=transport, auth=FakeAuth(), strategy="resto")
