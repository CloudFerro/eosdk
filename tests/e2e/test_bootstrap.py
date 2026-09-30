"""Bootstrap journeys: *how* a deployment's endpoints got resolved (plan §4.1).

This file owns the third axis of the suite — endpoint provenance — and asserts
that the same journey (login → search → download → list → open) works, and is
observably sourced from the right layer, for all three:

* **discovery** — a bare ``platform`` root, empty config dir (B1), the snapshot
  it persists (B2) and the documented CLI onboarding that pins it (B3);
* **pinned profile** — every endpoint in the config file, discovery never
  consulted;
* **env override** — every endpoint in ``EOSDK_*``, discovery never consulted.

The last two have no scenario id in the plan but are named as values of the
axis, so they live here with the rest of it.

B4 and B5 cover the two ways a discovery document can *refuse* to serve a call:
a capability the deployment disabled, and an API version this SDK does not
implement. Both must fail before any request reaches the service.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from eosdk.config.loader import _load_config_file
from eosdk.exceptions import EndpointUnreachable, UnsupportedApiVersion, UnsupportedCapability
from eosdk.models import Product
from tests.e2e import platform as fake
from tests.e2e.platform import FakePlatform

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from eosdk.client import Client
    from tests.e2e.conftest import Workspace
    from tests.e2e.surface import Invoke

pytestmark = pytest.mark.e2e

WELL_KNOWN = f"{fake.PLATFORM}{fake.WELL_KNOWN_PATH}"

#: the endpoint fields every journey below touches, and the service each belongs to
JOURNEY_FIELDS = (
    "keycloak",
    "keycloak_realm",
    "keycloak_client_id",
    "catalogue_stac",
    "eodata_http",
    "s3_endpoint",
    "s3_credentials",
)


def run_journey(client: Client, platform: FakePlatform, target: Path) -> dict[str, Any]:
    """login → search → download → list → open, the SPEC §7.1 script.

    Returns the observable results so each provenance test can assert on the
    *same* outcome; a test that only proved "no exception" would pass against a
    client that silently did nothing.
    """
    client.auth.login(platform.username, platform.password)
    products = list(client.search(collection="SENTINEL-2", limit=10))
    first = platform.products[0]
    product = next(p for p in products if p.name == first.name)
    reports = client.download([product], target=target, via="http")
    nodes = client.list(product, via="http", recursive=False)
    with client.open(product, "manifest.safe", via="s3") as handle:
        payload = handle.read()
    return {
        "names": [p.name for p in products],
        "report": reports[0],
        "root_children": sorted(node.name for node in nodes),
        "manifest": payload,
    }


def sources_of(client: Client, fields: Iterable[str] = JOURNEY_FIELDS) -> dict[str, str]:
    resolved = client.config.resolved()
    return {field: resolved[field].source for field in fields}


class TestDiscoveryBootstrap:
    """B1 — ``Client(platform=...)`` with nothing on disk."""

    def test_empty_config_dir_runs_the_whole_journey_from_discovery(
        self,
        platform: FakePlatform,
        make_client: Callable[..., Client],
        make_workspace: Callable[[str], Workspace],
    ) -> None:
        space = make_workspace("b1")
        assert not space.config.exists()  # the premise: zero local configuration
        client = make_client(space=space, platform=platform.urls.platform)

        result = run_journey(client, platform, space.downloads)

        first = platform.products[0]
        assert result["names"] == [
            p.name for p in platform.products if p.collection == "SENTINEL-2"
        ]
        assert result["report"].path.read_bytes() == first.zip_bytes
        assert result["report"].checksum_verified is True
        assert result["root_children"] == ["GRANULE", "MTD_MSIL2A.xml", "manifest.safe"]
        assert result["manifest"] == first.tree["manifest.safe"]

        # Every endpoint the journey used came from the document, and the
        # document was fetched once for the whole journey (resolver memoizes).
        assert set(sources_of(client).values()) == {"discovery"}
        assert platform.call_count("platform") == 1
        assert platform.requests_to("platform")[0].path == fake.WELL_KNOWN_PATH

    def test_calls_landed_on_the_hosts_the_document_advertised(
        self,
        platform: FakePlatform,
        make_client: Callable[..., Client],
        make_workspace: Callable[[str], Workspace],
    ) -> None:
        """A projection bug would resolve fields yet address the wrong host."""
        space = make_workspace("b1-hosts")
        client = make_client(space=space, platform=platform.urls.platform)
        run_journey(client, platform, space.downloads)

        advertised = {
            "keycloak": platform.urls.keycloak,
            "stac": platform.urls.stac,
            "eodata_http": platform.urls.eodata_http,
            "keys": platform.urls.keys,
        }
        for service, base in advertised.items():
            calls = platform.requests_to(service)
            assert calls, f"the journey never reached {service}"
            assert all(call.url.startswith(base) for call in calls), service
        # ...and nothing was addressed to a host the document did not name.
        assert not platform.unrouted


class TestProvenanceWithoutDiscovery:
    """The other two values of the provenance axis: pinned profile, env vars.

    Neither may fetch the platform document at all — a discovery request here
    would mean a locally pinned deployment still depends on being online.
    """

    def test_pinned_profile_runs_the_journey_and_never_fetches_the_document(
        self,
        platform: FakePlatform,
        workspace: Workspace,
        make_client: Callable[..., Client],
    ) -> None:
        workspace.write_pinned_profile(platform.urls)
        client = make_client()

        result = run_journey(client, platform, workspace.downloads)

        first = platform.products[0]
        assert result["report"].path.read_bytes() == first.zip_bytes
        assert result["root_children"] == ["GRANULE", "MTD_MSIL2A.xml", "manifest.safe"]
        assert result["manifest"] == first.tree["manifest.safe"]
        assert platform.call_count("platform") == 0
        assert all(source.startswith("profile:e2e(") for source in sources_of(client).values()), (
            sources_of(client)
        )

    def test_env_vars_run_the_journey_and_never_fetch_the_document(
        self,
        platform: FakePlatform,
        make_client: Callable[..., Client],
        make_workspace: Callable[[str], Workspace],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        space = make_workspace("env")  # no config file at all: env is the only layer
        for name, value in {
            "EOSDK_KEYCLOAK_URL": platform.urls.keycloak,
            "EOSDK_KEYCLOAK_REALM": fake.REALM,
            "EOSDK_KEYCLOAK_CLIENT_ID": fake.CLIENT_ID,
            "EOSDK_CATALOGUE_STAC_URL": platform.urls.stac,
            "EOSDK_CATALOGUE_ODATA_URL": platform.urls.odata,
            "EOSDK_EODATA_HTTP_URL": platform.urls.eodata_http,
            "EOSDK_S3_ENDPOINT": platform.urls.s3_endpoint,
            "EOSDK_S3_REGION": platform.urls.s3_region,
            "EOSDK_S3_CREDENTIALS_URL": platform.urls.keys,
        }.items():
            monkeypatch.setenv(name, value)
        client = make_client(space=space)

        result = run_journey(client, platform, space.downloads)

        first = platform.products[0]
        assert result["report"].path.read_bytes() == first.zip_bytes
        assert result["manifest"] == first.tree["manifest.safe"]
        assert platform.call_count("platform") == 0
        assert sources_of(client) == {
            "keycloak": "env:EOSDK_KEYCLOAK_URL",
            "keycloak_realm": "env:EOSDK_KEYCLOAK_REALM",
            "keycloak_client_id": "env:EOSDK_KEYCLOAK_CLIENT_ID",
            "catalogue_stac": "env:EOSDK_CATALOGUE_STAC_URL",
            "eodata_http": "env:EOSDK_EODATA_HTTP_URL",
            "s3_endpoint": "env:EOSDK_S3_ENDPOINT",
            "s3_credentials": "env:EOSDK_S3_CREDENTIALS_URL",
        }


class TestManagedProfileSnapshot:
    """B2 — the profile discovery writes, and how far it carries a second client."""

    def test_journey_writes_the_platform_named_profile(
        self, platform: FakePlatform, workspace: Workspace, client: Client
    ) -> None:
        run_journey(client, platform, workspace.downloads)

        assert client.discovered_profile == (fake.PLATFORM_NAME, "created")
        profile = _load_config_file(workspace.config).profiles[fake.PLATFORM_NAME]
        assert profile.platform == platform.urls.platform
        assert profile.discovered_from == WELL_KNOWN
        assert profile.description == fake.PLATFORM_DESCRIPTION
        assert profile.catalogue_stac == platform.urls.stac
        assert profile.catalogue_odata == platform.urls.odata
        assert profile.eodata_http == platform.urls.eodata_http
        assert profile.keycloak == platform.urls.keycloak
        assert profile.keycloak_realm == fake.REALM
        assert profile.keycloak_client_id == fake.CLIENT_ID
        assert profile.s3_credentials == platform.urls.keys
        assert profile.s3_endpoint == platform.urls.s3_endpoint
        assert profile.s3_region == platform.urls.s3_region
        # The user's own default_profile is not hijacked by the snapshot.
        assert _load_config_file(workspace.config).default_profile == "e2e"

    def test_second_client_searches_from_the_snapshot_while_discovery_404s(
        self,
        platform: FakePlatform,
        workspace: Workspace,
        client: Client,
        make_client: Callable[..., Client],
        make_workspace: Callable[[str], Workspace],
    ) -> None:
        run_journey(client, platform, workspace.downloads)

        # A *fresh* discovery cache dir, so nothing but the persisted profile can
        # supply the endpoints; the document itself is gone for good.
        cold = make_workspace("b2-cold")
        platform.fail_next("platform", 404, times=99)
        platform.reset_calls()
        second = make_client(
            space=cold,
            user_config=workspace.config,
            profile=fake.PLATFORM_NAME,
        )
        second.auth.login(platform.username, platform.password)
        names = [p.name for p in second.search(collection="SENTINEL-2", limit=10)]

        assert names == [p.name for p in platform.products if p.collection == "SENTINEL-2"]
        # Not "404 tolerated": the document was never even asked for.
        assert platform.call_count("platform") == 0
        assert all(
            source.startswith(f"profile:{fake.PLATFORM_NAME}(")
            for source in sources_of(second, ("keycloak", "catalogue_stac")).values()
        )

    def test_data_access_from_the_snapshot_still_demands_the_document(
        self,
        platform: FakePlatform,
        workspace: Workspace,
        client: Client,
        make_client: Callable[..., Client],
        make_workspace: Callable[[str], Workspace],
    ) -> None:
        """DEVIATION from plan B2 ("the journey must still complete").

        Auth and catalogue complete from the snapshot, but ``download``/``list``
        do not: ``Client._downloader`` asks ``discovery.strategies_for`` and
        ``Client._http_downloader`` asks ``discovery.api_version_for`` whenever
        ``discovery.configured`` — and the snapshot profile *includes* the
        ``platform`` root, so it is configured. With a cold discovery cache and
        an unreachable platform that is an ``EndpointUnreachable`` before any
        transfer starts. Asserted as it behaves, not as the plan wished.
        """
        run_journey(client, platform, workspace.downloads)
        product = next(iter(client.search(collection="SENTINEL-2", limit=1)))

        cold = make_workspace("b2-degraded")
        platform.fail_next("platform", 404, times=99)
        platform.reset_calls()
        second = make_client(space=cold, user_config=workspace.config, profile=fake.PLATFORM_NAME)
        second.auth.login(platform.username, platform.password)

        with pytest.raises(EndpointUnreachable) as exc_info:
            second.download([product], target=cold.downloads, via="http")
        assert exc_info.value.service == "discovery"
        assert exc_info.value.url == WELL_KNOWN
        assert platform.call_count("eodata_http") == 0  # failed before the transfer

    def test_the_snapshot_values_alone_carry_the_full_journey(
        self,
        platform: FakePlatform,
        workspace: Workspace,
        client: Client,
        make_client: Callable[..., Client],
        make_workspace: Callable[[str], Workspace],
    ) -> None:
        """The positive half of B2: the *values* round-trip and are complete.

        Re-pinned without ``platform``/``discovery_url`` — i.e. exactly the
        endpoint set discovery persisted, and nothing else — a second client
        runs the whole journey with the platform document permanently 404ing.
        """
        run_journey(client, platform, workspace.downloads)
        snapshot = _load_config_file(workspace.config).profiles[fake.PLATFORM_NAME]

        pinned = make_workspace("b2-pinned")
        endpoints = {
            field: value
            for field, value in snapshot.model_dump(exclude_none=True).items()
            if field not in {"platform", "discovery_url", "discovered_from", "description"}
        }
        pinned.config.write_text(
            'default_profile = "snapshot"\n[profiles.snapshot]\n'
            + "".join(f'{field} = "{value}"\n' for field, value in sorted(endpoints.items()))
        )
        platform.fail_next("platform", 404, times=99)
        platform.reset_calls()

        second = make_client(space=pinned)
        result = run_journey(second, platform, pinned.downloads)

        first = platform.products[0]
        assert result["report"].path.read_bytes() == first.zip_bytes
        assert result["root_children"] == ["GRANULE", "MTD_MSIL2A.xml", "manifest.safe"]
        assert result["manifest"] == first.tree["manifest.safe"]
        assert platform.call_count("platform") == 0


class TestCliOnboarding:
    """B3 — the documented sequence, end to end, through ``eo``."""

    def test_config_init_then_doctor_then_search_then_download(
        self,
        platform: FakePlatform,
        make_workspace: Callable[[str], Workspace],
        make_cli: Callable[[Workspace], Invoke],
    ) -> None:
        space = make_workspace("b3")
        cli = make_cli(space)

        # `config init` prompts for the platform root unless --platform is given.
        init = cli("config", "init", "--platform", platform.urls.platform)
        assert init.exit_code == 0, init.stderr
        assert fake.PLATFORM_NAME in init.stdout
        config = _load_config_file(space.config)
        assert config.default_profile == fake.PLATFORM_NAME
        assert config.profiles[fake.PLATFORM_NAME].catalogue_stac == platform.urls.stac

        # `doctor` exits 0 here: nothing *failed*. Sections that cannot run
        # without a session (S3 credentials) report ok=None (skipped), and
        # doctor only exits 1 when some check is ok=False (SPEC §6.8).
        doctor = cli("doctor", "--json")
        assert doctor.exit_code == 0, doctor.stdout + doctor.stderr
        sections = {section["section"]: section["results"] for section in json.loads(doctor.stdout)}
        checks = {
            f"{name}:{result['name']}": result
            for name, results in sections.items()
            for result in results
        }
        assert next(r["name"] for r in sections["Config"]) == "profile"
        assert sections["Config"][0]["detail"] == f"using profile '{fake.PLATFORM_NAME}'"
        # doctor reports the discovered endpoints, sourced from the profile
        # `config init` wrote — that is what proves the later commands read it.
        for field, url in (
            ("catalogue_stac", platform.urls.stac),
            ("catalogue_odata", platform.urls.odata),
            ("eodata_http", platform.urls.eodata_http),
            ("s3_credentials", platform.urls.keys),
        ):
            detail = checks[f"Config:{field} URL"]["detail"]
            assert detail == f"{url} (profile:{fake.PLATFORM_NAME}({space.config}))"
        assert checks["Discovery:platform document"]["ok"] is True
        assert "3 services" in checks["Discovery:platform document"]["detail"]
        assert checks["Discovery:catalogue/odata api"]["detail"] == "advertised v1 is supported"
        assert checks["Services:Catalogue (STAC)"]["ok"] is True
        assert checks["Services:EOData (HTTP)"]["ok"] is True
        assert checks["Services:S3 credentials"]["ok"] is None  # skipped: no session yet

        # The download service demands a bearer token, so the documented
        # sequence needs a login before the transfer step.
        login = cli(
            "auth", "login", "--username", platform.username, "--password", platform.password
        )
        assert login.exit_code == 0, login.stderr

        search = cli("search", "--collection", "SENTINEL-2", "--limit", "2", "--format", "json")
        assert search.exit_code == 0, search.stderr
        found = [json.loads(line) for line in search.stdout.splitlines() if line.strip()]
        assert [entry["name"] for entry in found] == [
            p.name for p in platform.products if p.collection == "SENTINEL-2"
        ][:2]

        download = cli(
            "download", "-", "--output", str(space.downloads), "--via", "http", input=search.stdout
        )
        assert download.exit_code == 0, download.stderr
        for entry in found:
            product = platform.product(entry["id"])
            assert product is not None
            assert (space.downloads / product.zip_name).read_bytes() == product.zip_bytes

        # One fetch for the whole onboarding: `config init` pulled the document,
        # `doctor` re-served it from the shared TTL cache, and every later
        # command read the pinned profile instead.
        assert platform.call_count("platform") == 1


class TestDegradedDeployment:
    """B4 — the deployment restricts what the http backend may do."""

    def test_capability_restricted_to_download_kills_list_but_not_download(
        self, platform: FakePlatform, client: Client, workspace: Workspace
    ) -> None:
        """The plan's intent for B4, on a real call path through ``Client``.

        Mutating the document before the client's first request matters: the
        resolver memoizes the parsed document on first fetch.
        """
        platform.set_http_capabilities("odata", ["download"])
        client.auth.login(platform.username, platform.password)
        product = next(iter(client.search(collection="SENTINEL-2", limit=1)))

        with pytest.raises(UnsupportedCapability) as exc_info:
            client.list(product, via="http")
        assert exc_info.value.backend == "http"
        assert exc_info.value.capability == "list"
        # No strategy of this deployment offers list, not even an unavailable one.
        assert exc_info.value.alternative is None
        assert platform.call_count("eodata_http", path="/Nodes") == 0

        reports = client.download([product], target=workspace.downloads, via="http")
        first = platform.products[0]
        assert reports[0].path.read_bytes() == first.zip_bytes
        assert reports[0].checksum_verified is True

    def test_dropping_odata_selects_deprecated_resto_which_is_unimplemented(
        self, platform: FakePlatform, client: Client, workspace: Workspace
    ) -> None:
        """DEVIATION from plan B4 ("download still works").

        With ``odata`` absent, selection does fall back to ``resto`` and warns
        about the sunset — but ``HttpDownloader.__init__`` refuses to construct
        any strategy other than ``odata`` (``NotImplementedError``, see
        ``src/eosdk/eodata/http.py``), so the download cannot complete. That is
        the SDK's real behaviour and is already pinned by
        ``tests/discovery/test_client_integration.py``; asserted here on the
        public ``Client.download`` path rather than on ``_downloader``.
        """
        platform.drop_http_strategy("odata")
        client.auth.login(platform.username, platform.password)
        product = Product(id=platform.products[0].uuid, name=platform.products[0].name)

        with (
            pytest.warns(DeprecationWarning, match="sunset 2027-01-01") as warnings_,
            pytest.raises(NotImplementedError, match="resto"),
        ):
            client.download([product], target=workspace.downloads, via="http")
        assert "replacement: 'odata'" in str(warnings_[0].message)
        assert platform.call_count("eodata_http") == 0
        assert not list(workspace.downloads.iterdir())

    def test_dropping_odata_also_removes_list(self, platform: FakePlatform, client: Client) -> None:
        """The half of B4 the plan gets right: ``list`` names what would provide it."""
        platform.drop_http_strategy("odata")
        client.auth.login(platform.username, platform.password)
        product = Product(id=platform.products[0].uuid, name=platform.products[0].name)

        with pytest.raises(UnsupportedCapability) as exc_info:
            client.list(product, via="http")
        assert exc_info.value.capability == "list"
        assert exc_info.value.alternative is not None
        assert "'odata'" in exc_info.value.alternative
        assert platform.call_count("eodata_http") == 0


class TestApiVersionGuard:
    """B5 — an advertised version this SDK does not implement stops the call dead."""

    def test_catalogue_odata_v3_fails_before_any_product_request(
        self, platform: FakePlatform, client: Client
    ) -> None:
        platform.set_api_version("catalogue/odata", "v3")

        with pytest.raises(UnsupportedApiVersion) as exc_info:
            client.search(collection="SENTINEL-2", limit=2, protocol="odata")
        assert exc_info.value.service == "catalogue/odata"
        assert exc_info.value.advertised == "v3"
        assert exc_info.value.supported == "v1"
        assert "EOSDK_CATALOGUE_ODATA_URL" in str(exc_info.value)
        assert platform.call_count("odata") == 0

    def test_data_access_http_v3_fails_before_any_download_request(
        self, platform: FakePlatform, client: Client, workspace: Workspace
    ) -> None:
        platform.set_api_version("data_access/http/odata", "v3")
        client.auth.login(platform.username, platform.password)
        product = Product(id=platform.products[0].uuid, name=platform.products[0].name)

        with pytest.raises(UnsupportedApiVersion) as exc_info:
            client.download([product], target=workspace.downloads, via="http")
        assert exc_info.value.service == "data_access/http/odata"
        assert "EOSDK_EODATA_HTTP_URL" in str(exc_info.value)
        assert platform.call_count("eodata_http") == 0
        assert not list(workspace.downloads.iterdir())

    def test_the_stac_catalogue_is_unaffected_by_an_odata_version_bump(
        self, platform: FakePlatform, client: Client
    ) -> None:
        """The guard is per service key, not a global kill switch."""
        platform.set_api_version("catalogue/odata", "v3")
        names = [p.name for p in client.search(collection="SENTINEL-2", limit=10)]
        assert names == [p.name for p in platform.products if p.collection == "SENTINEL-2"]
