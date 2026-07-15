"""Profile CRUD: discovered-profile sync (SPEC §6.2) and config writes (SPEC §7.4)."""

from pathlib import Path

import pytest
import tomlkit

from eosdk.config.loader import _load_config_file
from eosdk.config.profiles import (
    get_default_profile,
    list_profiles,
    save_discovered_profile,
    set_value,
)
from eosdk.exceptions import ConfigError

DISCOVERY_URL = "https://platform.example.eu/.well-known/eo-services.json"
VALUES = {
    "platform": "https://platform.example.eu",
    "description": "Example platform",
    "catalogue_stac": "https://catalogue.example.eu/stac",
    "eodata_http": "https://download.example.eu/odata",
}


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


class TestSaveDiscoveredProfile:
    def test_first_use_creates_marked_profile(self, config: Path) -> None:
        status = save_discovered_profile(
            config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL
        )
        assert status == "created"
        profile = _load_config_file(config).profiles["example-eu"]
        assert profile.platform == "https://platform.example.eu"
        assert profile.description == "Example platform"
        assert profile.eodata_http == "https://download.example.eu/odata"
        assert profile.discovered_from == DISCOVERY_URL

    def test_becomes_default_only_when_none_set(self, config: Path) -> None:
        save_discovered_profile(config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL)
        assert get_default_profile(config) == "example-eu"

    def test_existing_default_untouched(self, config: Path) -> None:
        config.write_text(
            'default_profile = "mine"\n[profiles.mine]\neodata_http = "https://z.eu"\n'
        )
        save_discovered_profile(config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL)
        assert get_default_profile(config) == "mine"

    def test_unchanged_document_does_not_rewrite(self, config: Path) -> None:
        save_discovered_profile(config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL)
        before = config.stat().st_mtime_ns
        status = save_discovered_profile(
            config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL
        )
        assert status == "unchanged"
        assert config.stat().st_mtime_ns == before

    def test_changed_document_resyncs_managed_profile(self, config: Path) -> None:
        save_discovered_profile(config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL)
        moved = dict(VALUES, eodata_http="https://download-v2.example.eu/odata")
        del moved["catalogue_stac"]  # service withdrawn online
        status = save_discovered_profile(
            config, "example-eu", values=moved, discovered_from=DISCOVERY_URL
        )
        assert status == "updated"
        profile = _load_config_file(config).profiles["example-eu"]
        assert profile.eodata_http == "https://download-v2.example.eu/odata"
        assert profile.catalogue_stac is None  # mirror resyncs wholesale

    def test_user_owned_profile_is_a_conflict(self, config: Path) -> None:
        config.write_text('[profiles.example-eu]\neodata_http = "https://my-canary.example.eu"\n')
        status = save_discovered_profile(
            config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL
        )
        assert status == "conflict"
        profile = _load_config_file(config).profiles["example-eu"]
        assert profile.eodata_http == "https://my-canary.example.eu"  # untouched
        assert profile.discovered_from is None

    def test_sibling_profiles_and_comments_survive(self, config: Path) -> None:
        config.write_text(
            "# my hand-written config\n"
            'default_profile = "mine"\n'
            "[profiles.mine]\n"
            'eodata_http = "https://z.eu"\n'
        )
        save_discovered_profile(config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL)
        text = config.read_text()
        assert "# my hand-written config" in text
        assert "profiles.mine" in text.replace('"', "")


class TestReadHelpers:
    def test_list_profiles_missing_file_is_empty(self, config: Path) -> None:
        assert list_profiles(config) == {}

    def test_get_default_profile_missing_file_is_none(self, config: Path) -> None:
        assert get_default_profile(config) is None


class TestSetValue:
    def test_default_profile_key_delegates(self, config: Path) -> None:
        set_value(config, "profiles.dev.eodata_http", "https://z.example.eu")
        set_value(config, "default_profile", "dev")
        assert get_default_profile(config) == "dev"

    def test_default_profile_key_rejects_unknown_name(self, config: Path) -> None:
        set_value(config, "profiles.dev.eodata_http", "https://z.example.eu")
        with pytest.raises(ConfigError, match="dev"):
            set_value(config, "default_profile", "nope")
        assert get_default_profile(config) is None


class TestAtomicWrite:
    def test_failed_write_keeps_file_and_leaves_no_temp(
        self, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_value(config, "profiles.dev.eodata_http", "https://z.example.eu")
        before = config.read_text()

        def boom(document: tomlkit.TOMLDocument) -> str:
            raise OSError("disk full")

        monkeypatch.setattr(tomlkit, "dumps", boom)
        with pytest.raises(OSError, match="disk full"):
            set_value(config, "profiles.dev.description", "broken write")
        assert config.read_text() == before  # target file untouched
        assert list(config.parent.glob("*.tmp")) == []  # temp file cleaned up
