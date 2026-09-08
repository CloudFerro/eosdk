"""Failure behaviour on a real call path (plan §4.6, R1-R9).

The unit tests cover retry policy, token state and the range planner one module
at a time. These drive the same code through ``Client``, where the interesting
questions live: does a batch finish when one member fails, does a refresh happen
exactly once, does an interrupted transfer leave the target directory clean, and
does the resumed run ask only for the bytes it is missing.

Nothing here sleeps: ``Transport.sleep`` is a recorder (see ``conftest``), and
token expiry is driven by an injected clock rather than by wall time.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

import pytest

from eosdk.exceptions import (
    AuthError,
    DownloadError,
    ProductNotFound,
    S3KeyLimitReached,
)
from tests.e2e.platform import FakePlatform, FakeProduct, S3CallRecorder

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from eosdk.client import Client
    from tests.e2e.surface import Invoke

pytestmark = pytest.mark.e2e

JUNE = "2026-06-01/2026-06-30"


@pytest.fixture
def authorizations(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str]]:
    """Every (path, bearer token) pair the platform is asked for.

    ``platform.calls`` deliberately records no credentials; R2 needs them, so
    the spy lives here instead of in the harness.
    """
    seen: list[tuple[str, str]] = []
    original = platform.handle

    def spy(method: str, url: str, **kwargs: Any) -> Any:
        headers = kwargs.get("headers") or {}
        authorization = ""
        for key, value in dict(headers).items():
            if key.lower() == "authorization":
                authorization = str(value)
        seen.append((url, authorization.removeprefix("Bearer ")))
        return original(method, url, **kwargs)

    monkeypatch.setattr(platform, "handle", spy)
    return seen


def june_products(client: Client, count: int) -> list[Any]:
    products = list(client.search(collection="SENTINEL-2", datetime=JUNE))
    assert len(products) >= count
    return products[:count]


class TestRetryPolicyOnRealTransfers:
    def test_r1_rate_limited_product_still_completes(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """R1 — one of three products is throttled; the batch still lands."""
        products = june_products(library_client, 3)
        throttled = products[1]
        platform.fail_next("eodata_http", 429, retry_after=2, product=throttled.id)
        platform.sleeps.clear()

        reports = library_client.download(
            products, target=tmp_path / "batch", via="http", concurrency=3
        )
        assert len(reports) == 3
        for report in reports:
            source = platform.product(report.product_id)
            assert report.path.read_bytes() == source.zip_bytes

        # the transport slept at least as long as Retry-After asked
        assert platform.sleeps, "the 429 must have caused a backoff"
        assert max(platform.sleeps) >= 2.0

    def test_r5_truncated_stream_restarts_from_zero(
        self,
        library_client: Client,
        platform: FakePlatform,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """R5 — http has no Range, so a broken stream is a restart, not an error."""
        source = platform.products[0]
        product = library_client.get(source.uuid, protocol="odata")
        platform.truncate_next_stream(source.uuid, after=64)

        with caplog.at_level(logging.INFO, logger="eosdk.eodata.http"):
            reports = library_client.download(product, target=tmp_path / "t", via="http")

        report = reports[0]
        assert report.attempts == 2  # the first attempt died mid-body
        assert report.path.read_bytes() == source.zip_bytes
        assert report.checksum_verified is True
        assert not list((tmp_path / "t").glob("*.part"))
        restart_notes = [
            record for record in caplog.records if "does not support HTTP Range" in record.message
        ]
        assert len(restart_notes) == 1  # logged once per fetch, not per attempt


class TestSessionFailures:
    def test_r2_expiry_between_search_and_download_refreshes_once(
        self,
        make_client: Callable[..., Client],
        platform: FakePlatform,
        authorizations: list[tuple[str, str]],
        tmp_path: Path,
    ) -> None:
        """R2 — one refresh, and everything afterwards carries the new bearer.

        The clock is injected rather than frozen with freezegun: ``KeycloakAuth``
        captures ``now=time.time`` as a keyword default at import time, so a
        module-level patch would never reach it.
        """
        clock = {"now": 1_000_000.0}
        platform.token_ttl = 300.0
        client = make_client()
        client.auth._now = lambda: clock["now"]
        client.auth.login(platform.username, platform.password)

        products = june_products(client, 2)
        first_token = sorted(platform.access_tokens)[-1]
        token_requests = platform.call_count("keycloak", "POST", path="/token")

        # inside the 30 s leeway the token counts as expired (SPEC §6.4)
        clock["now"] += 280.0
        authorizations.clear()
        reports = client.download(products, target=tmp_path / "r2", via="http", concurrency=2)

        assert len(reports) == 2
        refreshes = platform.call_count("keycloak", "POST", path="/token") - token_requests
        assert refreshes == 1
        new_token = sorted(platform.access_tokens)[-1]
        assert new_token != first_token
        bearers = {token for _, token in authorizations if token}
        assert bearers == {new_token}

    def test_r3_persistent_401_leaves_nothing_behind(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """R3 — 401 → forced refresh → 401 → AuthError, and a clean directory."""
        product = library_client.get(platform.products[0].uuid, protocol="odata")
        target = tmp_path / "r3"
        platform.reject_all_tokens()

        with pytest.raises(AuthError) as exc_info:
            library_client.download(product, target=target, via="http")
        error = exc_info.value
        assert error.realm == "eodata"
        assert error.profile == "e2e"
        assert list(target.iterdir()) == []


class TestBatchSemantics:
    def test_r4_mixed_batch_finishes_what_it_can(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """R4 — ``fetch`` finishes in-flight work, then raises the first error.

        ``BaseDownloader.fetch`` collects errors rather than short-circuiting
        (src/eosdk/eodata/base.py), so a doomed member must not cost the others
        their transfer.
        """
        products = june_products(library_client, 4)
        missing, throttled = products[0], products[1]
        platform.fail_next("eodata_http", 404, times=99, product=missing.id)
        platform.fail_next("eodata_http", 429, retry_after=1, product=throttled.id)

        target = tmp_path / "r4"
        with pytest.raises(ProductNotFound) as exc_info:
            library_client.download(products, target=target, via="http", concurrency=4)
        assert exc_info.value.product_id == missing.id

        landed = {path.name for path in target.iterdir()}
        assert landed == {
            platform.product(product.id).zip_name for product in products if product is not missing
        }
        assert not list(target.glob("*.part"))
        # the recovered product really did land, not just the two clean ones
        recovered = target / platform.product(throttled.id).zip_name
        assert recovered.read_bytes() == platform.product(throttled.id).zip_bytes

    def test_r7_checksum_mismatch_leaves_the_directory_clean(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """R7 — a wrong digest deletes the ``.part`` and names the algorithm."""

        class LyingProduct(FakeProduct):
            """Advertises a checksum its own bytes do not satisfy."""

            @property
            def md5(self) -> str:
                return "0" * 32

        source = platform.products[0]
        platform.products = [
            LyingProduct(uuid=source.uuid, name=source.name, tree=dict(source.tree))
        ]

        product = library_client.get(source.uuid, protocol="odata")
        assert product.checksum is not None
        assert product.checksum.algorithm == "md5"

        target = tmp_path / "r7"
        with pytest.raises(DownloadError, match=r"checksum mismatch \(md5\)") as exc_info:
            library_client.download(product, target=target, via="http")
        assert exc_info.value.backend == "http"
        assert list(target.iterdir()) == []  # no .part, no truncated final file


class TestResumeAcrossAProcessBoundary:
    def test_r6_new_client_asks_only_for_the_missing_ranges(
        self,
        library_client: Client,
        make_client: Callable[..., Client],
        platform: FakePlatform,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """R6 — the resume protocol survives losing the client that started it."""
        from eosdk.eodata._transfer import load_state
        from tests.e2e.platform import seed_s3

        # A product that is genuinely one file: `$value` and S3 then deliver the
        # same bytes, so the resumed transfer can be checksum-verified end to
        # end. (The default Sentinel-1 fixture is archived on purpose — see
        # test_parity_backends.py — which would make the digest disagree.)
        source = FakeProduct(
            uuid="66666666-6666-4666-8666-666666666666",
            name="S1A_IW_GRDH_1SDV_20260612T090000_012347_016ABE_9999.SAFE",
            collection="SENTINEL-1",
            datetime="2026-06-12T09:00:00Z",
            tree={"product.dat": b"resume-me!" * 768},
            prefix="Sentinel-1/SAR/GRD/2026/06/12/resume-fixture",
            payload_is_archive=False,
        )
        platform.products.append(source)
        seed_s3(platform, products=[source])

        product = library_client.get(source.uuid, protocol="odata")
        target = tmp_path / "r6"

        downloader = library_client._s3_downloader()
        # small parts, one at a time: the interruption point must be predictable.
        # S3Downloader captures DEFAULT_PART_SIZE as a keyword default, so the
        # instance is where a test can change it.
        downloader._part_size = 4096
        downloader._max_ranges_per_file = 1

        chunks = {"seen": 0}

        def interrupt(event: Any) -> None:
            if event.kind == "chunk":
                chunks["seen"] += 1
                if chunks["seen"] == 1:
                    raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            library_client.download(
                product, target=target, via="s3", concurrency=1, progress=interrupt
            )

        partial = target / source.name / "product.dat"
        state = load_state(partial)
        assert state is not None
        assert state.completed == [(0, 4095)]
        assert partial.with_name(partial.name + ".part").exists()

        recorder = S3CallRecorder().install(monkeypatch)
        resumed = make_client()
        resumed.auth.login(platform.username, platform.password)
        resumed_downloader = resumed._s3_downloader()
        resumed_downloader._part_size = 4096
        resumed_downloader._max_ranges_per_file = 1
        reports = resumed.download(product, target=target, via="s3", concurrency=1)

        assert partial.read_bytes() == source.tree["product.dat"]
        assert reports[0].checksum_verified is True  # a single object can be verified
        ranges = [params["Range"] for params in recorder.operations("GetObject")]
        assert ranges == ["bytes=4096-7679"]  # the first 4 KiB were never re-fetched
        assert load_state(partial) is None  # the sidecar is cleared on completion


class TestServiceHealthAndQuotas:
    def test_r8_readiness_verdict_is_cached_then_forced(
        self, cli: Invoke, platform: FakePlatform
    ) -> None:
        """R8 — doctor must not turn a rate-limited probe into a poll."""
        platform.not_ready("eodata_http")

        first = cli("doctor", "--json")
        assert first.exit_code == 1
        results = {
            result["name"]: result for section in first.json() for result in section["results"]
        }
        assert results["EOData (HTTP)"]["ok"] is False
        assert "not ready (HTTP 503)" in results["EOData (HTTP)"]["detail"]
        probes = platform.call_count("eodata_http", "GET", path="/ready")
        assert probes == 1

        second = cli("doctor", "--json")
        assert platform.call_count("eodata_http", "GET", path="/ready") == probes
        cached = {
            result["name"]: result for section in second.json() for result in section["results"]
        }
        assert "cached" in cached["EOData (HTTP)"]["detail"]

        forced = cli("doctor", "--json", "--force")
        assert forced.exit_code == 1
        assert platform.call_count("eodata_http", "GET", path="/ready") == probes + 1

    def test_r9_key_limit_is_reported_before_any_s3_call(
        self,
        library_client: Client,
        platform: FakePlatform,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """R9 — the cap surfaces as itself, not as an opaque S3 signing error."""
        platform.key_limit = 0
        recorder = S3CallRecorder().install(monkeypatch)
        product = library_client.get(platform.products[0].uuid, protocol="odata")

        with pytest.raises(S3KeyLimitReached) as exc_info:
            library_client.download(product, target=tmp_path / "r9", via="s3")
        message = str(exc_info.value)
        assert "eo keys revoke" in message
        assert "get_or_create" in message
        assert recorder.calls == []  # no S3 request was ever attempted


class TestProgressUnderFailure:
    def test_error_events_are_emitted_for_the_failing_product_only(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """The progress stream must stay usable when part of a batch fails."""
        products = june_products(library_client, 3)
        platform.fail_next("eodata_http", 404, times=99, product=products[0].id)

        events: list[Any] = []
        lock = threading.Lock()

        def record(event: Any) -> None:
            with lock:
                events.append(event)

        with pytest.raises(ProductNotFound):
            library_client.download(
                products, target=tmp_path / "p", via="http", concurrency=3, progress=record
            )

        kinds = {}
        for event in events:
            kinds.setdefault(event.product_id, []).append(event.kind)
        assert kinds[products[0].id][-1] == "error"
        for product in products[1:]:
            assert kinds[product.id][-1] == "done"
