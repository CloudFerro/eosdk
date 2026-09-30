"""Live smoke tests against a real platform (SPEC §12, plan §5).

Skipped unless ``EOSDK_SMOKE=1``. Anonymous tests need only that; the
``smoke_auth`` tier additionally needs ``EOSDK_SMOKE_USERNAME`` /
``EOSDK_SMOKE_PASSWORD`` (and optionally ``EOSDK_SMOKE_PLATFORM`` or ``EOSDK_*``
endpoint pins; endpoints are otherwise discovered from the CDSE platform root,
https://discover.dataspace.copernicus.eu).

Two things this suite has to get right beyond the assertions:

* **Nothing may leak server-side.** Every key pair the run creates is recorded
  in a session-scoped registry the moment it exists, and revoked in a ``finally``
  at session end. The registry is deliberately *not* derived from the on-disk
  key cache: that cache lives under ``tmp_path``, and a crash between creating a
  key and writing the cache — or a lost ``tmp_path`` — would orphan the key on
  the account with no record of it anywhere.
* **A failed nightly must be diagnosable without a rerun.** The session writes a
  summary artifact naming the platform, the endpoints that were resolved, every
  product id touched, every key the run minted (``keys_created``) and the ones
  teardown could not account for (``keys_leaked`` — empty on a clean run).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from eosdk import Client
from eosdk.exceptions import AuthError, EosdkError, S3KeyLimitReached

if TYPE_CHECKING:
    from collections.abc import Iterator

    from eosdk.models import Product

DEFAULT_PLATFORM = "https://discover.dataspace.copernicus.eu"
SUMMARY_ENV = "EOSDK_SMOKE_SUMMARY"
DEFAULT_SUMMARY = "smoke-summary.json"
#: seconds allowed for the one-shot `eo` commands that set a session up
CLI_SETUP_TIMEOUT = 120.0


@contextmanager
def skip_on_key_quota() -> Iterator[None]:
    """A full S3 key quota is an account precondition, not an SDK defect."""
    try:
        yield
    except S3KeyLimitReached as exc:
        # pytest runs with -q and no -rs, so a bare skip reason is invisible;
        # warn so the quota problem surfaces in the warnings summary.
        warnings.warn(
            f"smoke test skipped: could not create credentials in S3 Keys Manager: {exc}",
            stacklevel=3,
        )
        pytest.skip(f"account precondition not met: {exc}")


def _smoke_enabled() -> bool:
    return os.environ.get("EOSDK_SMOKE", "").strip() in ("1", "true", "yes")


@pytest.fixture(autouse=True)
def _require_smoke() -> None:
    if not _smoke_enabled():
        pytest.skip("live smoke disabled (set EOSDK_SMOKE=1)")


# -- the run registry ----------------------------------------------------------


@dataclass
class RunRegistry:
    """What this session created and touched, for teardown and for triage."""

    platform: str = ""
    endpoints: dict[str, str] = field(default_factory=dict)
    #: every access_id the run minted, never pruned — the triage record
    created: list[str] = field(default_factory=list)
    #: access_id -> the label it was created under, if any; entries are dropped
    #: as they are accounted for, so what is left at teardown is the leak set
    keys: dict[str, str | None] = field(default_factory=dict)
    products: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def record_key(self, access_id: str, label: str | None = None) -> None:
        if access_id not in self.keys:
            self.created.append(access_id)
        self.keys.setdefault(access_id, label)

    def record_product(self, product_id: str) -> None:
        if product_id and product_id not in self.products:
            self.products.append(product_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "endpoints": self.endpoints,
            "keys_created": list(self.created),
            "keys_leaked": sorted(self.keys),
            "products_touched": self.products,
            "notes": self.notes,
        }


_REGISTRY = RunRegistry()


def record_products(products: list[Product]) -> None:
    """Note the products a test touched, so a failure can be reproduced."""
    for product in products:
        _REGISTRY.record_product(product.id)


@pytest.fixture(scope="session")
def registry() -> RunRegistry:
    return _REGISTRY


@pytest.fixture(scope="session", autouse=True)
def _run_summary() -> Iterator[None]:
    """Revoke every created key, then write the run summary — always."""
    yield
    if not _smoke_enabled():
        return
    try:
        _revoke_registered_keys()
    finally:
        destination = Path(os.environ.get(SUMMARY_ENV) or DEFAULT_SUMMARY)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(_REGISTRY.as_dict(), indent=2))
        except OSError as exc:  # a missing artifact must not fail the run
            warnings.warn(f"could not write the smoke run summary: {exc}", stacklevel=2)


def _revoke_registered_keys() -> None:
    """Revoke from the registry, not from the on-disk cache.

    The cache is the thing most likely to be gone (it lives in ``tmp_path``),
    which is exactly when a leaked key would go unnoticed.

    The teardown client gets its own throwaway config and cache dirs like every
    other client in this suite: built with the defaults it would write the
    managed platform profile and a token cache into the runner's real
    ``~/.config/eosdk``, which is a side effect a test run has no business
    having.
    """
    if not _REGISTRY.keys:
        return
    username = os.environ.get("EOSDK_SMOKE_USERNAME")
    password = os.environ.get("EOSDK_SMOKE_PASSWORD")
    if not username or not password:
        _REGISTRY.notes.append("keys left unrevoked: no credentials available at teardown")
        return
    try:
        with tempfile.TemporaryDirectory(prefix="eosdk-smoke-teardown-") as scratch:
            root = Path(scratch)
            with Client(
                platform=_platform_root(),
                cwd=root,
                user_config=root / "config.toml",
                token_cache_dir=root / "tokens",
                keys_cache_dir=root / "s3keys",
                discovery_cache_dir=root / "discovery",
            ) as client:
                client.auth.login(username, password)
                for access_id in list(_REGISTRY.keys):
                    try:
                        client.keys.revoke(access_id)
                    except EosdkError as exc:
                        # A key a test revoked itself — `keys.ephemeral()` does,
                        # in its own `finally` — is gone before teardown looks:
                        # the service 404s and the registry entry is settled,
                        # not leaked. Anything else is a real failure to clean up.
                        if not isinstance(exc, AuthError) or exc.status_code != 404:
                            _REGISTRY.notes.append(f"could not revoke {access_id}: {exc}")
                            warnings.warn(
                                f"smoke teardown could not revoke S3 key {access_id}: {exc}",
                                stacklevel=2,
                            )
                            continue
                    del _REGISTRY.keys[access_id]
    except EosdkError as exc:  # cannot even log in: say so loudly, in the artifact
        _REGISTRY.notes.append(f"teardown could not authenticate: {exc}")
        warnings.warn(f"smoke teardown could not authenticate: {exc}", stacklevel=2)


def _platform_root() -> str:
    return os.environ.get("EOSDK_SMOKE_PLATFORM", DEFAULT_PLATFORM)


def _register_keys_on_creation(client: Client, registry: RunRegistry) -> None:
    """Record every key pair the moment the service hands one over.

    Wrapping ``create`` covers every path that mints a key —
    ``get_or_create``, ``ephemeral`` and the downloader's own labeled reuse all
    funnel through it — so nothing can be created without being registered.
    """
    provider = client.keys
    original = provider.create

    def create(label: str | None = None, **kwargs: Any) -> Any:
        credentials = original(label, **kwargs)
        registry.record_key(credentials.access_key, label)
        return credentials

    provider.create = create  # type: ignore[method-assign]


# -- clients -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, registry: RunRegistry) -> Iterator[Client]:
    platform = _platform_root()
    registry.platform = platform
    with Client(
        platform=platform,
        cwd=tmp_path,
        user_config=tmp_path / "unused.toml",
        token_cache_dir=tmp_path / "tokens",
        keys_cache_dir=tmp_path / "s3keys",
        discovery_cache_dir=tmp_path / "discovery",
    ) as c:
        _register_keys_on_creation(c, registry)
        yield c
        # config introspection must not fail an otherwise passing test
        with contextlib.suppress(EosdkError):
            registry.endpoints = {
                name: value.value
                for name, value in c.config.resolved().items()
                if value.value is not None
            }


@pytest.fixture
def logged_in_client(client: Client) -> Client:
    username, password = _credentials()
    client.auth.login(username, password)
    return client


# -- the CLI, as a subprocess --------------------------------------------------


def eo_command() -> list[str]:
    """The installed console script; that is what the wheel actually ships."""
    script = shutil.which("eo") or str(Path(sys.executable).parent / "eo")
    if Path(script).exists():
        return [script]
    # Fallback for an environment where the script was not linked (e.g. a bare
    # `pip install -e .` without scripts); the app object is the same.
    return [sys.executable, "-c", "from eosdk.cli.main import app; app()"]


@pytest.fixture
def cli_session(tmp_path: Path) -> dict[str, str]:
    """A logged-in ``eo`` environment of the suite's own, isolated from $HOME.

    A subprocess cannot be handed the constructor seams the library fixtures
    use, so the only lever is ``XDG_CONFIG_HOME``: every on-disk location the
    SDK owns (config, tokens, S3 keys, discovery cache) hangs off it. Pointing
    it into ``tmp_path`` keeps the run out of the developer's real
    ``~/.config/eosdk`` *and* gives the ``eo`` invocations one session to
    share. Without it they read a token cache nothing in this suite writes, and
    the test passes only on a machine where someone happened to have run
    ``eo auth login`` by hand recently enough.

    Bootstrapping the profile before logging in is the documented order, and
    the order matters: the reverse caches the session under ``default``, then
    the discovery call that same command makes writes the platform profile and
    makes it the default — so the next command looks for a session under a name
    that has none. That defect is pinned in
    ``tests/e2e/test_cli_shell.py::TestExitCodes::test_login_before_the_profile_exists_is_lost``.
    """
    username, password = _credentials()
    config_home = tmp_path / "xdg"
    config_home.mkdir()
    environment = {
        **os.environ,
        "XDG_CONFIG_HOME": str(config_home),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    bootstrap = _run_eo(environment, "config", "init", "--platform", _platform_root())
    assert bootstrap.returncode == 0, bootstrap.stderr
    # --password-stdin: the password must not reach the process table
    login = _run_eo(
        environment,
        "auth",
        "login",
        "--username",
        username,
        "--password-stdin",
        stdin=f"{password}\n",
    )
    assert login.returncode == 0, login.stderr
    return environment


def _run_eo(
    environment: dict[str, str], *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*eo_command(), *args],
        input=stdin,
        env=environment,
        capture_output=True,
        text=True,
        timeout=CLI_SETUP_TIMEOUT,
        check=False,
    )


def _credentials() -> tuple[str, str]:
    """The account the ``smoke_auth`` tier runs as, or a skip."""
    username = os.environ.get("EOSDK_SMOKE_USERNAME")
    password = os.environ.get("EOSDK_SMOKE_PASSWORD")
    if not username or not password:
        pytest.skip("authenticated smoke needs EOSDK_SMOKE_USERNAME/PASSWORD")
    return username, password
