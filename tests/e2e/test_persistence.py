"""What survives a process boundary (plan §4.7, S1-S5).

Four caches live on disk — tokens, S3 key secrets, the discovery document and
readiness verdicts — all profile-keyed and all designed for reuse across runs.
Every one of them is unit-tested in isolation; none of them had a test that
built a *second* client against the same directories and checked the second run
was quiet, which is the entire property they exist for.

The clocks here are injected rather than frozen: ``KeycloakAuth`` and
``DiscoveryCache`` both capture ``time.time`` as a keyword default at import
time, so ``freezegun`` never reaches them. Each object exposes the callable as
an instance attribute, which is the seam the tests use.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import pytest

from eosdk.discovery.cache import DEFAULT_TTL
from eosdk.exceptions import AuthError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from eosdk.client import Client
    from tests.e2e.conftest import Workspace
    from tests.e2e.platform import FakePlatform
    from tests.e2e.surface import Invoke

pytestmark = pytest.mark.e2e

JUNE = "2026-06-01/2026-06-30"


def journey(client: Client, platform: FakePlatform, target: Path) -> list[Any]:
    """search → download(http) → list → an s3 transfer that needs a key pair."""
    products = list(client.search(collection="SENTINEL-2", datetime="2026-06-15/2026-06-16"))
    assert len(products) == 1
    client.download(products, target=target, via="http")
    client.list(products[0], via="http", recursive=True)
    client.download(products, target=target / "s3", via="s3")
    return products


class TestSecondRunIsQuiet:
    """S1 — the reason all four caches exist."""

    def test_nothing_is_refetched_on_a_second_client(
        self,
        make_client: Callable[..., Client],
        platform: FakePlatform,
        tmp_path: Path,
    ) -> None:
        first = make_client()
        first.auth.login(platform.username, platform.password)
        journey(first, platform, tmp_path / "run1")
        first.close()

        assert platform.call_count("keys", "POST") == 1
        platform.reset_calls()

        second = make_client()
        journey(second, platform, tmp_path / "run2")

        # the platform document came off disk, the bearer came off disk, and the
        # labeled key was reused rather than minted again
        assert platform.call_count("platform") == 0
        assert platform.call_count("keycloak") == 0
        assert platform.call_count("keys", "POST") == 0
        # the labeled-reuse policy still *lists* keys: the local store records a
        # label→access_id mapping, but only the service knows whether that key
        # is still live (src/eosdk/auth/s3_keys.py::get_or_create)
        assert platform.call_count("keys", "GET") >= 1

    def test_a_different_cache_dir_starts_from_scratch(
        self,
        make_client: Callable[..., Client],
        make_workspace: Callable[[str], Workspace],
        platform: FakePlatform,
        tmp_path: Path,
    ) -> None:
        """The control for S1: quiet only because the caches were shared."""
        first = make_client()
        first.auth.login(platform.username, platform.password)
        journey(first, platform, tmp_path / "run1")
        first.close()

        elsewhere = make_workspace("fresh")
        elsewhere.write_platform_profile(platform.urls.platform)
        platform.reset_calls()

        second = make_client(space=elsewhere)
        second.auth.login(platform.username, platform.password)
        journey(second, platform, tmp_path / "run2")

        assert platform.call_count("platform") == 1
        assert platform.call_count("keycloak", "POST") >= 1
        assert platform.call_count("keys", "POST") == 1


class TestRefreshCoordination:
    """S2 — two clients, one token cache."""

    def test_sequential_refresh_costs_one_token_request(
        self, make_client: Callable[..., Client], platform: FakePlatform
    ) -> None:
        clock = {"now": 2_000_000.0}
        platform.token_ttl = 300.0

        first = make_client()
        second = make_client()
        for client in (first, second):
            client.auth._now = lambda: clock["now"]
        first.auth.login(platform.username, platform.password)

        clock["now"] += 290.0  # inside the 30 s leeway: both consider it expired
        platform.reset_calls()

        first_token = first.auth.access_token()
        assert platform.call_count("keycloak", "POST", path="/token") == 1
        second_token = second.auth.access_token()

        # the second client found the refreshed token in the shared cache
        assert platform.call_count("keycloak", "POST", path="/token") == 1
        assert second_token == first_token

    def test_concurrent_refresh_never_exceeds_one_request_per_client(
        self, make_client: Callable[..., Client], platform: FakePlatform
    ) -> None:
        """Honest bound: there is no cross-process lock.

        ``KeycloakAuth`` serializes refreshes per instance only, so two clients
        racing can each issue one. The plan's "exactly one token request" is not
        a property the current implementation has; what it does guarantee is
        that both callers end up with a usable bearer and neither crashes.
        """
        clock = {"now": 3_000_000.0}
        platform.token_ttl = 300.0
        clients = [make_client() for _ in range(2)]
        for client in clients:
            client.auth._now = lambda: clock["now"]
        clients[0].auth.login(platform.username, platform.password)

        clock["now"] += 290.0
        platform.reset_calls()

        results: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(clients))

        def refresh(client: Client) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(client.auth.access_token())
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=refresh, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert len(results) == len(clients)
        assert all(token in platform.access_tokens for token in results)
        assert 1 <= platform.call_count("keycloak", "POST", path="/token") <= len(clients)


class TestSessionLifecycleAcrossInvocations:
    """S3 — login, use, inspect, logout, inspect, as five separate `eo` runs."""

    def test_five_invocations_share_one_token_cache(
        self, cli: Invoke, platform: FakePlatform, workspace: Workspace
    ) -> None:
        token_file = workspace.tokens / "e2e.json"
        assert not token_file.exists()

        login = cli(
            "auth", "login", "--username", platform.username, "--password", platform.password
        )
        assert login.exit_code == 0, login.stderr
        assert token_file.exists()

        search = cli(
            "search", "--collection", "SENTINEL-2", "--from", "2026-06-01", "--to", "2026-06-30"
        )
        assert search.exit_code == 0, search.stderr

        status = cli("auth", "status")
        assert status.exit_code == 0
        assert "yes" in status.stdout

        logout = cli("auth", "logout")
        assert logout.exit_code == 0
        assert not token_file.exists()

        after = cli("auth", "status")
        assert after.exit_code == 1
        assert "not logged in" in after.stderr

    def test_a_bad_password_never_writes_a_session(self, cli: Invoke, workspace: Workspace) -> None:
        result = cli("auth", "login", "--username", "alice", "--password", "wrong")
        assert result.exit_code == 1
        assert "invalid_grant" in result.stderr
        assert not (workspace.tokens / "e2e.json").exists()


class TestCacheInvalidation:
    """S4 — TTL expiry, explicit refresh, and resync of the managed profile."""

    def test_expired_ttl_refetches_exactly_once(
        self, make_client: Callable[..., Client], platform: FakePlatform
    ) -> None:
        first = make_client()
        first.discovery.document()
        assert platform.call_count("platform") == 1
        first.close()

        second = make_client()
        # a client whose cache clock has moved past the TTL sees a stale entry
        second.discovery._cache._now = lambda: __import__("time").time() + DEFAULT_TTL + 60
        second.discovery.document()
        assert platform.call_count("platform") == 2
        second.discovery.document()  # memoized in-process
        assert platform.call_count("platform") == 2

    def test_refresh_busts_a_fresh_cache(self, client: Client, platform: FakePlatform) -> None:
        client.discovery.document()
        assert platform.call_count("platform") == 1
        client.discovery.document()
        assert platform.call_count("platform") == 1
        client.discovery.refresh()
        assert platform.call_count("platform") == 2

    def test_changed_document_resyncs_the_managed_profile(
        self, client: Client, platform: FakePlatform, workspace: Workspace
    ) -> None:
        from eosdk.config.loader import _load_config_file

        client.discovery.document()
        assert client.discovered_profile == ("example-eu", "created")
        assert (
            _load_config_file(workspace.config).profiles["example-eu"].s3_credentials
            == platform.urls.keys
        )

        moved = "https://keys-v2.example.eu/api"
        platform.discovery_document["services"]["data_access"]["s3"]["credentials"]["url"] = moved
        client.discovery.refresh()

        assert client.discovered_profile == ("example-eu", "updated")
        assert _load_config_file(workspace.config).profiles["example-eu"].s3_credentials == moved

    def test_an_unchanged_document_does_not_rewrite_the_profile(
        self, client: Client, workspace: Workspace
    ) -> None:
        client.discovery.document()
        before = workspace.config.read_text()
        client.discovery.refresh()
        assert client.discovered_profile == ("example-eu", "unchanged")
        assert workspace.config.read_text() == before


class TestCorruptCachesDegradeGracefully:
    """S5 — a cache must never become a hard dependency."""

    @pytest.mark.parametrize(
        "junk", ["", "{", '{"fetched_at": "yesterday"}', "not json at all"], ids=range(4)
    )
    def test_a_corrupt_discovery_cache_refetches(
        self,
        make_client: Callable[..., Client],
        platform: FakePlatform,
        workspace: Workspace,
        tmp_path: Path,
        junk: str,
    ) -> None:
        first = make_client()
        first.discovery.document()
        first.close()
        cached = next(iter(workspace.discovery.glob("*.json")))
        cached.write_text(junk)
        platform.reset_calls()

        second = make_client()
        second.auth.login(platform.username, platform.password)
        journey(second, platform, tmp_path / "after")
        assert platform.call_count("platform") == 1  # refetched, journey unaffected

    def test_a_corrupt_keys_cache_mints_a_new_pair(
        self,
        make_client: Callable[..., Client],
        platform: FakePlatform,
        workspace: Workspace,
        tmp_path: Path,
    ) -> None:
        first = make_client()
        first.auth.login(platform.username, platform.password)
        journey(first, platform, tmp_path / "run1")
        first.close()
        (workspace.keys / "e2e.json").write_text("{ truncated")
        platform.reset_calls()

        second = make_client()
        journey(second, platform, tmp_path / "run2")
        assert platform.call_count("keys", "POST") == 1

    def test_a_corrupt_readiness_cache_probes_again(
        self, cli: Invoke, platform: FakePlatform, workspace: Workspace
    ) -> None:
        assert cli("doctor", "--json").exit_code == 0
        probes = platform.call_count("eodata_http", "GET", path="/ready")
        assert probes == 1
        for verdict in workspace.readiness.glob("*.json"):
            verdict.write_text("]not json[")

        assert cli("doctor", "--json").exit_code == 0
        assert platform.call_count("eodata_http", "GET", path="/ready") == probes + 1

    def test_a_corrupt_token_cache_asks_for_a_login_and_recovers(
        self,
        make_client: Callable[..., Client],
        platform: FakePlatform,
        workspace: Workspace,
        tmp_path: Path,
    ) -> None:
        """A session cannot be "refetched" — it can only be re-established.

        The other three caches degrade to a fetch; this one degrades to the
        documented AuthError, which is the honest outcome and still not a crash.
        """
        first = make_client()
        first.auth.login(platform.username, platform.password)
        first.close()
        (workspace.tokens / "e2e.json").write_text("\x00\x01garbage")

        second = make_client()
        products = list(second.search(collection="SENTINEL-2", datetime="2026-06-15/2026-06-16"))
        with pytest.raises(AuthError, match="eo auth login"):
            second.download(products, target=tmp_path / "denied", via="http")

        second.auth.login(platform.username, platform.password)
        assert json.loads((workspace.tokens / "e2e.json").read_text())["access_token"]
        reports = second.download(products, target=tmp_path / "recovered", via="http")
        assert reports[0].path.read_bytes() == platform.products[0].zip_bytes
