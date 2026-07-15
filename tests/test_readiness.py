"""ReadinessProbe: single-shot /ready probes with an on-disk rate limit."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx

from eosdk.readiness import ReadinessProbe, default_readiness_dir
from eosdk.transport import Transport

BASE = "https://download.example.eu"
READY = f"{BASE}/ready"


class TestDefaultReadinessDir:
    def test_honours_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert default_readiness_dir() == tmp_path / "xdg" / "eosdk" / "readiness"

    def test_falls_back_to_home_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert default_readiness_dir() == Path.home() / ".config" / "eosdk" / "readiness"


@pytest.fixture
def transport() -> Transport:
    return Transport(sleep=lambda _: None)


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class TestReadinessProbe:
    @respx.mock
    def test_ready_and_cached_within_interval(self, tmp_path: Path, transport: Transport) -> None:
        route = respx.get(READY).mock(return_value=httpx.Response(200))
        clock = FakeClock()
        probe = ReadinessProbe(tmp_path, interval=300.0, now=clock)

        first = probe.check(transport, BASE, service="eodata_http")
        assert first.ready and not first.cached

        clock.now += 60
        second = probe.check(transport, BASE, service="eodata_http")
        assert second.ready and second.cached
        assert second.age == pytest.approx(60.0)
        assert route.call_count == 1

    @respx.mock
    def test_probes_again_after_interval(self, tmp_path: Path, transport: Transport) -> None:
        route = respx.get(READY).mock(return_value=httpx.Response(200))
        clock = FakeClock()
        probe = ReadinessProbe(tmp_path, interval=300.0, now=clock)

        probe.check(transport, BASE, service="eodata_http")
        clock.now += 301
        result = probe.check(transport, BASE, service="eodata_http")
        assert not result.cached
        assert route.call_count == 2

    @respx.mock
    def test_not_ready_is_single_shot_and_throttled(
        self, tmp_path: Path, transport: Transport
    ) -> None:
        route = respx.get(READY).mock(return_value=httpx.Response(503))
        clock = FakeClock()
        probe = ReadinessProbe(tmp_path, interval=300.0, now=clock)

        first = probe.check(transport, BASE, service="eodata_http")
        assert not first.ready
        assert "503" in first.detail
        assert route.call_count == 1  # no transport retry loop on 503

        clock.now += 10
        second = probe.check(transport, BASE, service="eodata_http")
        assert not second.ready and second.cached
        assert route.call_count == 1  # the failure verdict is throttled too

    @respx.mock
    def test_connect_failure_not_recorded(self, tmp_path: Path, transport: Transport) -> None:
        route = respx.get(READY).mock(side_effect=httpx.ConnectError("refused"))
        clock = FakeClock()
        probe = ReadinessProbe(tmp_path, interval=300.0, now=clock)

        first = probe.check(transport, BASE, service="eodata_http")
        assert not first.ready and not first.cached

        route.mock(return_value=httpx.Response(200))  # service comes back
        second = probe.check(transport, BASE, service="eodata_http")
        assert second.ready and not second.cached  # no stale verdict in the way

    @respx.mock
    def test_endpoints_are_throttled_independently(
        self, tmp_path: Path, transport: Transport
    ) -> None:
        other = "https://eodata.example.eu"
        respx.get(READY).mock(return_value=httpx.Response(200))
        s3_route = respx.get(f"{other}/ready").mock(return_value=httpx.Response(503))
        probe = ReadinessProbe(tmp_path, interval=300.0, now=FakeClock())

        assert probe.check(transport, BASE, service="eodata_http").ready
        assert not probe.check(transport, other, service="s3").ready
        assert s3_route.call_count == 1

    @respx.mock
    def test_force_bypasses_throttle_and_resets_it(
        self, tmp_path: Path, transport: Transport
    ) -> None:
        route = respx.get(READY).mock(return_value=httpx.Response(503))
        clock = FakeClock()
        probe = ReadinessProbe(tmp_path, interval=300.0, now=clock)

        assert not probe.check(transport, BASE, service="eodata_http").ready

        route.mock(return_value=httpx.Response(200))  # service recovers
        clock.now += 10
        forced = probe.check(transport, BASE, service="eodata_http", force=True)
        assert forced.ready and not forced.cached
        assert route.call_count == 2

        clock.now += 10  # the forced verdict restarts the interval for unforced checks
        after = probe.check(transport, BASE, service="eodata_http")
        assert after.ready and after.cached
        assert route.call_count == 2

    @respx.mock
    def test_429_verdict_is_recorded_and_throttled(
        self, tmp_path: Path, transport: Transport
    ) -> None:
        # 429 is the strongest reason to record: the service is telling us to
        # stop polling, so the not-ready verdict must be served from disk.
        route = respx.get(READY).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "60"})
        )
        clock = FakeClock()
        probe = ReadinessProbe(tmp_path, interval=300.0, now=clock)

        first = probe.check(transport, BASE, service="eodata_http")
        assert not first.ready and not first.cached
        assert "quota exceeded" in first.detail
        assert route.call_count == 1  # single-shot: no transport retry loop

        clock.now += 10
        second = probe.check(transport, BASE, service="eodata_http")
        assert not second.ready and second.cached
        assert route.call_count == 1

    @respx.mock
    def test_unwritable_store_still_returns_verdict(
        self,
        tmp_path: Path,
        transport: Transport,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The verdict outranks the throttle bookkeeping: a full disk must not
        # turn a successful probe into an error, and the temp file must go.
        respx.get(READY).mock(return_value=httpx.Response(200))
        probe = ReadinessProbe(tmp_path, interval=300.0, now=FakeClock())

        def fail_replace(src: object, dst: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("eosdk.readiness.os.replace", fail_replace)
        with caplog.at_level(logging.WARNING, logger="eosdk.readiness"):
            result = probe.check(transport, BASE, service="eodata_http")
        assert result.ready and not result.cached
        assert "could not persist readiness verdict" in caplog.text
        assert list(tmp_path.glob("*.tmp")) == []  # failed write leaves no litter

    @respx.mock
    def test_clock_gone_backwards_reprobes(self, tmp_path: Path, transport: Transport) -> None:
        route = respx.get(READY).mock(return_value=httpx.Response(200))
        clock = FakeClock()
        probe = ReadinessProbe(tmp_path, interval=300.0, now=clock)

        probe.check(transport, BASE, service="eodata_http")
        clock.now -= 500
        result = probe.check(transport, BASE, service="eodata_http")
        assert not result.cached
        assert route.call_count == 2
