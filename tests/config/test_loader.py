from pathlib import Path

import pytest

from eosdk.config.defaults import ENV_VAR_MAP
from eosdk.config.loader import load, user_config_path
from eosdk.config.settings import PENDING, Endpoints
from eosdk.exceptions import ConfigError

KW_URL = "https://kwargs.example.eu"
ENV_URL = "https://env.example.eu"
LOCAL_URL = "https://local.example.eu"
USER_URL = "https://user.example.eu"


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def local_toml(tmp_path: Path) -> Path:
    return write(
        tmp_path / "cwd" / "eosdk.toml",
        f'default_profile = "dev"\n[profiles.dev]\neodata_http = "{LOCAL_URL}"\n',
    )


@pytest.fixture
def user_toml(tmp_path: Path) -> Path:
    return write(
        tmp_path / "home" / "config.toml",
        f'default_profile = "dev"\n[profiles.dev]\neodata_http = "{USER_URL}"\n'
        f'[profiles.other]\neodata_http = "https://other.example.eu"\n',
    )


@pytest.fixture
def missing(tmp_path: Path) -> Path:
    return tmp_path / "does-not-exist" / "config.toml"


class TestPrecedenceMatrix:
    """Pairwise: each layer beats the next one down, for the same field."""

    def test_kwargs_beat_env(self, tmp_path: Path, missing: Path) -> None:
        cfg = load(
            kwargs_endpoints={"eodata_http": KW_URL},
            env={"EOSDK_EODATA_HTTP_URL": ENV_URL},
            cwd=tmp_path,
            user_config=missing,
        )
        assert cfg.endpoints.eodata_http == KW_URL
        assert cfg.sources["eodata_http"].source == "kwargs"

    def test_env_beats_local_file(self, local_toml: Path, missing: Path) -> None:
        cfg = load(
            env={"EOSDK_EODATA_HTTP_URL": ENV_URL},
            cwd=local_toml.parent,
            user_config=missing,
        )
        assert cfg.endpoints.eodata_http == ENV_URL
        assert cfg.sources["eodata_http"].source == "env:EOSDK_EODATA_HTTP_URL"

    def test_local_file_beats_user_file(self, local_toml: Path, user_toml: Path) -> None:
        cfg = load(env={}, cwd=local_toml.parent, user_config=user_toml)
        assert cfg.endpoints.eodata_http == LOCAL_URL
        assert cfg.sources["eodata_http"].source == f"profile:dev({local_toml})"

    def test_user_file_beats_default(self, tmp_path: Path, user_toml: Path) -> None:
        cfg = load(env={}, cwd=tmp_path, user_config=user_toml)
        assert cfg.endpoints.eodata_http == USER_URL
        assert cfg.sources["eodata_http"].source == f"profile:dev({user_toml})"

    def test_all_layers_present_kwargs_win(self, local_toml: Path, user_toml: Path) -> None:
        cfg = load(
            kwargs_endpoints={"eodata_http": KW_URL},
            env={"EOSDK_EODATA_HTTP_URL": ENV_URL},
            cwd=local_toml.parent,
            user_config=user_toml,
        )
        assert cfg.endpoints.eodata_http == KW_URL

    def test_unpinned_field_falls_to_default(self, tmp_path: Path, missing: Path) -> None:
        cfg = load(env={}, cwd=tmp_path, user_config=missing)
        assert cfg.endpoints.keycloak_realm == "CDSE"
        assert cfg.sources["keycloak_realm"].source == "default"


class TestProfileSelection:
    def test_kwarg_beats_env_profile(self, user_toml: Path, tmp_path: Path) -> None:
        cfg = load(
            profile="other",
            env={"EOSDK_PROFILE": "dev"},
            cwd=tmp_path,
            user_config=user_toml,
        )
        assert cfg.profile == "other"
        assert cfg.endpoints.eodata_http == "https://other.example.eu"

    def test_env_profile_beats_default_profile(self, user_toml: Path, tmp_path: Path) -> None:
        cfg = load(env={"EOSDK_PROFILE": "other"}, cwd=tmp_path, user_config=user_toml)
        assert cfg.profile == "other"

    def test_default_profile_used(self, user_toml: Path, tmp_path: Path) -> None:
        cfg = load(env={}, cwd=tmp_path, user_config=user_toml)
        assert cfg.profile == "dev"

    def test_unknown_profile_lists_available(self, user_toml: Path, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"dev.*other|other.*dev"):
            load(profile="nope", env={}, cwd=tmp_path, user_config=user_toml)


class TestDiscoveryPending:
    def test_platform_only_profile_yields_pending(self, tmp_path: Path, missing: Path) -> None:
        local = write(
            tmp_path / "eosdk.toml",
            'default_profile = "prod"\n[profiles.prod]\nplatform = "https://platform.example.eu"\n',
        )
        cfg = load(env={}, cwd=local.parent, user_config=missing)
        assert cfg.platform == "https://platform.example.eu"
        resolved = cfg.resolved()
        assert resolved["eodata_http"].source == "discovery"
        assert resolved["eodata_http"].display == PENDING
        assert cfg.endpoints.eodata_http is None

    def test_pinned_field_beats_discovery(self, tmp_path: Path, missing: Path) -> None:
        local = write(
            tmp_path / "eosdk.toml",
            'default_profile = "staging"\n[profiles.staging]\n'
            'platform = "https://staging.example.eu"\n'
            f'eodata_http = "{LOCAL_URL}"\n',
        )
        cfg = load(env={}, cwd=local.parent, user_config=missing)
        assert cfg.endpoints.eodata_http == LOCAL_URL
        assert cfg.resolved()["catalogue_stac"].display == PENDING

    def test_platform_kwarg_bootstrap(self, tmp_path: Path, missing: Path) -> None:
        cfg = load(platform="https://p.example.eu", env={}, cwd=tmp_path, user_config=missing)
        assert cfg.platform == "https://p.example.eu"
        assert cfg.resolved()["eodata_http"].source == "discovery"

    def test_require_pending_raises_with_pin_hint(self, tmp_path: Path, missing: Path) -> None:
        cfg = load(platform="https://p.example.eu", env={}, cwd=tmp_path, user_config=missing)
        with pytest.raises(ConfigError, match="EOSDK_EODATA_HTTP_URL"):
            cfg.require("eodata_http", service="eodata_http")

    def test_require_unset_raises_with_pin_hint(self, tmp_path: Path, missing: Path) -> None:
        cfg = load(env={}, cwd=tmp_path, user_config=missing)
        with pytest.raises(ConfigError, match="EOSDK_EODATA_HTTP_URL"):
            cfg.require("eodata_http", service="eodata_http")


class StubDiscovery:
    """DiscoveryHook double: serves a fixed field->URL mapping, records calls."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def resolve(self, platform: str) -> dict[str, str]:
        self.calls.append(platform)
        return self.mapping


class TestDiscoveryHook:
    """Precedence step 5: values a live discovery hook returns fill pending fields."""

    DISCOVERED = "https://discovered.example.eu"

    def test_discovered_value_fills_pending_field(self, tmp_path: Path, missing: Path) -> None:
        hook = StubDiscovery({"eodata_http": f"{self.DISCOVERED}/", "s3_region": "waw3-1"})
        cfg = load(
            platform="https://p.example.eu",
            env={},
            cwd=tmp_path,
            user_config=missing,
            discovery=hook,
        )
        assert hook.calls == ["https://p.example.eu"]
        assert cfg.endpoints.eodata_http == self.DISCOVERED  # URL-validated, "/" trimmed
        assert cfg.sources["eodata_http"].source == "discovery"
        assert cfg.endpoints.s3_region == "waw3-1"  # non-URL field passes through
        assert cfg.resolved()["catalogue_stac"].display == PENDING  # not advertised

    def test_local_pin_beats_discovered_value(self, tmp_path: Path, missing: Path) -> None:
        hook = StubDiscovery({"eodata_http": self.DISCOVERED})
        cfg = load(
            platform="https://p.example.eu",
            env={"EOSDK_EODATA_HTTP_URL": ENV_URL},
            cwd=tmp_path,
            user_config=missing,
            discovery=hook,
        )
        assert cfg.endpoints.eodata_http == ENV_URL

    def test_invalid_discovered_url_rejected(self, tmp_path: Path, missing: Path) -> None:
        hook = StubDiscovery({"eodata_http": "not a url"})
        with pytest.raises(ConfigError, match="discovery"):
            load(
                platform="https://p.example.eu",
                env={},
                cwd=tmp_path,
                user_config=missing,
                discovery=hook,
            )

    def test_hook_not_consulted_without_platform(self, tmp_path: Path, missing: Path) -> None:
        hook = StubDiscovery({"eodata_http": self.DISCOVERED})
        cfg = load(env={}, cwd=tmp_path, user_config=missing, discovery=hook)
        assert hook.calls == []
        assert cfg.endpoints.eodata_http is None


class TestValidation:
    def test_bad_env_url_names_env_var(self, tmp_path: Path, missing: Path) -> None:
        with pytest.raises(ConfigError, match="EOSDK_EODATA_HTTP_URL"):
            load(env={"EOSDK_EODATA_HTTP_URL": "not a url"}, cwd=tmp_path, user_config=missing)

    def test_malformed_toml_names_file(self, tmp_path: Path, missing: Path) -> None:
        write(tmp_path / "eosdk.toml", "definitely [not toml")
        with pytest.raises(ConfigError, match=r"eosdk\.toml"):
            load(env={}, cwd=tmp_path, user_config=missing)

    def test_valid_toml_bad_schema_names_file(self, tmp_path: Path, missing: Path) -> None:
        write(tmp_path / "eosdk.toml", "banana = 1\n")  # unknown top-level key
        with pytest.raises(ConfigError, match=r"invalid config file .*eosdk\.toml"):
            load(env={}, cwd=tmp_path, user_config=missing)

    def test_unknown_kwarg_endpoint(self, tmp_path: Path, missing: Path) -> None:
        with pytest.raises(ConfigError, match="zippr"):
            load(kwargs_endpoints={"zippr": KW_URL}, env={}, cwd=tmp_path, user_config=missing)

    def test_tls_verify_env(self, tmp_path: Path, missing: Path) -> None:
        cfg = load(env={"EOSDK_TLS_VERIFY": "0"}, cwd=tmp_path, user_config=missing)
        assert cfg.verify_tls is False


class TestDefaultsIntegrity:
    def test_env_var_map_matches_model(self) -> None:
        assert set(ENV_VAR_MAP) == set(Endpoints.model_fields)


def test_user_config_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert user_config_path() == tmp_path / "eosdk" / "config.toml"
