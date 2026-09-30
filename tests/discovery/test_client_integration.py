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
        assert resolved["eodata_http"].display == PENDING

    @respx.mock
    def test_first_use_resolves_pending_endpoints(self, client: Client) -> None:
        route = mock_platform()
        keycloak_url = client._endpoint("keycloak", service="keycloak")
        assert route.call_count == 1
        assert keycloak_url == "https://auth.example.eu"
        resolved = client.config.resolved()
        assert resolved["eodata_http"].value == "https://download.example.eu/odata"
        assert resolved["eodata_http"].source == "discovery"
        assert resolved["keycloak_realm"].value == "eodata"

    @respx.mock
    def test_discovered_client_id_resolves(self, client: Client) -> None:
        mock_platform()
        client._endpoint("keycloak", service="keycloak")
        resolved = client.config.resolved()
        assert resolved["keycloak_client_id"].value == "example-public"
        assert resolved["keycloak_client_id"].source == "discovery"
        assert client.config.endpoints.keycloak_client_id == "example-public"

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
            'eodata_http = "https://download-canary.example.eu"\n'
        )
        with Client(
            cwd=tmp_path,
            user_config=tmp_path / "missing.toml",
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            pinned = client._endpoint("eodata_http", service="eodata_http")
            assert pinned == "https://download-canary.example.eu"  # pin beats discovery
            stac = client._endpoint("catalogue_stac", service="catalogue_stac")
            assert stac == "https://stac.example.eu/v1"  # sibling discovered
            resolved = client.config.resolved()
            assert resolved["eodata_http"].source.startswith("profile:staging")
            assert resolved["catalogue_stac"].source == "discovery"

    @respx.mock
    def test_discovery_services_and_refresh(self, client: Client) -> None:
        route = mock_platform()
        services = client.discovery.services()
        assert "data_access" in services
        client.discovery.refresh()
        assert route.call_count == 2


class TestPlatformSnapshot:
    """The platform-named profile written from the discovery document (SPEC §6.2)."""

    @respx.mock
    def test_first_use_saves_platform_profile(self, tmp_path: Path) -> None:
        from eosdk.config.loader import _load_config_file

        mock_platform()
        user_config = tmp_path / "config.toml"
        with Client(
            platform=PLATFORM,
            cwd=tmp_path,
            user_config=user_config,
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            client._endpoint("keycloak", service="keycloak")
            assert client.discovered_profile == ("example-eu", "created")
        profile = _load_config_file(user_config).profiles["example-eu"]
        assert profile.platform == PLATFORM
        assert profile.discovered_from == WELL_KNOWN
        assert profile.description == "Example Earth-observation data platform (example.eu)"
        assert profile.eodata_http == "https://download.example.eu/odata"
        assert profile.keycloak == "https://auth.example.eu"

    @respx.mock
    def test_document_without_platform_block_saves_nothing(self, tmp_path: Path) -> None:
        document = spec_document()
        del document["platform"]
        mock_platform(document)
        user_config = tmp_path / "config.toml"
        with Client(
            platform=PLATFORM,
            cwd=tmp_path,
            user_config=user_config,
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            client._endpoint("keycloak", service="keycloak")
            assert client.discovered_profile is None
        assert not user_config.exists()

    @respx.mock
    def test_user_owned_profile_conflict_left_untouched(self, tmp_path: Path) -> None:
        mock_platform()
        user_config = tmp_path / "config.toml"
        original = '[profiles.example-eu]\neodata_http = "https://my-canary.example.eu"\n'
        user_config.write_text(original)
        with Client(
            platform=PLATFORM,
            cwd=tmp_path,
            user_config=user_config,
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            client.discovery.document()
            assert client.discovered_profile == ("example-eu", "conflict")
        assert user_config.read_text() == original

    @respx.mock
    def test_online_change_resyncs_managed_profile_on_refresh(self, tmp_path: Path) -> None:
        from eosdk.config.loader import _load_config_file

        route = mock_platform()
        user_config = tmp_path / "config.toml"
        with Client(
            platform=PLATFORM,
            cwd=tmp_path,
            user_config=user_config,
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            client.discovery.document()
            assert client.discovered_profile == ("example-eu", "created")
            changed = spec_document()
            changed["services"]["data_access"]["s3"]["credentials"]["url"] = (
                "https://keys-v2.example.eu/api"
            )
            route.mock(return_value=httpx.Response(200, json=changed))
            client.discovery.refresh()
            assert client.discovered_profile == ("example-eu", "updated")
        profile = _load_config_file(user_config).profiles["example-eu"]
        assert profile.s3_credentials == "https://keys-v2.example.eu/api"

    @respx.mock
    def test_persistence_failure_does_not_break_the_call(self, tmp_path: Path) -> None:
        mock_platform()
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("")  # config parent path exists as a *file*
        with Client(
            platform=PLATFORM,
            cwd=tmp_path,
            user_config=blocked / "config.toml",
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            assert client._endpoint("keycloak", service="keycloak")
            assert client.discovered_profile is None


class TestVersionGuard:
    @respx.mock
    def test_advertised_v3_fails_before_any_download_call(self, client: Client) -> None:
        document = spec_document()
        document["services"]["data_access"]["http"]["odata"]["api_version"] = "v3"
        mock_platform(document)
        download_route = respx.get(url__startswith="https://download.example.eu")
        with pytest.raises(UnsupportedApiVersion) as exc_info:
            client.download(Product(id="x", name="X"), target=".", via="http")
        message = str(exc_info.value)
        assert "v3" in message
        assert "v1" in message
        assert "EOSDK_EODATA_HTTP_URL" in message
        assert download_route.call_count == 0  # failed early, never mid-download

    @respx.mock
    def test_absent_api_version_uses_default(self, client: Client) -> None:
        document = spec_document()
        del document["services"]["data_access"]["http"]["odata"]["api_version"]
        mock_platform(document)
        # strategy selection + version guard pass; construction succeeds
        downloader = client._http_downloader()
        assert downloader is not None


class TestStrategySelection:
    @respx.mock
    def test_current_strategy_silent(self, client: Client, recwarn: Any) -> None:
        mock_platform()
        client._downloader(
            "http", __import__("eosdk.eodata.capabilities", fromlist=["C"]).Capability.DOWNLOAD
        )
        deprecations = [w for w in recwarn.list if w.category is DeprecationWarning]
        assert not deprecations  # odata chosen; resto never warned about

    @respx.mock
    def test_odata_absent_falls_back_to_resto_with_warning(self, client: Client) -> None:
        from eosdk.eodata.capabilities import Capability

        document = spec_document()
        del document["services"]["data_access"]["http"]["odata"]
        mock_platform(document)
        with (
            pytest.warns(DeprecationWarning, match="2027-01-01") as record,
            # resto is selected (valid for download) but not implemented yet
            pytest.raises(NotImplementedError),
        ):
            client._downloader("http", Capability.DOWNLOAD)
        assert "odata" in str(record[0].message)

    @respx.mock
    def test_list_unavailable_when_odata_absent(self, client: Client) -> None:
        from eosdk.eodata.capabilities import Capability

        document = spec_document()
        del document["services"]["data_access"]["http"]["odata"]
        mock_platform(document)
        with pytest.raises(UnsupportedCapability, match="list"):
            client._downloader("http", Capability.LIST)

    @respx.mock
    def test_capability_disabled_by_deployment(self, client: Client) -> None:
        document = spec_document()
        document["services"]["data_access"]["http"]["odata"]["capabilities"] = ["download"]
        mock_platform(document)
        with pytest.raises(UnsupportedCapability, match="list"):
            client.list(Product(id="x", name="X"), via="http")


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
