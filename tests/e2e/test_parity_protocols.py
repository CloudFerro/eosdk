"""stac and odata must describe the same products — plan section 4.5, PP1..PP4.

Every test here is an *agreement* test: one :class:`~tests.e2e.platform.FakeProduct`
is reached through both catalogues and the two normalizations are compared. The
harness projects a single source object into a STAC item and an OData entry
independently, so a difference found here is a normalization bug in the SDK, not
a fixture that drifted.

The four claims, in order:

* PP1 — ``get(id)`` returns the same ``Product`` on both protocols, compared
  field by field rather than by the name prefix the live smoke test checks.
* PP2 — the two ``s3_path`` extraction chains (STAC asset hrefs /
  ``file:local_path`` vs OData ``S3Path``) address the same objects, even though
  they deliberately spell the address differently.
* PP3 — the two checksum vocabularies (STAC ``file:checksum`` multihash vs OData
  ``Checksum[]``) decode to the same expectation, and that expectation really
  describes the delivered bytes.
* PP4 — the same logical query translates to filters that select the same
  products on both backends.
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import TYPE_CHECKING, Any

import pytest

from eosdk.exceptions import DownloadError
from eosdk.models import Checksum
from tests.e2e.platform import FakePlatform, FakeProduct

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from eosdk.client import Client
    from eosdk.models import Product
    from tests.e2e.conftest import Workspace

pytestmark = pytest.mark.e2e

PROTOCOLS = ("stac", "odata")

#: ``Product`` fields that are legitimately protocol-specific and therefore
#: excluded from the PP1 comparison: ``raw`` is the untouched backend document
#: by definition, ``s3_path`` is spelled differently on purpose (PP2 proves the
#: two spellings resolve to the same objects).
_PER_PROTOCOL_FIELDS = frozenset({"raw", "s3_path"})


def _comparable(product: Product) -> dict[str, Any]:
    """Everything ``Product`` carries except the per-protocol fields.

    Built from ``model_dump()`` instead of a hand-picked tuple so that a field
    added to ``Product`` later is compared automatically — a hand-written list
    is exactly how the live smoke check ended up asserting a name prefix only.
    """
    return {
        name: value
        for name, value in product.model_dump().items()
        if name not in _PER_PROTOCOL_FIELDS
    }


def _tree_of(root: Path) -> dict[str, bytes]:
    """Logical path -> bytes for everything under ``root``."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@contextlib.contextmanager
def _logged_in_client(space: Workspace, instance: FakePlatform) -> Iterator[Client]:
    """A ``Client`` on ``space`` bootstrapped from ``instance``'s discovery document.

    ``make_client`` is bound to the default ``platform`` fixture, so the tests
    that need a *custom* product set build their client here instead of pulling
    a second respx/moto stack in through ``platform``.
    """
    from eosdk.client import Client

    space.write_platform_profile(instance.urls.platform)
    client = Client(
        cwd=space.root,
        user_config=space.config,
        token_cache_dir=space.tokens,
        keys_cache_dir=space.keys,
        discovery_cache_dir=space.discovery,
        readiness_cache_dir=space.readiness,
    )
    try:
        client.auth.login(instance.username, instance.password)
        yield client
    finally:
        client.close()


class TestProductIdentity:
    """PP1 — ``get(id)`` agrees on both protocols, for every default product."""

    def test_get_returns_the_same_product_on_both_protocols(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        for fake in platform.products:
            platform.reset_calls()
            stac = library_client.get(fake.uuid, protocol="stac")
            odata = library_client.get(fake.uuid, protocol="odata")

            # Both catalogues were actually consulted: without this the whole
            # comparison could pass by accidentally answering from one backend.
            assert platform.call_count("stac") >= 1, "the stac get never reached the stac service"
            assert platform.call_count("odata") >= 1, "the odata get never reached odata"

            view = _comparable(stac)
            unset = sorted(name for name, value in view.items() if value is None)
            assert unset == [], f"{fake.name}: nothing to compare for {unset}"
            assert view == _comparable(odata), f"{fake.name}: protocols disagree"

    def test_the_agreed_product_is_the_one_the_platform_describes(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        """The agreement is with the source object, not merely with each other."""
        for fake in platform.products:
            for protocol in PROTOCOLS:
                product = library_client.get(fake.uuid, protocol=protocol)
                assert product.id == fake.uuid
                assert product.name == fake.name
                assert product.collection == fake.collection
                assert product.size == fake.size
                assert product.cloud_cover == fake.cloud_cover
                assert product.geometry == fake.geometry
                assert product.datetime is not None
                assert product.datetime.isoformat() == fake.instant.isoformat()


class TestS3PathParity:
    """PP2 — two spellings of the S3 address, one set of objects."""

    def test_the_two_vocabularies_differ_by_design(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        for fake in platform.products:
            stac = library_client.get(fake.uuid, protocol="stac")
            odata = library_client.get(fake.uuid, protocol="odata")
            # STAC collapses the s3:// asset hrefs to their common root; OData
            # reports the absolute /bucket/key form. Different strings, one object.
            assert stac.s3_path == fake.s3_uri
            assert odata.s3_path == fake.s3_path
            assert stac.s3_path != odata.s3_path

    @pytest.mark.parametrize("index", [0, 4], ids=["branching-safe-tree", "single-object"])
    def test_both_addresses_download_identical_trees(
        self,
        library_client: Client,
        platform: FakePlatform,
        tmp_path: Path,
        index: int,
    ) -> None:
        fake = platform.products[index]
        trees = {}
        for protocol in PROTOCOLS:
            product = library_client.get(fake.uuid, protocol=protocol)
            target = tmp_path / f"via-{protocol}"
            # checksum=False on purpose: the product-level checksum describes the
            # `$value` archive, while S3 serves the *extracted* objects. PP3
            # verifies that checksum over the protocol that delivers the archive.
            reports = library_client.download([product], target, via="s3", checksum=False)
            assert [report.path for report in reports] == [target / fake.name]
            trees[protocol] = _tree_of(target / fake.name)

        # Compared against the source tree first, so two empty directories
        # cannot pass as "identical".
        assert trees["stac"] == fake.tree
        assert trees["odata"] == fake.tree

    def test_the_stac_local_path_chain_agrees_too(
        self,
        make_platform: Callable[..., FakePlatform],
        make_workspace: Callable[[str], Workspace],
        tmp_path: Path,
    ) -> None:
        """The second STAC extraction chain: ``file:local_path`` instead of hrefs.

        A deployment that publishes no ``s3://`` asset hrefs falls back to the
        Product asset's ``file:local_path`` (``catalogue/stac.py::_product_s3_path``).
        There the two chains converge on the *same* string — which is itself the
        assertion: the fallback must not invent a different address.
        """
        fake = FakeProduct(
            uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            name="S2B_MSIL2A_20260701T095029_N0511_R079_T34UEE_20260701T105512.SAFE",
            datetime="2026-07-01T09:50:29Z",
            prefix="Sentinel-2/MSI/L2A/2026/07/01/local-path-product",
            stac_s3_style="local_path",
        )
        instance = make_platform(products=[fake])
        space = make_workspace("local-path")
        with _logged_in_client(space, instance) as client:
            stac = client.get(fake.uuid, protocol="stac")
            odata = client.get(fake.uuid, protocol="odata")
            assert stac.s3_path == fake.s3_path  # the /bucket/key form, not s3://
            assert stac.s3_path == odata.s3_path

            for protocol, product in (("stac", stac), ("odata", odata)):
                target = tmp_path / f"local-path-{protocol}"
                client.download([product], target, via="s3", checksum=False)
                assert _tree_of(target / fake.name) == fake.tree


class TestChecksumParity:
    """PP3 — one expectation behind two checksum vocabularies."""

    def test_multihash_and_checksum_array_decode_to_the_same_value(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        for fake in platform.products:
            stac = library_client.get(fake.uuid, protocol="stac")
            odata = library_client.get(fake.uuid, protocol="odata")
            expected = Checksum(algorithm="md5", value=hashlib.md5(fake.zip_bytes).hexdigest())
            assert stac.checksum == expected
            assert odata.checksum == expected

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_checksum_true_verifies_the_delivered_bytes(
        self,
        library_client: Client,
        platform: FakePlatform,
        tmp_path: Path,
        protocol: str,
    ) -> None:
        fake = platform.products[1]
        product = library_client.get(fake.uuid, protocol=protocol)
        target = tmp_path / protocol
        (report,) = library_client.download([product], target, via="http", checksum=True)
        assert report.checksum_verified is True
        assert report.path == target / fake.zip_name
        assert report.path.read_bytes() == fake.zip_bytes

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    def test_a_wrong_expectation_fails_on_both_protocols(
        self,
        make_platform: Callable[..., FakePlatform],
        make_workspace: Callable[[str], Workspace],
        tmp_path: Path,
        protocol: str,
    ) -> None:
        """Without this the previous test could pass on an unchecked pipeline."""
        fake = _MisdeclaredChecksum(
            uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            name="S2A_MSIL2A_20260705T095031_N0511_R079_T34UEE_20260705T105514.SAFE",
            datetime="2026-07-05T09:50:31Z",
        )
        instance = make_platform(products=[fake])
        space = make_workspace(f"misdeclared-{protocol}")
        with _logged_in_client(space, instance) as client:
            product = client.get(fake.uuid, protocol=protocol)
            assert product.checksum == Checksum(algorithm="md5", value=_WRONG_MD5)

            target = tmp_path / f"rejected-{protocol}"
            with pytest.raises(DownloadError, match=r"checksum mismatch \(md5\)") as raised:
                client.download([product], target, via="http", checksum=True)
            assert raised.value.product_id == fake.uuid
            assert raised.value.backend == "http"
            # R7's cleanup claim, asserted here because this is the only place
            # in the parity file that fails a transfer: nothing survives.
            assert list(target.iterdir()) == []

            # The transfer itself is fine — only the expectation was wrong.
            (report,) = client.download([product], target, via="http", checksum=False)
            assert report.checksum_verified is None
            assert report.path.read_bytes() == fake.zip_bytes


_WRONG_MD5 = "0" * 32


class _MisdeclaredChecksum(FakeProduct):
    """A product whose advertised md5 does not describe its ``$value`` body.

    Both projections read :attr:`FakeProduct.md5`, so overriding it corrupts the
    STAC multihash and the OData ``Checksum[]`` entry in exactly the same way —
    which is what a server-side metadata error looks like from the client.
    """

    @property
    def md5(self) -> str:
        return _WRONG_MD5


# -- PP4 -----------------------------------------------------------------------
# Expected hits are given as indices into ``FakePlatform.products`` (see the
# default set in ``platform.default_products``); each query is chosen so that it
# excludes at least one product, otherwise "both protocols agree" would only
# prove that neither filter did anything.

QUERIES = [
    pytest.param({"collection": "SENTINEL-2"}, (0, 1, 2, 3), id="collection"),
    pytest.param({"collection": "SENTINEL-1"}, (4,), id="other-collection"),
    pytest.param(
        {"collection": "SENTINEL-2", "datetime": "2026-06-01/2026-06-10"},
        (1, 2),
        id="collection+interval",
    ),
    pytest.param(
        {"collection": "SENTINEL-2", "bbox": (22.5, 52.9, 24.0, 53.5)},
        (0, 1, 2),
        id="collection+bbox",
    ),
    pytest.param(
        {"collection": "SENTINEL-2", "filters": {"cloudCover": "<20"}},
        (0, 1),
        id="collection+cloudCover",
    ),
    # Boundary cases: product 2 sits *exactly* on the threshold, so a strict
    # operator translated as its inclusive twin (or the reverse) changes the
    # result set on one protocol only. Without a product on the boundary,
    # `lt`/`le` and `gt`/`ge` are indistinguishable here and a translation
    # drift in `_odata_filter._OP_TOKENS` would pass this file unnoticed.
    pytest.param(
        {"collection": "SENTINEL-2", "filters": {"cloudCover": "<42"}},
        (0, 1),
        id="cloudCover-lt-on-the-boundary",
    ),
    pytest.param(
        {"collection": "SENTINEL-2", "filters": {"cloudCover": "<=42"}},
        (0, 1, 2),
        id="cloudCover-lte-on-the-boundary",
    ),
    pytest.param(
        {"collection": "SENTINEL-2", "filters": {"cloudCover": ">42"}},
        (3,),
        id="cloudCover-gt-on-the-boundary",
    ),
    pytest.param(
        {"collection": "SENTINEL-2", "filters": {"cloudCover": ">=42"}},
        (2, 3),
        id="cloudCover-gte-on-the-boundary",
    ),
    pytest.param(
        {"collection": "SENTINEL-2", "filters": {"productType": "S2MSI2A"}},
        (0, 1, 3),
        id="collection+productType",
    ),
    pytest.param(
        {
            "collection": "SENTINEL-2",
            "datetime": "2026-06-01/2026-06-30",
            "bbox": (22.0, 52.0, 25.0, 54.0),
            "filters": {"cloudCover": "<50"},
        },
        (0, 1, 2),
        id="combination",
    ),
]


class TestFilterTranslationParity:
    """PP4 — one logical query, two filter dialects, one answer.

    The harness *evaluates* the ``$filter`` the SDK generates (an unrecognized
    clause answers HTTP 400 rather than silently matching nothing), so an empty
    or lopsided result here means the translation diverged.
    """

    @pytest.mark.parametrize(("query", "expected"), QUERIES)
    def test_both_protocols_select_the_same_products(
        self,
        library_client: Client,
        platform: FakePlatform,
        query: dict[str, Any],
        expected: tuple[int, ...],
    ) -> None:
        wanted = {platform.products[index].uuid for index in expected}
        assert wanted, "an empty expectation would make this test vacuous"

        found = {}
        counts = {}
        for protocol in PROTOCOLS:
            result = library_client.search(protocol=protocol, **query)
            found[protocol] = {product.id for product in result}
            counts[protocol] = len(result)  # numberMatched / @odata.count

        assert found["stac"] == wanted
        assert found["odata"] == wanted
        # The advertised totals feed len(SearchResult); they are a second,
        # independent projection of the same match set and must agree too.
        assert counts["stac"] == counts["odata"] == len(wanted)

    def test_paging_does_not_change_the_agreement(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        """The equal id sets above are assembled across a paging boundary.

        With ``platform.page_size == 2`` a four-hit query needs two pages on
        each protocol, so PP4's agreement is a property of the *assembled*
        result rather than of a single response.
        """
        platform.reset_calls()
        stac = [p.id for p in library_client.search(collection="SENTINEL-2", protocol="stac")]
        stac_pages = platform.call_count("stac", path="/search")

        platform.reset_calls()
        odata = [p.id for p in library_client.search(collection="SENTINEL-2", protocol="odata")]
        odata_pages = platform.call_count("odata", path="/Products")

        assert len(stac) == len(odata) == 4
        assert stac_pages == 2
        assert odata_pages == 2
