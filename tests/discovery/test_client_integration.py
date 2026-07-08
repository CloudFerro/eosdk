"""Phase-3 exit criteria: single-root bootstrap, precedence, version guard."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk import Client
from eosdk.config.settings import PENDING
from eosdk.exceptions import UnsupportedApiVersion, UnsupportedCapability
from eosdk.models import Product
from tests.discovery.test_models import spec_document

PLATFORM = "https://platform.example.eu"
WELL_KNOWN = f"{PLATFORM}/.well-known/eo-services.json"
FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    with Client(
        platform=PLATFORM,
        cwd=tmp_path,
        user_config=tmp_path / "missing.toml",
        token_cache_dir=tmp_path / "tokens",
        discovery_cache_dir=tmp_path / "discovery",
    ) as c:
        yield c


def mock_platform(document: dict[str, Any] | None = None) -> respx.Route:
    return respx.get(WELL_KNOWN).mock(
        return_value=httpx.Response(200, json=document or spec_document())
    )


class TestSingleRootBootstrap:
    @respx.mock
    def test_construction_makes_zero_http_calls(self, tmp_path: Path) -> None:
        route = mock_platform()
        with Client(
            platform=PLATFORM,
            cwd=tmp_path,
            user_config=tmp_path / "missing.toml",
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            resolved = client.config.resolved()
        assert route.call_count == 0  # SPEC §6.1: construction is offline
        assert resolved["zipper"].display == PENDING

    @respx.mock
    def test_first_use_resolves_pending_endpoints(self, client: Client) -> None:
        route = mock_platform()
        keycloak_url = client._endpoint("keycloak", service="keycloak")
        assert route.call_count == 1
        assert keycloak_url == "https://auth.example.eu"
        resolved = client.config.resolved()
        assert resolved["zipper"].value == "https://zipper.example.eu/odata"
        assert resolved["zipper"].source == "discovery"
        assert resolved["keycloak_realm"].value == "eodata"

    @respx.mock
    def test_second_use_is_memoized(self, client: Client) -> None:
        route = mock_platform()
        client._endpoint("keycloak", service="keycloak")
        client._endpoint("catalogue_stac", service="catalogue_stac")
        assert route.call_count == 1

    @respx.mock
    def test_pinned_endpoint_never_reconsulted(self, tmp_path: Path) -> None:
        mock_platform()
        config = tmp_path / "eosdk.toml"
        config.write_text(
            'default_profile = "staging"\n'
            "[profiles.staging]\n"
            f'platform = "{PLATFORM}"\n'
            'zipper = "https://zipper-canary.example.eu"\n'
        )
        with Client(
            cwd=tmp_path,
            user_config=tmp_path / "missing.toml",
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            zipper = client._endpoint("zipper", service="zipper")
            assert zipper == "https://zipper-canary.example.eu"  # pin beats discovery
            stac = client._endpoint("catalogue_stac", service="catalogue_stac")
            assert stac == "https://catalogue.example.eu/stac"  # sibling discovered
            resolved = client.config.resolved()
            assert resolved["zipper"].source.startswith("profile:staging")
            assert resolved["catalogue_stac"].source == "discovery"

    @respx.mock
    def test_discovery_services_and_refresh(self, client: Client) -> None:
        route = mock_platform()
        services = client.discovery.services()
        assert "zipper" in services
        client.discovery.refresh()
        assert route.call_count == 2


class TestVersionGuard:
    @respx.mock
    def test_advertised_v3_fails_before_any_zipper_call(self, client: Client) -> None:
        document = spec_document()
        document["services"]["zipper"]["odata"]["api_version"] = "v3"
        mock_platform(document)
        zipper_route = respx.get(url__startswith="https://zipper.example.eu")
        with pytest.raises(UnsupportedApiVersion) as exc_info:
            client.download(Product(id="x", name="X"), target=".", via="zipper")
        message = str(exc_info.value)
        assert "v3" in message
        assert "v1" in message
        assert "EOSDK_ZIPPER_URL" in message
        assert zipper_route.call_count == 0  # failed early, never mid-download

    @respx.mock
    def test_absent_api_version_uses_default(self, client: Client) -> None:
        document = spec_document()
        del document["services"]["zipper"]["odata"]["api_version"]
        mock_platform(document)
        # strategy selection + version guard pass; construction succeeds
        downloader = client._zipper_downloader()
        assert downloader is not None


class TestStrategySelection:
    @respx.mock
    def test_current_strategy_silent(self, client: Client, recwarn: Any) -> None:
        mock_platform()
        client._downloader(
            "zipper", __import__("eosdk.eodata.capabilities", fromlist=["C"]).Capability.DOWNLOAD
        )
        deprecations = [w for w in recwarn.list if w.category is DeprecationWarning]
        assert not deprecations  # odata chosen; resto never warned about

    @respx.mock
    def test_odata_absent_falls_back_to_resto_with_warning(self, client: Client) -> None:
        from eosdk.eodata.capabilities import Capability

        document = spec_document()
        del document["services"]["zipper"]["odata"]
        mock_platform(document)
        with (
            pytest.warns(DeprecationWarning, match="2027-01-01") as record,
            # resto is selected (valid for download) but not implemented yet
            pytest.raises(NotImplementedError),
        ):
            client._downloader("zipper", Capability.DOWNLOAD)
        assert "odata" in str(record[0].message)

    @respx.mock
    def test_list_unavailable_when_odata_absent(self, client: Client) -> None:
        from eosdk.eodata.capabilities import Capability

        document = spec_document()
        del document["services"]["zipper"]["odata"]
        mock_platform(document)
        with pytest.raises(UnsupportedCapability, match="list"):
            client._downloader("zipper", Capability.LIST)

    @respx.mock
    def test_capability_disabled_by_deployment(self, client: Client) -> None:
        document = spec_document()
        document["services"]["zipper"]["odata"]["capabilities"] = ["download"]
        mock_platform(document)
        with pytest.raises(UnsupportedCapability, match="list"):
            client.list(Product(id="x", name="X"), via="zipper")


class TestOidcFixtureAlignment:
    @respx.mock
    def test_discovered_keycloak_feeds_auth(self, client: Client) -> None:
        mock_platform()
        respx.get("https://auth.example.eu/realms/eodata/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json=json.loads((FIXTURES / "keycloak_openid_configuration.json").read_text()),
            )
        )
        endpoints = client.auth._endpoints()
        assert endpoints.issuer == "https://auth.example.eu/realms/eodata"
