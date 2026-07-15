"""Doctor internals (SPEC §6.8): the Discovery section's document and version checks."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eosdk import Client
from eosdk.doctor import CheckResult, _discovery_section

PLATFORM = "https://platform.example.eu"
WELL_KNOWN = f"{PLATFORM}/.well-known/eo-services.json"
FIXTURES = Path(__file__).parent / "fixtures"


def spec_document() -> dict[str, Any]:
    return json.loads((FIXTURES / "discovery_document.json").read_text())


def make_client(tmp_path: Path, *, platform: str | None = PLATFORM) -> Client:
    return Client(
        platform=platform,
        cwd=tmp_path,
        user_config=tmp_path / "missing.toml",
        token_cache_dir=tmp_path / "tokens",
        discovery_cache_dir=tmp_path / "discovery",
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    with make_client(tmp_path) as c:
        yield c


def by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {result.name: result for result in results}


class TestDiscoverySection:
    def test_unconfigured_platform_is_skipped(self, tmp_path: Path) -> None:
        with make_client(tmp_path, platform=None) as client:
            section = _discovery_section(client)
        (result,) = section.results
        assert result.ok is None  # skipped, never a false failure
        assert "no platform root" in result.detail
        assert section.failed is False

    @respx.mock
    def test_advertised_supported_versions_all_pass(self, client: Client) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        results = by_name(_discovery_section(client).results)
        assert results["platform document"].ok is True
        assert "schema 1.0" in results["platform document"].detail
        for key in ("data_access/http/odata", "catalogue/odata", "data_access/s3/credentials"):
            assert results[f"{key} api"].ok is True
            assert "advertised v1 is supported" in results[f"{key} api"].detail

    @respx.mock
    def test_service_without_advertised_version_not_checked(self, client: Client) -> None:
        document = spec_document()
        del document["services"]["data_access"]["http"]["odata"]["api_version"]
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=document))
        results = by_name(_discovery_section(client).results)
        assert "data_access/http/odata api" not in results  # silent: default applies
        assert results["catalogue/odata api"].ok is True  # siblings still checked
        assert _discovery_section(client).failed is False

    @respx.mock
    def test_unsupported_advertised_version_fails_with_hint(self, client: Client) -> None:
        document = spec_document()
        document["services"]["catalogue"]["odata"]["api_version"] = "v9"
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=document))
        results = by_name(_discovery_section(client).results)
        assert results["api versions"].ok is False
        assert "v9" in results["api versions"].detail
        assert results["api versions"].hint == "upgrade eosdk or pin the URL"

    @respx.mock
    def test_unreachable_document_reports_probe_failure(self, client: Client) -> None:
        respx.get(WELL_KNOWN).mock(side_effect=httpx.ConnectError("down"))
        section = _discovery_section(client)
        results = by_name(section.results)
        assert results["platform document"].ok is False
        assert "unreachable" in results["platform document"].detail
        assert section.failed is True
        # the outage is reported once; it must not resurface as a misleading
        # "api versions" failure hinting at a version mismatch
        assert "api versions" not in results
