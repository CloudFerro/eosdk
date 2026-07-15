import json
from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from eosdk.cli._state import CliState
from eosdk.cli.main import app
from tests.discovery.test_models import spec_document

PLATFORM = "https://platform.example.eu"
WELL_KNOWN = f"{PLATFORM}/.well-known/eo-services.json"


def platform_state(tmp_path: Path) -> CliState:
    config = tmp_path / "config.toml"
    config.write_text(f'default_profile = "prod"\n[profiles.prod]\nplatform = "{PLATFORM}"\n')
    return CliState(
        cwd=tmp_path,
        user_config=config,
        token_cache_dir=tmp_path / "tokens",
        discovery_cache_dir=tmp_path / "discovery",
    )


class TestDiscover:
    @respx.mock
    def test_table_output(self, tmp_path: Path) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        result = CliRunner().invoke(app, ["discover"], obj=platform_state(tmp_path))
        assert result.exit_code == 0, result.output
        assert "data_access" in result.output
        assert "http/odata" in result.output
        assert "resto" in result.output
        assert "deprecated" in result.output
        assert "2027-01-01" in result.output
        assert "platform: example-eu" in result.output
        assert "saved as profile example-eu (created)" in result.output

    @respx.mock
    def test_conflicting_user_profile_reported(self, tmp_path: Path) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        state = platform_state(tmp_path)
        config = tmp_path / "config.toml"
        config.write_text(
            config.read_text() + '[profiles.example-eu]\neodata_http = "https://mine.example.eu"\n'
        )
        result = CliRunner().invoke(app, ["discover"], obj=state)
        assert result.exit_code == 0, result.output
        assert "not discovery-managed" in result.output
        assert 'eodata_http = "https://mine.example.eu"' in config.read_text()

    @respx.mock
    def test_json_output(self, tmp_path: Path) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        result = CliRunner().invoke(app, ["discover", "--json"], obj=platform_state(tmp_path))
        assert result.exit_code == 0
        document = json.loads(result.output)
        assert document["version"] == "1.0"

    @respx.mock
    def test_refresh_busts_cache(self, tmp_path: Path) -> None:
        route = respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        state = platform_state(tmp_path)
        CliRunner().invoke(app, ["discover"], obj=state)
        CliRunner().invoke(app, ["discover"], obj=state)  # served from disk cache
        assert route.call_count == 1
        CliRunner().invoke(app, ["discover", "--refresh"], obj=state)
        assert route.call_count == 2

    def test_no_platform_is_friendly_error(self, tmp_path: Path) -> None:
        state = CliState(
            cwd=tmp_path,
            user_config=tmp_path / "missing.toml",
            discovery_cache_dir=tmp_path / "discovery",
        )
        result = CliRunner().invoke(app, ["discover"], obj=state)
        assert result.exit_code == 1
        assert "EOSDK_PLATFORM" in result.output


class TestDoctorDiscoverySection:
    @respx.mock
    def test_flips_from_skip_to_real_checks(self, tmp_path: Path) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        result = CliRunner().invoke(app, ["doctor", "--json"], obj=platform_state(tmp_path))
        sections = json.loads(result.output)
        discovery = next(s for s in sections if s["section"] == "Discovery")
        document_check = discovery["results"][0]
        assert document_check["ok"] is True
        assert "schema 1.0" in document_check["detail"]

    @respx.mock
    def test_bad_api_version_fails_doctor(self, tmp_path: Path) -> None:
        document = spec_document()
        document["services"]["data_access"]["http"]["odata"]["api_version"] = "v9"
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=document))
        result = CliRunner().invoke(app, ["doctor", "--json"], obj=platform_state(tmp_path))
        assert result.exit_code == 1
        sections = json.loads(result.output)
        discovery = next(s for s in sections if s["section"] == "Discovery")
        assert any(r["ok"] is False for r in discovery["results"])
