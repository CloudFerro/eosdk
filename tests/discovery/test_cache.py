import json
import stat
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from eosdk.discovery.cache import DiscoveryCache, fetch_document
from eosdk.discovery.models import DiscoveryError
from eosdk.exceptions import EndpointUnreachable
from eosdk.transport import RetryPolicy, Transport
from tests.discovery.test_models import spec_document

URL = "https://platform.example.eu/.well-known/eo-services.json"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cache(tmp_path: Path, clock: Clock) -> DiscoveryCache:
    return DiscoveryCache(tmp_path / "discovery", ttl=3600.0, now=clock)


@pytest.fixture
def transport() -> Iterator[Transport]:
    with Transport(retry=RetryPolicy(attempts=2, jitter=False), sleep=lambda _: None) as t:
        yield t


class TestCache:
    @respx.mock
    def test_fresh_hit_makes_no_http_calls(
        self, cache: DiscoveryCache, transport: Transport
    ) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json=spec_document()))
        fetch_document(URL, cache, transport)
        assert route.call_count == 1
        fetch_document(URL, cache, transport)
        assert route.call_count == 1  # served from fresh cache

    @respx.mock
    def test_ttl_expiry_refetches(
        self, cache: DiscoveryCache, transport: Transport, clock: Clock
    ) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json=spec_document()))
        fetch_document(URL, cache, transport)
        clock.now += 3601
        fetch_document(URL, cache, transport)
        assert route.call_count == 2

    @respx.mock
    def test_refresh_busts_fresh_cache(self, cache: DiscoveryCache, transport: Transport) -> None:
        route = respx.get(URL).mock(return_value=httpx.Response(200, json=spec_document()))
        fetch_document(URL, cache, transport)
        fetch_document(URL, cache, transport, refresh=True)
        assert route.call_count == 2

    @respx.mock
    def test_offline_with_stale_cache_serves_stale(
        self, cache: DiscoveryCache, transport: Transport, clock: Clock
    ) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, json=spec_document()))
        fetch_document(URL, cache, transport)
        clock.now += 7200  # stale now
        respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
        document = fetch_document(URL, cache, transport)
        assert document.version == "1.0"

    @respx.mock
    def test_offline_without_cache_raises_with_hint(
        self, cache: DiscoveryCache, transport: Transport
    ) -> None:
        respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(EndpointUnreachable, match="EOSDK_DISCOVERY_URL"):
            fetch_document(URL, cache, transport)

    @respx.mock
    def test_bad_schema_is_not_cached_and_raises(
        self, cache: DiscoveryCache, transport: Transport
    ) -> None:
        bad = spec_document()
        bad["version"] = "9.0"
        respx.get(URL).mock(return_value=httpx.Response(200, json=bad))
        with pytest.raises(DiscoveryError):
            fetch_document(URL, cache, transport)
        assert cache.get(URL) is None

    @respx.mock
    def test_cache_file_permissions(
        self, cache: DiscoveryCache, transport: Transport, tmp_path: Path
    ) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, json=spec_document()))
        fetch_document(URL, cache, transport)
        (cache_file,) = (tmp_path / "discovery").glob("*.json")
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
        payload = json.loads(cache_file.read_text())
        assert "fetched_at" in payload  # timestamp in-file, not mtime

    def test_corrupt_cache_treated_as_missing(self, cache: DiscoveryCache, tmp_path: Path) -> None:
        cache.put(URL, spec_document())
        (cache_file,) = (tmp_path / "discovery").glob("*.json")
        cache_file.write_text("{corrupt")
        assert cache.get(URL) is None

    def test_invalidate(self, cache: DiscoveryCache) -> None:
        cache.put(URL, spec_document())
        cache.invalidate(URL)
        assert cache.get(URL) is None
