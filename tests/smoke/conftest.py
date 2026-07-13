"""Live smoke tests against a real platform (SPEC §12, plan step 4.2).

Skipped unless EOSDK_SMOKE=1. Anonymous tests need only that; authenticated
tests additionally need EOSDK_SMOKE_USERNAME / EOSDK_SMOKE_PASSWORD (and
optionally EOSDK_SMOKE_PLATFORM or EOSDK_* endpoint pins; the built-in CDSE
defaults are used otherwise). Tests are small and idempotent; ephemeral keys
are revoked on context exit, and labeled keys created during a test are
revoked in fixture teardown (their cache lives in tmp_path, so without
revocation they would leak server-side on every run).
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from eosdk import Client
from eosdk.exceptions import EosdkError, S3KeyLimitReached


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


@pytest.fixture(autouse=True)
def _blank_builtin_defaults() -> None:
    """Shadows the root conftest fixture: smoke tests want the real endpoints."""
    return None


def _revoke_cached_keys(client: Client, keys_dir: Path) -> None:
    """Revoke labeled keys recorded in the test's throwaway cache.

    The label->access_id mapping exists only client-side; once tmp_path is
    gone the key can neither be reused nor recognized as ours, so it must be
    revoked while the record still exists.
    """
    for cache_file in keys_dir.glob("*.json"):
        try:
            entries = dict(json.loads(cache_file.read_text()))
        except (OSError, ValueError):
            continue
        for entry in entries.values():
            access_id = entry.get("access_id")
            if not access_id:
                continue
            try:
                client.keys.revoke(access_id)
            except EosdkError as exc:
                warnings.warn(
                    f"smoke teardown could not revoke S3 key {access_id}: {exc}", stacklevel=2
                )


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
        _revoke_cached_keys(c, tmp_path / "s3keys")


@pytest.fixture
def logged_in_client(client: Client) -> Client:
    username = os.environ.get("EOSDK_SMOKE_USERNAME")
    password = os.environ.get("EOSDK_SMOKE_PASSWORD")
    if not username or not password:
        pytest.skip("authenticated smoke needs EOSDK_SMOKE_USERNAME/PASSWORD")
    client.auth.login(username, password)
    return client
