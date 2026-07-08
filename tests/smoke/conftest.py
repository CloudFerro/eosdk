"""Live smoke tests against a real platform (SPEC §12, plan step 4.2).

Skipped unless EOSDK_SMOKE=1. Anonymous tests need only that; authenticated
tests additionally need EOSDK_SMOKE_USERNAME / EOSDK_SMOKE_PASSWORD (and
optionally EOSDK_SMOKE_PLATFORM or EOSDK_* endpoint pins; the built-in CDSE
defaults are used otherwise). Tests are small and idempotent; ephemeral keys
are revoked on exit.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from eosdk import Client


def _smoke_enabled() -> bool:
    return os.environ.get("EOSDK_SMOKE", "").strip() in ("1", "true", "yes")


@pytest.fixture(autouse=True)
def _require_smoke() -> None:
    if not _smoke_enabled():
        pytest.skip("live smoke disabled (set EOSDK_SMOKE=1)")


@pytest.fixture(autouse=True)
def _blank_builtin_defaults() -> None:
    """Shadows the root conftest fixture: smoke tests want the real endpoints."""
    return None


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    platform = os.environ.get("EOSDK_SMOKE_PLATFORM")
    with Client(
        platform=platform,
        cwd=tmp_path,
        user_config=tmp_path / "unused.toml",
        token_cache_dir=tmp_path / "tokens",
        keys_cache_dir=tmp_path / "s3keys",
        discovery_cache_dir=tmp_path / "discovery",
    ) as c:
        yield c


@pytest.fixture
def logged_in_client(client: Client) -> Client:
    username = os.environ.get("EOSDK_SMOKE_USERNAME")
    password = os.environ.get("EOSDK_SMOKE_PASSWORD")
    if not username or not password:
        pytest.skip("authenticated smoke needs EOSDK_SMOKE_USERNAME/PASSWORD")
    client.auth.login(username, password)
    return client
