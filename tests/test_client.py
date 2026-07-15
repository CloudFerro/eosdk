from pathlib import Path

from eosdk import Client


def test_client_resolves_profile_endpoints(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'default_profile = "local"\n'
        "[profiles.local]\n"
        'catalogue_stac = "http://localhost:8081/stac"\n'
        'eodata_http = "http://localhost:8082"\n'
        'keycloak = "http://localhost:8180"\n'
    )
    with Client(profile="local", cwd=tmp_path, user_config=config) as client:
        resolved = client.config.resolved()
    assert client.config.endpoints.eodata_http == "http://localhost:8082"
    assert resolved["eodata_http"].source == f"profile:local({config})"
    assert resolved["keycloak_realm"].source == "default"


def test_client_kwargs_override(tmp_path: Path) -> None:
    with Client(
        endpoints={"eodata_http": "http://localhost:9999"},
        cwd=tmp_path,
        user_config=tmp_path / "missing.toml",
    ) as client:
        assert client.config.endpoints.eodata_http == "http://localhost:9999"
        assert client.config.resolved()["eodata_http"].source == "kwargs"


def test_client_importable_from_package_root() -> None:
    from eosdk import Client as RootClient

    assert RootClient is Client


def test_context_manager_closes_transport(tmp_path: Path) -> None:
    client = Client(cwd=tmp_path, user_config=tmp_path / "missing.toml")
    with client:
        pass
    assert client._transport._client.is_closed


def test_odata_catalogue_memoized(tmp_path: Path) -> None:
    from eosdk.catalogue.odata import ODataCatalogue

    config = tmp_path / "config.toml"
    config.write_text(
        'default_profile = "local"\n'
        "[profiles.local]\n"
        'catalogue_odata = "http://localhost:8083/odata"\n'
        'keycloak = "http://localhost:8180"\n'
    )
    with Client(
        profile="local", cwd=tmp_path, user_config=config, token_cache_dir=tmp_path / "tokens"
    ) as client:
        catalogue = client._catalogue("odata")
        assert isinstance(catalogue, ODataCatalogue)
        assert client._catalogue("odata") is catalogue  # lazy accessor memoizes


def test_discovery_url_recorded_in_platform_snapshot(tmp_path: Path) -> None:
    """A client bootstrapped from an explicit discovery_url stamps it into the
    saved platform profile, so the profile can re-discover on its own later."""
    import httpx
    import respx

    from eosdk.config.loader import _load_config_file
    from tests.discovery.test_models import spec_document

    well_known = "https://platform.example.eu/.well-known/eo-services.json"
    user_config = tmp_path / "config.toml"
    with respx.mock:
        respx.get(well_known).mock(return_value=httpx.Response(200, json=spec_document()))
        with Client(
            endpoints={"discovery_url": well_known},
            cwd=tmp_path,
            user_config=user_config,
            discovery_cache_dir=tmp_path / "discovery",
        ) as client:
            client.discovery.document()
            assert client.discovered_profile == ("example-eu", "created")
    profile = _load_config_file(user_config).profiles["example-eu"]
    assert profile.discovery_url == well_known
