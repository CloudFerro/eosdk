"""The SPEC §7.1 script, once per cell of protocol x backend (plan §4.4).

``search -> download -> list -> open`` is the acceptance script the SDK is
specified against. Running it per cell is what catches a *combination* that
breaks while both axes work in isolation — F4 in particular (OData x S3) was
entirely untested: OData derives ``s3_path`` from ``S3Path``, STAC from the
common prefix of its ``s3://`` asset hrefs, and nothing proved the OData chain
produces an address S3 can actually resolve.

Cells differ in what is legitimately available, so the matrix is explicit and an
unsupported step is an *assertion* — never a skip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from eosdk.exceptions import AuthError, UnsupportedCapability

if TYPE_CHECKING:
    from pathlib import Path

    from eosdk.client import Client
    from eosdk.models import Product
    from tests.e2e.platform import FakePlatform

pytestmark = pytest.mark.e2e

#: one file deep inside the SAFE tree, addressed by its logical path
B04 = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"

# A window that selects exactly the one product carrying the full SAFE tree, so
# every cell downloads, lists and opens the same bytes.
BBOX = (22.5, 52.9, 24.0, 53.5)
INTERVAL = "2026-06-15/2026-06-16"


@dataclass(frozen=True)
class Cell:
    id: str
    protocol: str
    via: str
    #: `open` needs RandomAccess; only the s3 backend advertises it today
    open_supported: bool

    @property
    def label(self) -> str:
        return f"{self.id}-{self.protocol}-{self.via}"


MATRIX = [
    Cell("F1", "stac", "http", open_supported=False),
    Cell("F2", "stac", "s3", open_supported=True),
    Cell("F3", "odata", "http", open_supported=False),
    Cell("F4", "odata", "s3", open_supported=True),
]


@pytest.fixture(params=MATRIX, ids=lambda cell: cell.label)
def cell(request: pytest.FixtureRequest) -> Cell:
    return request.param


def find_the_safe_product(client: Client, protocol: str) -> Product:
    results = client.search(
        collection="SENTINEL-2",
        bbox=BBOX,
        datetime=INTERVAL,
        filters={"cloudCover": "<20"},
        protocol=protocol,
        limit=10,
    )
    products = list(results)
    assert len(products) == 1, [product.name for product in products]
    return products[0]


class TestTheAcceptanceScript:
    """F1-F4: the whole script, per cell."""

    def test_search_download_list_open(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path, cell: Cell
    ) -> None:
        source = platform.products[0]

        # -- search ------------------------------------------------------------
        product = find_the_safe_product(library_client, cell.protocol)
        assert product.name == source.name
        assert product.id == source.uuid
        # the two catalogues advertise the S3 root in their own vocabulary; both
        # must address the same object (see PP2 for the byte-level proof)
        assert product.s3_path == (source.s3_uri if cell.protocol == "stac" else source.s3_path)

        # -- download ----------------------------------------------------------
        target = tmp_path / cell.label
        reports = library_client.download(
            product, target=target, via=cell.via, concurrency=4, checksum=True
        )
        assert len(reports) == 1
        report = reports[0]
        assert report.product_id == source.uuid

        if cell.via == "http":
            # one delivered object: the product zip, named as advertised by
            # Content-Disposition, and verified against the catalogue checksum
            assert report.path == target / source.zip_name
            assert report.path.read_bytes() == source.zip_bytes
            assert report.checksum_verified is True
        else:
            # the s3 backend delivers the extracted tree under the product name
            assert report.path == target / source.name
            for logical, content in source.tree.items():
                assert (report.path / logical).read_bytes() == content
            # a product-level checksum covers exactly one delivered object, so a
            # multi-file product legitimately reports "nothing to verify"
            assert report.checksum_verified is None
        assert report.bytes > 0

        # -- list --------------------------------------------------------------
        nodes = library_client.list(product, via=cell.via, recursive=True)
        by_path = {node.path: node for node in nodes}
        assert set(source.tree) <= set(by_path)
        band = by_path[B04]
        assert band.is_dir is False
        assert band.size == len(source.tree[B04])
        assert by_path["GRANULE"].is_dir is True

        # -- open --------------------------------------------------------------
        if not cell.open_supported:
            with pytest.raises(UnsupportedCapability) as exc_info:
                library_client.open(product, path=B04, via=cell.via)
            error = exc_info.value
            assert (error.backend, error.capability) == (cell.via, "open")
            return

        with library_client.open(product, path=B04, via=cell.via) as handle:
            assert handle.read() == source.tree[B04]
        # ranged: a seek must not re-read the whole object
        with library_client.open(product, path=B04, via=cell.via) as handle:
            handle.seek(4)
            assert handle.read(12) == source.tree[B04][4:16]

    def test_keys_are_minted_once_on_first_s3_use(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """F2 — the claim ``tests/test_spec_71_acceptance.py`` makes, kept here.

        The key pair must not be created at construction time, and a second S3
        operation must reuse the labeled one rather than mint another.
        """
        product = find_the_safe_product(library_client, "stac")
        assert platform.call_count("keys", "POST") == 0

        library_client.download(product, target=tmp_path / "d", via="s3")
        assert platform.call_count("keys", "POST") == 1

        library_client.list(product, via="s3", recursive=True)
        with library_client.open(product, path=B04, via="s3") as handle:
            handle.read(8)
        assert platform.call_count("keys", "POST") == 1

    def test_odata_s3_path_resolves_in_s3(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """F4 — the OData extraction chain, isolated.

        ``S3Path`` is an absolute ``/bucket/key`` string while STAC yields an
        ``s3://`` URI; only one of the two chains had ever been exercised
        against a real bucket.
        """
        source = platform.products[0]
        product = library_client.get(source.uuid, protocol="odata")
        assert product.s3_path == source.s3_path
        assert product.s3_path.startswith("/")

        reports = library_client.download(product, target=tmp_path / "f4", via="s3")
        delivered = {
            path.relative_to(reports[0].path).as_posix(): path.read_bytes()
            for path in reports[0].path.rglob("*")
            if path.is_file()
        }
        assert delivered == source.tree


class TestAnonymousBoundary:
    """F5 — where the anonymous surface ends and a session starts."""

    def test_catalogue_is_public_but_download_is_not(
        self, client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        products = list(client.search(collection="SENTINEL-2", datetime=INTERVAL, protocol="stac"))
        assert [product.name for product in products] == [platform.products[0].name]
        assert {collection.id for collection in client.collections()} == {
            "SENTINEL-1",
            "SENTINEL-2",
            "SENTINEL-3",
        }
        assert client.queryables("SENTINEL-2")

        with pytest.raises(AuthError) as exc_info:
            client.download(products[0], target=tmp_path / "anon", via="http")
        error = exc_info.value
        assert error.realm == "eodata"
        assert error.profile == "e2e"
        assert "eo auth login" in str(error)
        # nothing half-written was left behind by the refusal
        assert list((tmp_path / "anon").iterdir()) == []


class TestProductsAreBackendAgnostic:
    """F6 — a normalized Product does not remember which catalogue found it."""

    def test_either_catalogue_feeds_either_backend(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        source = platform.products[0]
        from_stac = find_the_safe_product(library_client, "stac")
        from_odata = find_the_safe_product(library_client, "odata")

        for label, product in (("stac", from_stac), ("odata", from_odata)):
            for via in ("http", "s3"):
                reports = library_client.download(
                    product, target=tmp_path / f"{label}-{via}", via=via
                )
                assert reports[0].bytes > 0
        # the http artifacts are byte-identical whichever catalogue supplied the id
        assert (tmp_path / "stac-http" / source.zip_name).read_bytes() == (
            tmp_path / "odata-http" / source.zip_name
        ).read_bytes()

    def test_download_endpoint_pinned_away_from_the_catalogue(
        self,
        make_client: object,
        platform: FakePlatform,
        tmp_path: Path,
    ) -> None:
        """The catalogue comes from discovery; the transfer endpoint is pinned.

        Per-service precedence (SPEC §6.1) means one field can be pinned while
        its siblings stay discovery-sourced — this asserts a journey survives
        that split, and that the pin really is what the transfer used.
        """
        client = make_client(  # type: ignore[operator]
            endpoints={"eodata_http": platform.urls.eodata_http}
        )
        client.auth.login(platform.username, platform.password)

        resolved = client.config.resolved()
        assert resolved["eodata_http"].source == "kwargs"
        assert resolved["catalogue_stac"].source == "discovery"

        product = find_the_safe_product(client, "stac")
        platform.reset_calls()
        reports = client.download(product, target=tmp_path / "pinned", via="http")
        assert reports[0].path.read_bytes() == platform.products[0].zip_bytes
        assert platform.call_count("eodata_http", "GET") == 1
