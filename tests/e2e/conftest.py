"""Fixtures for the end-to-end journeys.

Every fixture here is hermetic: respx serves the HTTP services, moto serves S3,
``Transport.sleep`` is replaced by a recorder, and all four on-disk caches live
under ``tmp_path``. The library client and the CLI share the *same* config file
and the *same* cache directories, which is what makes surface parity and
process-boundary persistence testable at all.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import respx
from moto import mock_aws
from typer.testing import CliRunner

from eosdk.cli._state import CliState
from eosdk.cli.main import app
from eosdk.transport import Transport
from tests.e2e import platform as fake
from tests.e2e.platform import FakePlatform, install_respx_routes, seed_s3
from tests.e2e.surface import CliResult, CliSurface, Invoke, LibrarySurface, Surface

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from eosdk.client import Client

PROFILE = "e2e"

pytestmark = pytest.mark.e2e


# -- speed and rendering -------------------------------------------------------


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record and skip every transport backoff.

    ``Transport.sleep`` is a dataclass field whose default is captured at class
    creation, so neither ``monkeypatch.setattr(time, "sleep", ...)`` nor setting
    the class attribute reaches instances. Patching ``__post_init__`` does, and
    it covers Transports the CLI builds internally too.
    """
    recorded: list[float] = []
    original = Transport.__post_init__

    def post_init(self: Transport) -> None:
        original(self)
        self.sleep = recorded.append

    monkeypatch.setattr(Transport, "__post_init__", post_init)
    return recorded


@pytest.fixture(autouse=True)
def _plain_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ANSI, no wrapping: CLI output is parsed, not eyeballed."""
    from eosdk.cli import _state

    for console in (_state.stdout, _state.stderr):
        monkeypatch.setattr(console, "_force_terminal", False)
        monkeypatch.setattr(console, "_color_system", None)
        monkeypatch.setattr(console, "_width", 400)


# -- the platform --------------------------------------------------------------


@pytest.fixture
def platform(_instant_sleep: list[float]) -> Iterator[FakePlatform]:
    """A running FakePlatform: respx routes, a moto bucket, the default products."""
    with mock_aws(), respx.mock(assert_all_called=False) as router:
        instance = FakePlatform()
        instance.sleeps = _instant_sleep
        install_respx_routes(instance, router)
        seed_s3(instance)
        yield instance
        assert not instance.unrouted, f"requests nothing served: {instance.unrouted}"


@pytest.fixture
def make_platform(_instant_sleep: list[float]) -> Iterator[Callable[..., FakePlatform]]:
    """Build a FakePlatform with a custom product/collection set."""
    stack = contextlib.ExitStack()
    built: list[FakePlatform] = []

    def factory(**kwargs: Any) -> FakePlatform:
        stack.enter_context(mock_aws())
        router = stack.enter_context(respx.mock(assert_all_called=False))
        instance = FakePlatform(**kwargs)
        instance.sleeps = _instant_sleep
        install_respx_routes(instance, router)
        seed_s3(instance)
        built.append(instance)
        return instance

    with stack:
        yield factory
    for instance in built:
        assert not instance.unrouted, f"requests nothing served: {instance.unrouted}"


# -- workspace (config + the four caches, shared by both surfaces) -------------


@dataclass
class Workspace:
    root: Path
    config: Path
    tokens: Path
    keys: Path
    discovery: Path
    readiness: Path
    downloads: Path

    def write_platform_profile(self, platform_url: str, *, name: str = PROFILE) -> None:
        """A profile with nothing but the platform root: everything discovered."""
        self.config.write_text(
            f'default_profile = "{name}"\n[profiles.{name}]\nplatform = "{platform_url}"\n'
        )

    def write_pinned_profile(self, urls: fake.ServiceUrls, *, name: str = PROFILE) -> None:
        """A profile with every endpoint pinned: discovery never consulted."""
        self.config.write_text(
            f'default_profile = "{name}"\n'
            f"[profiles.{name}]\n"
            f'catalogue_stac = "{urls.stac}"\n'
            f'catalogue_odata = "{urls.odata}"\n'
            f'eodata_http = "{urls.eodata_http}"\n'
            f'keycloak = "{urls.keycloak}"\n'
            f'keycloak_realm = "{fake.REALM}"\n'
            f'keycloak_client_id = "{fake.CLIENT_ID}"\n'
            f's3_credentials = "{urls.keys}"\n'
            f's3_endpoint = "{urls.s3_endpoint}"\n'
            f's3_region = "{urls.s3_region}"\n'
        )

    def cache_files(self) -> dict[str, list[Path]]:
        return {
            "tokens": sorted(self.tokens.glob("*.json")),
            "keys": sorted(self.keys.glob("*.json")),
            "discovery": sorted(self.discovery.glob("*.json")),
            "readiness": sorted(self.readiness.glob("*.json")),
        }


@pytest.fixture
def workspace(tmp_path: Path, platform: FakePlatform) -> Workspace:
    space = _workspace(tmp_path)
    space.write_platform_profile(platform.urls.platform)
    return space


def _workspace(root: Path) -> Workspace:
    home = root / "home"
    home.mkdir(exist_ok=True)
    space = Workspace(
        root=home,
        config=home / "config.toml",
        tokens=home / "tokens",
        keys=home / "s3keys",
        discovery=home / "discovery",
        readiness=home / "readiness",
        downloads=root / "downloads",
    )
    space.downloads.mkdir(exist_ok=True)
    return space


@pytest.fixture
def make_workspace(tmp_path: Path) -> Callable[[str], Workspace]:
    """A second, independent workspace (different caches, same platform)."""

    def factory(name: str) -> Workspace:
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        return _workspace(root)

    return factory


# -- clients -------------------------------------------------------------------


@pytest.fixture
def make_client(workspace: Workspace) -> Iterator[Callable[..., Client]]:
    """Build a ``Client`` against the workspace; every client is closed at teardown."""
    from eosdk.client import Client

    opened: list[Client] = []

    def factory(*, space: Workspace | None = None, **kwargs: Any) -> Client:
        target = space or workspace
        options: dict[str, Any] = {
            "cwd": target.root,
            "user_config": target.config,
            "token_cache_dir": target.tokens,
            "keys_cache_dir": target.keys,
            "discovery_cache_dir": target.discovery,
            "readiness_cache_dir": target.readiness,
        }
        options.update(kwargs)
        client = Client(**options)
        opened.append(client)
        return client

    yield factory
    for client in opened:
        client.close()


@pytest.fixture
def client(make_client: Callable[..., Client]) -> Client:
    """An anonymous client: endpoints resolve from discovery, no session."""
    return make_client()


@pytest.fixture
def library_client(client: Client, platform: FakePlatform) -> Client:
    """A logged-in client — what most journeys start from."""
    client.auth.login(platform.username, platform.password)
    return client


# -- the CLI -------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_invoke(runner: CliRunner, space: Workspace) -> Invoke:
    state = CliState(
        cwd=space.root,
        user_config=space.config,
        token_cache_dir=space.tokens,
        keys_cache_dir=space.keys,
        discovery_cache_dir=space.discovery,
        readiness_cache_dir=space.readiness,
    )

    def invoke(*args: str, **kwargs: Any) -> CliResult:
        result = runner.invoke(app, list(args), obj=state, **kwargs)
        return CliResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=_stderr_of(result),
            stdout_bytes=result.stdout_bytes,
            exception=result.exception,
        )

    return invoke


def _stderr_of(result: Any) -> str:
    try:
        return str(result.stderr)
    except (ValueError, AttributeError):  # runner without a separate stderr buffer
        return ""


@pytest.fixture
def cli(runner: CliRunner, workspace: Workspace) -> Invoke:
    """Invoke ``eo`` in-process against the same config and caches as ``client``."""
    return _make_invoke(runner, workspace)


@pytest.fixture
def make_cli(runner: CliRunner) -> Callable[[Workspace], Invoke]:
    def factory(space: Workspace) -> Invoke:
        return _make_invoke(runner, space)

    return factory


@pytest.fixture
def logged_in_cli(cli: Invoke, platform: FakePlatform) -> Invoke:
    result = cli("auth", "login", "--username", platform.username, "--password", platform.password)
    assert result.exit_code == 0, result.stderr
    return cli


# -- one journey, both surfaces ------------------------------------------------


@pytest.fixture(params=["library", "cli"])
def surface(
    request: pytest.FixtureRequest,
    make_client: Callable[..., Client],
    cli: Invoke,
    platform: FakePlatform,
) -> Surface:
    """Parametrized over both public surfaces; already logged in."""
    adapter: Surface = (
        LibrarySurface(make_client()) if request.param == "library" else CliSurface(cli)
    )
    adapter.login(platform.username, platform.password)
    return adapter
