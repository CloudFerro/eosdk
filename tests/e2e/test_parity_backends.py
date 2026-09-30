"""Backend parity: ``via="http"`` and ``via="s3"`` describe the same product.

Plan §4.5, PB1-PB3. Both backends are projections of one :class:`FakeProduct`:
the download service hands over a zip built from ``tree``, the bucket holds the
same ``tree`` extracted. A difference in delivered bytes, in node metadata or
in ``DownloadReport`` shape is therefore a bug in one of the two backends and
never a fixture that drifted.

* PB1 — the same product downloaded both ways carries the same content, and the
  report fields each backend can honestly claim.
* PB2 — listings agree on paths *and* sizes *and* ``is_dir``, recursively and
  level by level (``tests/eodata/test_http_list.py`` proves this for the
  downloaders; here it runs through ``Client``'s capability dispatch).
* PB3 — ``open`` is offered exactly where the capability matrix says it is, and
  what it returns is byte-identical to what the other backend delivered.
"""

from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import pytest

from eosdk.eodata.capabilities import BUILTIN_MATRIX, Capability
from eosdk.exceptions import DownloadError, UnsupportedCapability
from tests.e2e.platform import FakeProduct, seed_s3

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from eosdk.client import Client
    from eosdk.models import Node, Product
    from tests.e2e.platform import FakePlatform

pytestmark = pytest.mark.e2e

#: indices into ``platform.products`` (see the harness's ``default_products``)
DEEP_TREE = 0  # full SAFE tree: branches at the root, four directory levels
SINGLE_FILE = 4  # SENTINEL-1: one stored object, the shape s3 tries to checksum


# -- helpers -------------------------------------------------------------------


def _unzip(archive: Path, root: str) -> dict[str, bytes]:
    """Logical path -> bytes for a ``$value`` artifact.

    The service packs members as ``<product name>/<logical path>``; stripping
    that root is what makes the archive comparable to an extracted tree.
    """
    with zipfile.ZipFile(archive) as handle:
        names = handle.namelist()
        assert all(name.startswith(f"{root}/") for name in names), names
        return {name[len(root) + 1 :]: handle.read(name) for name in names}


def _files_under(root: Path) -> dict[str, bytes]:
    """Logical path -> bytes for a directory tree, leftovers included.

    Nothing is filtered: a stray ``.part`` or resume sidecar shows up as an
    extra key and breaks the comparison, which is the point.
    """
    return {
        entry.relative_to(root).as_posix(): entry.read_bytes()
        for entry in sorted(root.rglob("*"))
        if entry.is_file()
    }


def _node_map(nodes: Iterable[Node]) -> dict[str, tuple[str, bool, int | None]]:
    """path -> (name, is_dir, size): the whole ``Node`` contract minus ``raw``."""
    return {node.path: (node.name, node.is_dir, node.size) for node in nodes}


SINGLE_OBJECT_NAME = "S1A_IW_GRDH_1SDV_20260611T090000_012346_016ABD_5678"
SINGLE_OBJECT_PAYLOAD = b"\x00GRD-single-object" * 512


def _register_single_object_product(platform: FakePlatform) -> FakeProduct:
    """A product whose ``$value`` *is* the stored object, not an archive of it.

    A real single-file product is streamed verbatim by ``$value``, so one
    catalogue checksum describes both the http artifact and the single s3
    object. Pinning ``_zip`` models that; the harness's default SENTINEL-1
    product zips its one file instead, which is exactly what breaks s3's
    product-level checksum (see the deviation test below).
    """
    product = FakeProduct(
        uuid="66666666-6666-4666-8666-666666666666",
        name=SINGLE_OBJECT_NAME,
        collection="SENTINEL-1",
        datetime="2026-06-11T09:00:00Z",
        bbox=(20.0, 50.0, 21.5, 51.5),
        attributes={"productType": "IW_GRDH_1S"},
        tree={"measurement.dat": SINGLE_OBJECT_PAYLOAD},
        prefix=f"Sentinel-1/SAR/GRD/2026/06/11/{SINGLE_OBJECT_NAME}",
        _zip=SINGLE_OBJECT_PAYLOAD,
    )
    platform.products.append(product)
    seed_s3(platform, products=[product])
    return product


# -- PB1: download -------------------------------------------------------------


class TestDownloadParity:
    """PB1: one zip against one extracted tree."""

    def test_the_http_archive_unpacks_to_the_s3_tree(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        source = platform.products[DEEP_TREE]
        product = library_client.get(source.name)

        [via_http] = library_client.download([product], tmp_path / "http", via="http")
        [via_s3] = library_client.download([product], tmp_path / "s3", via="s3")

        # The backends differ in packaging, and only in packaging: http names a
        # single file after Content-Disposition, s3 names a directory after the
        # product (the SPEC §6.6 "path root" caveat).
        assert via_http.path == tmp_path / "http" / source.zip_name
        assert via_s3.path == tmp_path / "s3" / source.name

        delivered_by_http = _unzip(via_http.path, source.name)
        delivered_by_s3 = _files_under(via_s3.path)
        assert delivered_by_http == delivered_by_s3 == source.tree
        assert len(source.tree) == 5  # a five-file, four-directory product

    def test_report_fields_agree_where_both_backends_can_agree(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        source = platform.products[DEEP_TREE]
        product = library_client.get(source.name)

        [via_http] = library_client.download([product], tmp_path / "http", via="http")
        [via_s3] = library_client.download([product], tmp_path / "s3", via="s3")

        assert via_http.product_id == via_s3.product_id == source.uuid
        assert via_http.attempts == via_s3.attempts == 1

        # `bytes` is what each backend actually moved, so the two differ by the
        # zip framing and nothing else — every payload byte is accounted for.
        assert via_http.bytes == len(source.zip_bytes)
        assert via_s3.bytes == sum(len(content) for content in source.tree.values())
        assert via_http.bytes > via_s3.bytes
        assert via_http.bytes - via_s3.bytes == len(source.zip_bytes) - via_s3.bytes

        # http hashed the one artifact the catalogue checksum describes. s3
        # delivered five objects, and a product-level checksum covers exactly
        # one (`S3Downloader._fetch_one`: `len(written) == 1`), so it reports
        # "nothing verified" rather than claiming a check it never made.
        assert via_http.checksum_verified is True
        assert via_s3.checksum_verified is None

    def test_a_true_single_object_product_verifies_on_both_backends(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """PB1: the one shape where s3 can honour ``checksum=True``."""
        source = _register_single_object_product(platform)
        product = library_client.get(source.name)
        assert product.checksum is not None
        assert product.checksum.algorithm == "md5"

        [via_http] = library_client.download([product], tmp_path / "http", via="http")
        [via_s3] = library_client.download([product], tmp_path / "s3", via="s3")

        assert via_http.checksum_verified is True
        assert via_s3.checksum_verified is True
        assert via_http.bytes == via_s3.bytes == len(SINGLE_OBJECT_PAYLOAD)
        # Same bytes, one flat file each way.
        assert via_http.path.read_bytes() == SINGLE_OBJECT_PAYLOAD
        assert _files_under(via_s3.path) == {"measurement.dat": SINGLE_OBJECT_PAYLOAD}

    def test_an_archived_single_object_product_fails_the_s3_checksum(
        self, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        """PB1 deviation: ``checksum_verified true on both`` does not hold here.

        The catalogue checksum describes what ``$value`` delivers — for this
        product an archive wrapping its single file. s3 hands over the file
        itself, and ``S3Downloader._fetch_one`` treats "exactly one delivered
        object" as a proxy for "the product checksum covers this payload". The
        proxy is unsound; asserting the real behaviour keeps the unsoundness
        visible instead of hiding it behind ``checksum=False``.
        """
        source = platform.products[SINGLE_FILE]
        product = library_client.get(source.name)

        [via_http] = library_client.download([product], tmp_path / "http", via="http")
        assert via_http.checksum_verified is True

        with pytest.raises(DownloadError) as excinfo:
            library_client.download([product], tmp_path / "s3", via="s3")
        assert "checksum mismatch (md5)" in str(excinfo.value)
        assert excinfo.value.backend == "s3"
        assert excinfo.value.product_id == source.uuid

        # The bytes s3 refused were the right bytes all along...
        delivered = tmp_path / "s3" / source.name / "product.dat"
        assert delivered.read_bytes() == source.tree["product.dat"]
        # ...and, unlike the http path (which unlinks its `.part` on mismatch),
        # s3 has already moved every object into place before the product
        # checksum runs, so a rejected transfer stays on disk. Plan R7 expects
        # a clean target directory; on this backend it is not clean.
        assert _files_under(tmp_path / "s3") == {
            f"{source.name}/product.dat": source.tree["product.dat"]
        }


# -- PB2: list -----------------------------------------------------------------


class TestListParity:
    """PB2: identical paths, sizes and ``is_dir`` flags from both backends."""

    @pytest.mark.parametrize("index", [DEEP_TREE, SINGLE_FILE], ids=["deep-tree", "single-file"])
    def test_recursive_listings_agree(
        self, library_client: Client, platform: FakePlatform, index: int
    ) -> None:
        source = platform.products[index]
        product = library_client.get(source.name)

        via_http = _node_map(library_client.list(product, via="http", recursive=True))
        via_s3 = _node_map(library_client.list(product, via="s3", recursive=True))
        assert via_http == via_s3

        # ...and what they agree on is the product, not an empty coincidence:
        # http walks the Nodes hierarchy one request per directory, s3 walks a
        # key prefix and synthesizes directories, so both must land on `tree`.
        assert {path for path, (_, is_dir, _) in via_http.items() if not is_dir} == set(source.tree)
        assert {path for path, (_, is_dir, _) in via_http.items() if is_dir} == set(
            source.directories()
        )
        assert {path: size for path, (_, is_dir, size) in via_http.items() if not is_dir} == {
            path: len(content) for path, content in source.tree.items()
        }
        # directories carry no size on either backend
        assert all(size is None for _, is_dir, size in via_http.values() if is_dir)

    def test_single_level_listings_agree_at_the_root_and_every_directory(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        source = platform.products[DEEP_TREE]
        product = library_client.get(source.name)
        directories = source.directories()
        assert len(directories) == 4  # the tree really does nest

        for path in ["", *directories]:
            via_http = _node_map(library_client.list(product, path, via="http"))
            via_s3 = _node_map(library_client.list(product, path, via="s3"))
            assert via_http == via_s3, path
            expected = {
                f"{path}/{name}".lstrip("/"): (name, is_dir, size)
                for name, is_dir, size in source.children(path)
            }
            assert via_http == expected, path

    def test_a_nested_level_is_not_the_whole_tree(
        self, library_client: Client, platform: FakePlatform
    ) -> None:
        """Guard for the level tests above: ``recursive=False`` must not recurse."""
        source = platform.products[DEEP_TREE]
        product = library_client.get(source.name)
        deepest = source.directories()[-1]

        for via in ("http", "s3"):
            level = _node_map(library_client.list(product, deepest, via=via))
            assert set(level) == {f"{deepest}/{name}" for name, _, _ in source.children(deepest)}
            assert len(level) == 2  # the two R10m bands, no ancestors, no siblings


# -- PB3: open -----------------------------------------------------------------

#: What the built-in matrix claims about RandomAccess, per backend. Derived, so
#: a new backend (or a capability change) is picked up without editing a list.
RANDOM_ACCESS: dict[str, bool] = {
    backend: any(Capability.OPEN in strategy.capabilities for strategy in strategies)
    for backend, strategies in BUILTIN_MATRIX.items()
}

#: Backends this file knows how to hand a catalogue ``Product`` to. A backend
#: added to BUILTIN_MATRIX must be listed here — and given whatever addressing
#: it needs — or PB3 fails loudly instead of silently not covering it.
ADDRESSABLE = frozenset({"http", "s3"})


class TestRandomAccessParity:
    """PB3: ``open`` where the matrix says so, byte-identical to the other backend."""

    def test_the_matrix_holds_no_backend_this_file_cannot_address(self) -> None:
        unknown = sorted(set(BUILTIN_MATRIX) - ADDRESSABLE)
        assert not unknown, (
            f"BUILTIN_MATRIX gained backend(s) {unknown}; teach PB3 how to address "
            "them (and how to read from them) instead of leaving them untested"
        )
        # The table must stay non-vacuous in both directions, otherwise the
        # parametrized test below could pass by asserting nothing interesting.
        assert RANDOM_ACCESS["s3"] is True
        assert any(RANDOM_ACCESS.values())

    @pytest.mark.parametrize(("backend", "expects_random_access"), sorted(RANDOM_ACCESS.items()))
    def test_open_is_offered_exactly_where_the_matrix_says(
        self,
        library_client: Client,
        reference: tuple[Product, str, bytes],
        backend: str,
        expects_random_access: bool,
    ) -> None:
        product, path, expected = reference

        # Dispatch consults the discovery-merged strategies rather than
        # BUILTIN_MATRIX; the intersection gate may only remove capabilities,
        # so the built-in table is an upper bound that must hold here too.
        merged = library_client.discovery.strategies_for(backend)
        offered = any(
            Capability.OPEN in strategy.capabilities and strategy.available for strategy in merged
        )
        assert offered is expects_random_access

        if not expects_random_access:
            with pytest.raises(UnsupportedCapability) as excinfo:
                library_client.open(product, path, via=backend)
            assert excinfo.value.backend == backend
            assert excinfo.value.capability == "open"
            return

        assert len(expected) > 1024  # seeks below must land inside a real payload
        with library_client.open(product, path, via=backend) as handle:
            assert handle.read() == expected
            assert handle.seek(0, io.SEEK_END) == len(expected)

            assert handle.seek(0) == 0
            assert handle.read(64) == expected[:64]
            assert handle.read(64) == expected[64:128]  # sequential, served from readahead

            middle = len(expected) // 2
            assert handle.seek(middle) == middle
            assert handle.tell() == middle
            assert handle.read(1000) == expected[middle : middle + 1000]

            assert handle.seek(-32, io.SEEK_END) == len(expected) - 32
            assert handle.read() == expected[-32:]
            assert handle.read() == b""  # at EOF, not wrapped around

    def test_open_rejects_a_path_that_is_not_in_the_product(
        self, library_client: Client, reference: tuple[Product, str, bytes]
    ) -> None:
        """The capability is dispatched before the path is resolved, not after."""
        product, _, _ = reference
        with pytest.raises(DownloadError) as excinfo:
            library_client.open(product, "GRANULE/nope.jp2", via="s3")
        assert excinfo.value.backend == "s3"
        assert "no such file inside product" in str(excinfo.value)


@pytest.fixture
def reference(
    library_client: Client, platform: FakePlatform, tmp_path: Path
) -> tuple[Product, str, bytes]:
    """(product, logical path, the bytes the *http* backend delivered for it).

    Taking the reference from the other backend's artifact is what makes PB3 a
    parity assertion: ``open`` is compared against bytes that travelled a
    different service, not against the same bucket it reads from.
    """
    source = platform.products[DEEP_TREE]
    product = library_client.get(source.name)
    [report] = library_client.download([product], tmp_path / "reference", via="http")
    path = max(source.tree, key=lambda logical: len(source.tree[logical]))
    return product, path, _unzip(report.path, source.name)[path]
