import json
from pathlib import Path

from eosdk.config.loader import load
from tests.cli.conftest import Invoke

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


class TestInit:
    def test_init_platform_profile_loadable(self, invoke_bare: Invoke, tmp_path: Path) -> None:
        result = invoke_bare(
            "config",
            "init",
            "--name",
            "prod",
            "--platform",
            "https://platform.example.eu",
        )
        assert result.exit_code == 0, result.output
        config = tmp_path / "empty" / "config.toml"
        # loop closure: the file `eo config init` writes is consumable by load()
        resolved = load(env={}, cwd=tmp_path / "empty", user_config=config)
        assert resolved.profile == "prod"
        assert resolved.platform == "https://platform.example.eu"

    def test_init_refuses_clobber_without_force(self, invoke: Invoke) -> None:
        result = invoke("config", "init", "--name", "test", "--platform", "https://p.example.eu")
        assert result.exit_code == 1
        assert "--force" in result.output
