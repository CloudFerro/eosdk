"""save_discovered_profile: create / resync / conflict semantics (SPEC §6.2)."""

from pathlib import Path

import pytest

from eosdk.config.loader import _load_config_file
from eosdk.config.profiles import get_default_profile, save_discovered_profile

DISCOVERY_URL = "https://platform.example.eu/.well-known/eo-services.json"
VALUES = {
    "platform": "https://platform.example.eu",
    "description": "Example platform",
    "catalogue_stac": "https://catalogue.example.eu/stac",
    "zipper": "https://zipper.example.eu/odata",
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
        assert profile.zipper == "https://zipper.example.eu/odata"
        assert profile.discovered_from == DISCOVERY_URL

    def test_becomes_default_only_when_none_set(self, config: Path) -> None:
        save_discovered_profile(config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL)
        assert get_default_profile(config) == "example-eu"

    def test_existing_default_untouched(self, config: Path) -> None:
        config.write_text('default_profile = "mine"\n[profiles.mine]\nzipper = "https://z.eu"\n')
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
        moved = dict(VALUES, zipper="https://zipper-v2.example.eu/odata")
        del moved["catalogue_stac"]  # service withdrawn online
        status = save_discovered_profile(
            config, "example-eu", values=moved, discovered_from=DISCOVERY_URL
        )
        assert status == "updated"
        profile = _load_config_file(config).profiles["example-eu"]
        assert profile.zipper == "https://zipper-v2.example.eu/odata"
        assert profile.catalogue_stac is None  # mirror resyncs wholesale

    def test_user_owned_profile_is_a_conflict(self, config: Path) -> None:
        config.write_text('[profiles.example-eu]\nzipper = "https://my-canary.example.eu"\n')
        status = save_discovered_profile(
            config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL
        )
        assert status == "conflict"
        profile = _load_config_file(config).profiles["example-eu"]
        assert profile.zipper == "https://my-canary.example.eu"  # untouched
        assert profile.discovered_from is None

    def test_sibling_profiles_and_comments_survive(self, config: Path) -> None:
        config.write_text(
            "# my hand-written config\n"
            'default_profile = "mine"\n'
            "[profiles.mine]\n"
            'zipper = "https://z.eu"\n'
        )
        save_discovered_profile(config, "example-eu", values=VALUES, discovered_from=DISCOVERY_URL)
        text = config.read_text()
        assert "# my hand-written config" in text
        assert "profiles.mine" in text.replace('"', "")
