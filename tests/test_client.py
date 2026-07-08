from pathlib import Path

from eosdk import Client


def test_client_resolves_profile_endpoints(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'default_profile = "local"\n'
        "[profiles.local]\n"
        'catalogue_stac = "http://localhost:8081/stac"\n'
        'zipper = "http://localhost:8082"\n'
        'keycloak = "http://localhost:8180"\n'
    )
    with Client(profile="local", cwd=tmp_path, user_config=config) as client:
        resolved = client.config.resolved()
    assert client.config.endpoints.zipper == "http://localhost:8082"
    assert resolved["zipper"].source == f"profile:local({config})"
    assert resolved["keycloak_realm"].source == "default"


def test_client_kwargs_override(tmp_path: Path) -> None:
    with Client(
        endpoints={"zipper": "http://localhost:9999"},
        cwd=tmp_path,
        user_config=tmp_path / "missing.toml",
    ) as client:
        assert client.config.endpoints.zipper == "http://localhost:9999"
        assert client.config.resolved()["zipper"].source == "kwargs"


def test_client_importable_from_package_root() -> None:
    from eosdk import Client as RootClient

    assert RootClient is Client


def test_context_manager_closes_transport(tmp_path: Path) -> None:
    client = Client(cwd=tmp_path, user_config=tmp_path / "missing.toml")
    with client:
        pass
    assert client._transport._client.is_closed
