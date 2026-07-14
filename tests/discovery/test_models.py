import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from eosdk.discovery.models import (
    DiscoveryError,
    api_versions,
    derive_discovery_url,
    parse_document,
    project_endpoints,
    strategy_sets,
)
from eosdk.eodata.capabilities import Capability

FIXTURES = Path(__file__).parent.parent / "fixtures"


def spec_document() -> dict[str, Any]:
    return json.loads((FIXTURES / "discovery_document.json").read_text())


class TestParse:
    def test_spec_example_parses(self) -> None:
        document = parse_document(spec_document())
        assert document.version == "1.0"
        assert "zipper" in document.services

    @pytest.mark.parametrize("version", ["2.0", "0.9", "banana"])
    def test_unknown_major_rejected_whole(self, version: str) -> None:
        raw = spec_document()
        raw["version"] = version
        with pytest.raises(DiscoveryError, match="schema version"):
            parse_document(raw)

    def test_unknown_services_and_keys_ignored(self) -> None:
        raw = spec_document()
        raw["services"]["quantum_teleporter"] = {"url": "https://qt.example.eu"}
        raw["future_top_level_key"] = {"x": 1}
        document = parse_document(raw)
        assert "quantum_teleporter" in document.services  # kept raw, unused

    def test_minimal_document(self) -> None:
        document = parse_document({"version": "1.2", "services": {}})
        assert document.services == {}


class TestPlatformInfo:
    def test_spec_example_platform_block(self) -> None:
        document = parse_document(spec_document())
        assert document.platform is not None
        assert document.platform.name == "example-eu"
        assert document.platform.description == (
            "Example Earth-observation data platform (example.eu)"
        )
        assert document.platform.profile_name == "example-eu"

    def test_platform_block_optional(self) -> None:
        raw = spec_document()
        del raw["platform"]
        assert parse_document(raw).platform is None

    def test_profile_name_is_slugified(self) -> None:
        raw = spec_document()
        raw["platform"] = {"name": "  Copernicus Data Space!  "}
        info = parse_document(raw).platform
        assert info is not None
        assert info.description is None
        assert info.profile_name == "copernicus-data-space"

    def test_unusable_name_yields_no_profile(self) -> None:
        raw = spec_document()
        raw["platform"] = {"name": "  ***  "}
        info = parse_document(raw).platform
        assert info is not None
        assert info.profile_name is None

    def test_platform_block_without_name_is_malformed(self) -> None:
        raw = spec_document()
        raw["platform"] = {"description": "nameless"}
        with pytest.raises(DiscoveryError, match="malformed"):
            parse_document(raw)


class TestDeriveUrl:
    def test_well_known_derivation(self) -> None:
        url = derive_discovery_url("https://platform.example.eu/")
        assert url == "https://platform.example.eu/.well-known/eo-services.json"

    def test_explicit_override_wins(self) -> None:
        url = derive_discovery_url(
            "https://platform.example.eu", "https://platform.example.eu/api/v1/discovery"
        )
        assert url == "https://platform.example.eu/api/v1/discovery"


class TestProjection:
    def test_spec_example_projection(self) -> None:
        projected = project_endpoints(parse_document(spec_document()))
        assert projected["catalogue_stac"] == "https://catalogue.example.eu/stac"
        assert projected["catalogue_odata"] == "https://catalogue.example.eu/odata"
        assert projected["exos_endpoint"] == "https://s3.example.eu"
        assert projected["exos_region"] == "default"
        assert projected["keys_manager"] == "https://keys.example.eu/api"
        assert projected["keycloak"] == "https://auth.example.eu"
        assert projected["keycloak_realm"] == "eodata"
        assert projected["keycloak_client_id"] == "example-public"
        assert projected["zipper"]  # multi-strategy service collapses to a base

    def test_client_id_absent_not_projected(self) -> None:
        raw = spec_document()
        del raw["services"]["auth"]["client_id"]
        projected = project_endpoints(parse_document(raw))
        assert "keycloak_client_id" not in projected
        assert projected["keycloak_realm"] == "eodata"  # issuer mapping unaffected

    def test_client_id_without_issuer_projected(self) -> None:
        raw = spec_document()
        raw["services"]["auth"] = {"client_id": "standalone-public"}
        projected = project_endpoints(parse_document(raw))
        assert projected["keycloak_client_id"] == "standalone-public"
        assert "keycloak" not in projected

    def test_issuer_without_realms_rejected(self) -> None:
        raw = spec_document()
        raw["services"]["auth"]["issuer"] = "https://auth.example.eu/oauth"
        with pytest.raises(DiscoveryError, match="realms"):
            project_endpoints(parse_document(raw))


class TestStrategySets:
    def test_spec_example_merges_deprecation(self) -> None:
        sets = strategy_sets(parse_document(spec_document()))
        zipper = {s.name: s for s in sets["zipper"]}
        assert zipper["odata"].deprecated is False
        assert zipper["odata"].url == "https://zipper.example.eu/odata"
        assert zipper["resto"].deprecated is True
        assert zipper["resto"].sunset == dt.date(2027, 1, 1)
        assert zipper["resto"].replacement == "odata"
        # order = built-in preference
        assert [s.name for s in sets["zipper"]] == ["odata", "resto"]

    def test_capabilities_restriction(self) -> None:
        raw = spec_document()
        raw["services"]["zipper"]["odata"]["capabilities"] = ["download"]
        sets = strategy_sets(parse_document(raw))
        odata = next(s for s in sets["zipper"] if s.name == "odata")
        assert odata.capabilities == {Capability.DOWNLOAD}  # list disabled remotely

    def test_capabilities_cannot_extend(self) -> None:
        raw = spec_document()
        raw["services"]["zipper"]["resto"]["capabilities"] = ["download", "open", "list"]
        sets = strategy_sets(parse_document(raw))
        resto = next(s for s in sets["zipper"] if s.name == "resto")
        assert resto.capabilities == {Capability.DOWNLOAD}  # built-in matrix rules

    def test_service_described_but_strategy_missing_is_unavailable(self) -> None:
        raw = spec_document()
        del raw["services"]["zipper"]["resto"]
        sets = strategy_sets(parse_document(raw))
        resto = next(s for s in sets["zipper"] if s.name == "resto")
        assert resto.available is False

    def test_service_absent_keeps_builtins(self) -> None:
        raw = spec_document()
        del raw["services"]["zipper"]
        sets = strategy_sets(parse_document(raw))
        assert all(s.available for s in sets["zipper"])

    def test_unknown_strategy_ignored(self) -> None:
        raw = spec_document()
        raw["services"]["zipper"]["grpc"] = {"url": "https://z.example.eu/grpc"}
        sets = strategy_sets(parse_document(raw))
        assert {s.name for s in sets["zipper"]} == {"odata", "resto"}


class TestApiVersions:
    def test_extraction(self) -> None:
        versions = api_versions(parse_document(spec_document()))
        assert versions["keys_manager"] == "v1"
        assert versions["zipper/odata"] == "v1"
        assert versions["catalogue/odata"] == "v1"
        assert "exos" not in versions  # versioning owned externally
