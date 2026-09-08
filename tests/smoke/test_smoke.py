"""Small, idempotent end-to-end checks against the live platform.

Tiered by cost so CI can run the cheap half on every pipeline and the rest
nightly (plan §5):

``smoke``       fast, read-only, anonymous — no credentials needed
``smoke_auth``  needs ``EOSDK_SMOKE_USERNAME`` / ``EOSDK_SMOKE_PASSWORD``
``slow``        transfers real bytes, or spends a key-manager slot

A test carries every tier that applies, so ``-m "smoke and not slow"`` is a
meaningful selection.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.smoke.conftest import eo_command, record_products, skip_on_key_quota

if TYPE_CHECKING:
    from eosdk import Client

BBOX = (22.5, 52.9, 24.0, 53.5)
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "cli"


@pytest.mark.smoke
class TestAnonymousCatalogue:
    """Read-only, no account: what every pipeline can afford to run."""

    def test_stac_search(self, client: Client) -> None:
        products = list(
            client.search(collection="sentinel-2-l2a", bbox=BBOX, limit=2, protocol="stac")
        )
        assert products
        record_products(products)
        product = products[0]
        assert product.id
        assert product.name
        assert product.s3_path
        assert product.checksum is not None

    def test_odata_search_with_attribute_filter(self, client: Client) -> None:
        results = client.search(
            collection="SENTINEL-2",
            datetime="2026-06-01/2026-06-05",  # bounded: unbounded counts are slow on CDSE
            filters={"cloudCover": "<10", "productType": "S2MSI2A"},
            limit=2,
            protocol="odata",
        )
        products = list(results)
        # A live catalogue owes no guarantee that a fixed five-day window holds
        # two products under 10% cloud: assert the filter worked and returned
        # something, not an exact count that a quiet week would turn red.
        assert products
        assert len(products) <= 2  # the limit is honoured
        assert len(results) >= len(products)  # @odata.count
        for product in products:
            assert (product.cloud_cover or 0.0) < 10
        record_products(products)

    def test_stac_and_odata_agree_on_a_product(self, client: Client) -> None:
        """Cross-protocol identity, past the name-prefix check it used to make.

        The hermetic version of this lives in
        ``tests/e2e/test_parity_protocols.py``; here it guards the *live*
        vocabularies, where the two backends really are different services.
        """
        (stac_product,) = list(
            client.search(collection="sentinel-2-l2a", bbox=BBOX, limit=1, protocol="stac")
        )
        odata_product = client._odata_catalogue().get(stac_product.id)
        record_products([stac_product, odata_product])

        assert odata_product.name.startswith(stac_product.name[:60])
        assert odata_product.id == stac_product.id
        # both must address the same object, in their own vocabularies
        assert stac_product.s3_path is not None
        assert odata_product.s3_path is not None
        assert _s3_key(stac_product.s3_path) == _s3_key(odata_product.s3_path)
        # size and acquisition time are protocol-independent facts
        assert odata_product.datetime == stac_product.datetime
        if stac_product.size is not None and odata_product.size is not None:
            assert stac_product.size == odata_product.size
        # checksums: same algorithm and digest, or the SDK's `checksum=True`
        # would mean different things per protocol
        if stac_product.checksum and odata_product.checksum:
            assert stac_product.checksum == odata_product.checksum

    def test_queryables_for_two_collections(self, client: Client) -> None:
        for collection in ("sentinel-2-l2a", "sentinel-1-grd"):
            queryables = client.queryables(collection)
            names = {queryable.name for queryable in queryables}
            assert names, f"{collection} advertises no queryables"
            # whatever is advertised must be usable as a filter key for the
            # same protocol — that round trip is the documented contract
            assert any(name in names for name in ("eo:cloud_cover", "datetime", "platform"))

    def test_doctor_reports_catalogues_reachable(self, client: Client) -> None:
        from eosdk.doctor import run_doctor

        sections = {s.name: s for s in run_doctor(client)}
        services = {r.name: r for r in sections["Services"].results}
        assert services["Catalogue (STAC)"].ok is True
        assert services["Catalogue (OData)"].ok is True


@pytest.mark.smoke
class TestAnonymousBootstrap:
    """The Phase-3 headline path, against the real discovery document."""

    def test_platform_document_resolves_every_endpoint(self, client: Client) -> None:
        document = client.discovery.document()
        assert document.version.startswith("1.")
        assert "catalogue" in document.services

        endpoints = client.discovery.endpoints()
        for field in ("catalogue_stac", "catalogue_odata", "keycloak", "s3_endpoint"):
            assert endpoints.get(field), f"{field} was not projected from discovery"

        # and the projection is what the client actually uses
        assert (
            client._endpoint("catalogue_stac", service="catalogue_stac")
            == endpoints["catalogue_stac"]
        )

    def test_doctor_json_matches_the_committed_schema(self, client: Client) -> None:
        schema_path = SCHEMAS / "doctor.schema.json"
        if not schema_path.is_file():
            pytest.skip(f"no committed schema at {schema_path}")
        jsonschema = pytest.importorskip("jsonschema")

        from eosdk.doctor import run_doctor

        payload = [
            {
                "section": section.name,
                "results": [
                    {"name": r.name, "ok": r.ok, "detail": r.detail, "hint": r.hint}
                    for r in section.results
                ],
            }
            for section in run_doctor(client)
        ]
        jsonschema.Draft202012Validator(json.loads(schema_path.read_text())).validate(payload)


@pytest.mark.smoke_auth
class TestAuthenticated:
    def test_login_and_status(self, logged_in_client: Client) -> None:
        assert logged_in_client.auth.status().logged_in

    def test_ephemeral_key_lifecycle(self, logged_in_client: Client) -> None:
        with skip_on_key_quota(), logged_in_client.keys.ephemeral() as credentials:
            assert credentials.access_key
            assert credentials.require_secret()
            active = {c.access_key for c in logged_in_client.keys.list()}
            assert credentials.access_key in active
        active_after = {c.access_key for c in logged_in_client.keys.list()}
        assert credentials.access_key not in active_after  # revoked on exit

    def test_http_list_product_root(self, logged_in_client: Client) -> None:
        (product,) = list(
            logged_in_client.search(
                collection="sentinel-2-l2a", bbox=BBOX, limit=1, protocol="stac"
            )
        )
        record_products([product])
        nodes = logged_in_client.list(product, via="http")
        assert nodes
        assert any(n.is_dir for n in nodes) or any(n.name.endswith(".xml") for n in nodes)

    @pytest.mark.slow
    def test_s3_open_ranged_read(self, logged_in_client: Client, tmp_path: Path) -> None:
        (product,) = list(
            logged_in_client.search(
                collection="sentinel-2-l2a", bbox=BBOX, limit=1, protocol="stac"
            )
        )
        record_products([product])
        with skip_on_key_quota():
            nodes = logged_in_client.list(product, via="s3", recursive=True)
        target = next(n for n in nodes if not n.is_dir and n.name.endswith(".xml"))
        with logged_in_client.open(product, path=target.path, via="s3") as fh:
            head = fh.read(256)
        assert head  # ranged read, no full-product transfer

    @pytest.mark.slow
    def test_odata_journey_downloads_the_smallest_product(
        self, logged_in_client: Client, tmp_path: Path
    ) -> None:
        """search → get → download, on the protocol the suite never exercised.

        The smallest match keeps the byte cost of a nightly run bounded; a
        collection with genuinely large products would otherwise make this the
        most expensive test in the repository.
        """
        products = list(
            logged_in_client.search(
                collection="SENTINEL-2",
                datetime="2026-06-01/2026-06-05",
                filters={"productType": "S2MSI2A"},
                limit=20,
                protocol="odata",
            )
        )
        sized = [p for p in products if p.size]
        if not sized:
            pytest.skip("the OData backend reported no product sizes to choose from")
        smallest = min(sized, key=lambda product: product.size or 0)
        record_products([smallest])

        fetched = logged_in_client.get(smallest.id, protocol="odata")
        assert fetched.id == smallest.id
        assert fetched.name == smallest.name

        reports = logged_in_client.download(fetched, target=tmp_path / "odata", via="http")
        assert reports[0].path.is_file()
        assert reports[0].bytes > 0
        if fetched.checksum is not None:
            assert reports[0].checksum_verified is True


@pytest.mark.smoke_auth
@pytest.mark.slow
class TestCliPipeline:
    """The documented composition, run for real once per nightly."""

    def test_search_piped_into_download(self, cli_session: dict[str, str], tmp_path: Path) -> None:
        """The session comes from ``cli_session``, not from a library fixture.

        The two processes below have to find the same tokens on disk, and a
        library client's ``token_cache_dir`` is invisible to them; ``eo`` reads
        whatever ``XDG_CONFIG_HOME`` points at, which is what the fixture sets.
        """
        target = tmp_path / "piped"
        search = subprocess.Popen(
            [
                *eo_command(),
                "search",
                "--collection",
                "sentinel-2-l2a",
                "--limit",
                "1",
                "--format",
                "json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=cli_session,
        )
        assert search.stdout is not None
        download = subprocess.Popen(
            [*eo_command(), "download", "-", "-o", str(target), "--via", "http", "--quiet"],
            stdin=search.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=cli_session,
        )
        search.stdout.close()
        _, err = download.communicate(timeout=900)
        search.wait(timeout=900)

        assert download.returncode == 0, err.decode()
        assert any(path.is_file() for path in target.rglob("*"))


def _s3_key(s3_path: str) -> str:
    """``s3://bucket/key`` and ``/bucket/key`` reduced to a comparable form."""
    return s3_path.removeprefix("s3://").lstrip("/").rstrip("/")
