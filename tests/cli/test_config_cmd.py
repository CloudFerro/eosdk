import json
from pathlib import Path

import httpx
import respx

from eosdk.config.loader import load
from tests.cli.conftest import Invoke
from tests.discovery.test_models import spec_document

WELL_KNOWN = "https://platform.example.eu/.well-known/eo-services.json"

COMMENTED_TOML = """\
# eosdk configuration — do not remove this comment
default_profile = "test"

[profiles.test]  # main profile
catalogue_stac = "https://catalogue.example.eu/stac"  # trailing comment
eodata_http = "https://download.example.eu"
keycloak = "https://auth.example.eu"
"""


class TestShow:
    def test_table_shows_sources(self, invoke: Invoke) -> None:
        result = invoke("config", "show")
        assert result.exit_code == 0
        # rich folds long URLs across lines; assert a fold-safe fragment
        assert "download.example" in result.output
        assert "profile:test" in result.output

    def test_json_output(self, invoke: Invoke) -> None:
        result = invoke("config", "show", "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["eodata_http"]["value"] == "https://download.example.eu"
        assert data["eodata_http"]["source"].startswith("profile:test")


class TestSet:
    def test_set_preserves_comments(self, invoke: Invoke, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(COMMENTED_TOML)
        result = invoke(
            "config", "set", "profiles.test.eodata_http", "https://download-canary.example.eu"
        )
        assert result.exit_code == 0, result.output
        text = config.read_text()
        assert "# eosdk configuration — do not remove this comment" in text
        assert "# main profile" in text
        assert "# trailing comment" in text
        assert "https://download-canary.example.eu" in text

    def test_invalid_url_leaves_file_untouched(self, invoke: Invoke, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        before = config.read_text()
        result = invoke("config", "set", "profiles.test.eodata_http", "not a url")
        assert result.exit_code == 1
        assert config.read_text() == before

    def test_unknown_key_rejected(self, invoke: Invoke) -> None:
        result = invoke("config", "set", "profiles.test.zippr", "https://x.example.eu")
        assert result.exit_code == 1
        assert "zippr" in result.output

    def test_arbitrary_path_rejected(self, invoke: Invoke) -> None:
        result = invoke("config", "set", "some.random.path.here", "value")
        assert result.exit_code == 1


class TestUseAndProfiles:
    def test_use_unknown_profile_lists_available(self, invoke: Invoke) -> None:
        result = invoke("config", "use", "nope")
        assert result.exit_code == 1
        assert "test" in result.output

    def test_use_and_profiles_listing(self, invoke: Invoke, tmp_path: Path) -> None:
        invoke("config", "set", "profiles.staging.platform", "https://staging.example.eu")
        assert invoke("config", "use", "staging").exit_code == 0
        result = invoke("config", "profiles")
        assert result.exit_code == 0
        assert "staging" in result.output
        assert "(default)" in result.output

    def test_profiles_empty_config_points_at_init(self, invoke_bare: Invoke) -> None:
        result = invoke_bare("config", "profiles")
        assert result.exit_code == 0
        # rich folds the long tmp path; normalize whitespace before asserting
        flat = " ".join(result.output.split())
        assert "no profiles in" in flat
        assert "run eo config init" in flat


class TestInit:
    @respx.mock
    def test_init_platform_resolves_and_pins(self, invoke_bare: Invoke, tmp_path: Path) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        result = invoke_bare("config", "init", "--platform", "https://platform.example.eu")
        assert result.exit_code == 0, result.output
        config = tmp_path / "empty" / "config.toml"
        # loop closure: the file `eo config init` writes is consumable by load()
        resolved = load(env={}, cwd=tmp_path / "empty", user_config=config)
        # name comes from the document's platform block (SPEC §6.2), not a prompt
        assert resolved.profile == "example-eu"
        assert resolved.platform == "https://platform.example.eu"
        # endpoints are pinned at init time — no more <pending>
        assert resolved.endpoints.catalogue_stac is not None
        assert resolved.sources["catalogue_stac"].source.startswith("profile:example-eu")
        # discovery_url is derived from the platform, not left pending
        assert resolved.sources["discovery_url"].source == "derived"
        assert resolved.endpoints.discovery_url == (
            "https://platform.example.eu/.well-known/eo-services.json"
        )
        # the success message points the user at the next verification step
        assert "eo doctor" in result.output

    @respx.mock
    def test_init_name_overrides_platform_name(self, invoke_bare: Invoke, tmp_path: Path) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        result = invoke_bare(
            "config", "init", "--name", "prod", "--platform", "https://platform.example.eu"
        )
        assert result.exit_code == 0, result.output
        config = tmp_path / "empty" / "config.toml"
        resolved = load(env={}, cwd=tmp_path / "empty", user_config=config)
        assert resolved.profile == "prod"
        assert resolved.endpoints.catalogue_stac is not None

    @respx.mock
    def test_init_reinit_resyncs_managed_profile_without_force(
        self, invoke_bare: Invoke, tmp_path: Path
    ) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        first = invoke_bare("config", "init", "--platform", "https://platform.example.eu")
        assert first.exit_code == 0, first.output
        # re-running init on a discovery-managed profile resyncs it, no --force
        second = invoke_bare("config", "init", "--platform", "https://platform.example.eu")
        assert second.exit_code == 0, second.output
        config = tmp_path / "empty" / "config.toml"
        resolved = load(env={}, cwd=tmp_path / "empty", user_config=config)
        assert resolved.profile == "example-eu"

    def test_init_unreachable_platform_hard_fails(
        self, invoke_bare: Invoke, tmp_path: Path
    ) -> None:
        # discovery is not mocked: the fetch fails and no profile is written
        result = invoke_bare("config", "init", "--platform", "https://platform.example.eu")
        assert result.exit_code == 1
        assert not (tmp_path / "empty" / "config.toml").exists()

    @respx.mock
    def test_init_refuses_clobber_without_force(self, invoke: Invoke) -> None:
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        result = invoke(
            "config", "init", "--name", "test", "--platform", "https://platform.example.eu"
        )
        assert result.exit_code == 1
        assert "--force" in result.output

    @respx.mock
    def test_init_prompts_for_platform_url(self, invoke_bare: Invoke, tmp_path: Path) -> None:
        # no --platform: confirm the single-root question, then type the URL
        respx.get(WELL_KNOWN).mock(return_value=httpx.Response(200, json=spec_document()))
        result = invoke_bare("config", "init", input="y\nhttps://platform.example.eu\n")
        assert result.exit_code == 0, result.output
        config = tmp_path / "empty" / "config.toml"
        resolved = load(env={}, cwd=tmp_path / "empty", user_config=config)
        assert resolved.profile == "example-eu"
        assert resolved.platform == "https://platform.example.eu"

    def test_init_manual_endpoint_entry(self, invoke_bare: Invoke, tmp_path: Path) -> None:
        # decline the platform root; fields prompt in sorted order — fill
        # catalogue_stac (second) and leave the other five empty to skip them
        answers = "n\n" + "\n" + "https://cat.example.eu/stac\n" + "\n" * 4
        result = invoke_bare("config", "init", "--name", "manual", input=answers)
        assert result.exit_code == 0, result.output
        assert "Manual endpoint entry" in result.output
        text = (tmp_path / "empty" / "config.toml").read_text()
        assert 'catalogue_stac = "https://cat.example.eu/stac"' in text
        assert "catalogue_odata" not in text
        assert "platform" not in text
