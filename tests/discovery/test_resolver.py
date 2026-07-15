"""DiscoveryResolver bootstrap surface: configured-ness, URL derivation, fallbacks."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from eosdk.discovery.resolver import DiscoveryResolver
from eosdk.eodata.capabilities import BUILTIN_MATRIX
from eosdk.transport import RetryPolicy, Transport

PLATFORM = "https://platform.example.eu"
EXPLICIT_URL = "https://platform.example.eu/api/v1/discovery"


@pytest.fixture
def transport() -> Iterator[Transport]:
    with Transport(retry=RetryPolicy(attempts=2, jitter=False), sleep=lambda _: None) as t:
        yield t


def make_resolver(
    transport: Transport,
    tmp_path: Path,
    *,
    platform: str | None = None,
    discovery_url: str | None = None,
) -> DiscoveryResolver:
    return DiscoveryResolver(
        platform=platform,
        discovery_url=discovery_url,
        transport=transport,
        cache_dir=tmp_path / "discovery",
    )


class TestUnconfigured:
    """No platform root: the resolver degrades to built-ins without any HTTP."""

    def test_not_configured_and_no_url(self, transport: Transport, tmp_path: Path) -> None:
        resolver = make_resolver(transport, tmp_path)
        assert resolver.configured is False
        assert resolver.url is None

    def test_strategies_fall_back_to_builtin_matrix(
        self, transport: Transport, tmp_path: Path
    ) -> None:
        resolver = make_resolver(transport, tmp_path)
        assert resolver.strategies_for("http") == BUILTIN_MATRIX["http"]

    def test_api_version_is_none(self, transport: Transport, tmp_path: Path) -> None:
        resolver = make_resolver(transport, tmp_path)
        assert resolver.api_version_for("data_access/http/odata") is None


class TestUrlDerivation:
    def test_platform_derives_well_known(self, transport: Transport, tmp_path: Path) -> None:
        resolver = make_resolver(transport, tmp_path, platform=f"{PLATFORM}/")
        assert resolver.configured is True
        assert resolver.url == f"{PLATFORM}/.well-known/eo-services.json"

    def test_explicit_discovery_url_wins(self, transport: Transport, tmp_path: Path) -> None:
        resolver = make_resolver(transport, tmp_path, platform=PLATFORM, discovery_url=EXPLICIT_URL)
        assert resolver.url == EXPLICIT_URL

    def test_discovery_url_alone_configures(self, transport: Transport, tmp_path: Path) -> None:
        resolver = make_resolver(transport, tmp_path, discovery_url=EXPLICIT_URL)
        assert resolver.configured is True
        assert resolver.url == EXPLICIT_URL
