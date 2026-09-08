"""Transfer journeys per data-access backend — E2E_TEST_PLAN.md §4.3, T1..T5.

Every test here is parametrized over ``via`` and holds the catalogue fixed:
products are built straight from :class:`FakeProduct` by :func:`product_of`
instead of coming out of a search, so a red test in this file accuses the
download backend and nothing else.

What the file pins down, backend by backend:

* T1 — a 4-product batch lands with the names each backend advertises, byte
  for byte, and ``DownloadReport.checksum_verified`` says only what that
  backend can honestly claim;
* T2 — ``list(recursive=True)`` is exactly the BFS union of the per-level
  listings, with real sizes and ``is_dir`` on directories only;
* T3 — ``open`` works where the backend advertises ``RandomAccess`` and is
  refused with a diagnosable error where it does not;
* T4 — the progress contract under real concurrency;
* T5 — the ephemeral-key lifecycle around an S3 transfer (s3 only).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from eosdk.eodata.s3 import RangedS3File
from eosdk.exceptions import DownloadError, UnsupportedCapability
from eosdk.models import Checksum, Product
from tests.e2e.platform import S3CallRecorder
from tests.e2e.surface import normalized_nodes

if TYPE_CHECKING:
    from pathlib import Path

    from eosdk.client import Client
    from eosdk.eodata.base import ProgressEvent
    from tests.e2e.conftest import Workspace
    from tests.e2e.platform import FakePlatform, FakeProduct

pytestmark = pytest.mark.e2e

#: both data-access backends; T5 narrows this to s3 for a documented reason
via_backend = pytest.mark.parametrize("via", ["http", "s3"])


# -- the fixed catalogue -------------------------------------------------------


def product_of(fake: FakeProduct) -> Product:
    """The normalized catalogue view of a :class:`FakeProduct`, built offline.

    Every field is what *both* ``_item_to_product`` and ``_entry_to_product``
    already extract from this product (asserted by
    ``test_platform_harness.TestProjections``), so bypassing search costs no
    fidelity: ``checksum`` is the md5 of the ``$value`` payload and ``s3_path``
    is the OData ``/bucket/key`` vocabulary, both of which the SDK accepts.
    """
    return Product(
        id=fake.uuid,
        name=fake.name,
        collection=fake.collection,
        size=fake.size,
        datetime=fake.instant,
        cloud_cover=fake.cloud_cover,
        checksum=Checksum(algorithm="md5", value=fake.md5),
        s3_path=fake.s3_path,
    )


def batch(platform: FakePlatform) -> list[Product]:
    """The four multi-file SENTINEL-2 products (T1's batch)."""
    return [product_of(fake) for fake in platform.products[:4]]


def expected_payload(fake: FakeProduct, target: Path, via: str) -> dict[Path, bytes]:
    """Where a backend puts a product's bytes, and which bytes those are.

    The two backends deliver genuinely different things: ``http`` streams the
    packaged ``$value`` zip under the name the Content-Disposition header
    advertises, ``s3`` copies the object tree into a directory named after the
    product.
    """
    if via == "http":
        return {target / fake.zip_name: fake.zip_bytes}
    return {target / fake.name / logical: content for logical, content in fake.tree.items()}


def expected_bytes(fake: FakeProduct, via: str) -> int:
    if via == "http":
        return fake.size
    return sum(len(content) for content in fake.tree.values())


def expected_verified(fake: FakeProduct, via: str) -> bool | None:
    """What each backend can honestly claim about a product-level checksum.

    ``http`` hashes the single stream it wrote, so ``True``. ``s3`` delivers
    one object per file and a product-level checksum describes one payload, so
    ``eodata/s3.py`` only verifies when the product is a single object —
    otherwise it reports ``None`` (nothing verified) rather than lying.
    """
    if via == "http":
        return True
    return True if len(fake.tree) == 1 else None


def files_under(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


# -- T1: a 4-product batch -----------------------------------------------------


@via_backend
class TestT1BatchDownload:
    def test_batch_lands_with_advertised_names_and_exact_bytes(
        self,
        library_client: Client,
        platform: FakePlatform,
        workspace: Workspace,
        via: str,
    ) -> None:
        """T1: four products, concurrency 4, checksums on."""
        products = batch(platform)
        target = workspace.downloads

        reports = library_client.download(products, target, via=via, concurrency=4, checksum=True)

        assert [report.product_id for report in reports] == [p.id for p in products]

        wanted: dict[Path, bytes] = {}
        for fake in platform.products[:4]:
            wanted.update(expected_payload(fake, target, via))
        # An exact file-set comparison also proves no `.part` body and no
        # resume sidecar survived the transfer.
        assert files_under(target) == wanted

    def test_report_fields_match_what_the_backend_delivered(
        self,
        library_client: Client,
        platform: FakePlatform,
        workspace: Workspace,
        via: str,
    ) -> None:
        """T1: path, byte count and checksum_verified, per product."""
        products = batch(platform)
        target = workspace.downloads

        reports = library_client.download(products, target, via=via, concurrency=4, checksum=True)

        by_id = {report.product_id: report for report in reports}
        for fake in platform.products[:4]:
            report = by_id[fake.uuid]
            assert report.path == (target / fake.zip_name if via == "http" else target / fake.name)
            assert report.bytes == expected_bytes(fake, via)
            assert report.checksum_verified is expected_verified(fake, via)

        # Not a tautology: the two backends genuinely disagree here, and the
        # disagreement is the point of the assertion above.
        assert {report.checksum_verified for report in reports} == (
            {True} if via == "http" else {None}
        )

    def test_single_object_product_is_the_only_s3_checksum_shape(
        self,
        library_client: Client,
        platform: FakePlatform,
        workspace: Workspace,
        via: str,
    ) -> None:
        """T1: the single-object rule in ``eodata/s3.py``, exercised head-on.

        ``S3Downloader._fetch_one`` verifies a product-level checksum exactly
        when one object was written. The Sentinel-1 product is that shape — but
        the object it writes is the *unpacked* payload while the catalogue
        checksum describes the packaged ``$value`` zip, so the rule fires and
        then fails. That mismatch is intrinsic to the two vocabularies (it is
        equally true on CDSE), so this asserts the refusal rather than a
        verification that cannot happen. See ``deviations`` in the unit report.
        """
        fake = platform.products[4]
        assert len(fake.tree) == 1, "this test needs the single-object product"
        target = workspace.downloads

        if via == "http":
            reports = library_client.download([product_of(fake)], target, via=via)
            assert reports[0].checksum_verified is True
            assert (target / fake.zip_name).read_bytes() == fake.zip_bytes
            return

        with pytest.raises(DownloadError) as excinfo:
            library_client.download([product_of(fake)], target, via=via, checksum=True)
        assert "checksum mismatch (md5)" in str(excinfo.value)
        assert excinfo.value.backend == "s3"

        # With verification off the same transfer succeeds and claims nothing.
        reports = library_client.download([product_of(fake)], target, via=via, checksum=False)
        assert reports[0].checksum_verified is None
        assert files_under(target) == expected_payload(fake, target, via)


# -- T2: listing ---------------------------------------------------------------


def level_of(fake: FakeProduct, path: str) -> set[tuple[str, str, bool, int | None]]:
    """(path, name, is_dir, size) of one directory level, straight from the tree."""
    level = set()
    for name, is_dir, size in fake.children(path):
        logical = f"{path}/{name}" if path else name
        level.add((logical, name, is_dir, size))
    return level


def bfs_union(fake: FakeProduct) -> set[tuple[str, str, bool, int | None]]:
    """Every level of the tree, unioned — what ``recursive=True`` must return."""
    collected = level_of(fake, "")
    queue = [entry[0] for entry in collected if entry[2]]
    while queue:
        current = queue.pop()
        children = level_of(fake, current)
        collected |= children
        queue.extend(entry[0] for entry in children if entry[2])
    return collected


@via_backend
class TestT2Listing:
    def test_recursive_listing_is_the_bfs_union_of_the_levels(
        self,
        library_client: Client,
        platform: FakePlatform,
        via: str,
    ) -> None:
        """T2: recursive == union of per-level listings == the harness tree."""
        fake = platform.products[0]  # the only full SAFE tree, four levels deep
        product = product_of(fake)

        flat = library_client.list(product, via=via, recursive=False)
        assert normalized_nodes(flat) == level_of(fake, "")

        # Walk the tree one level at a time through the SDK, exactly as a
        # caller without `recursive=True` would have to.
        union = set(normalized_nodes(flat))
        queue = [node.path for node in flat if node.is_dir]
        while queue:
            current = queue.pop()
            children = library_client.list(product, current, via=via, recursive=False)
            assert normalized_nodes(children) == level_of(fake, current)
            union |= normalized_nodes(children)
            queue.extend(node.path for node in children if node.is_dir)

        deep = library_client.list(product, via=via, recursive=True)
        assert normalized_nodes(deep) == union == bfs_union(fake)

    def test_sizes_are_real_lengths_and_is_dir_marks_directories_only(
        self,
        library_client: Client,
        platform: FakePlatform,
        via: str,
    ) -> None:
        """T2: the two populated Node fields, against the source bytes."""
        fake = platform.products[0]
        nodes = library_client.list(product_of(fake), via=via, recursive=True)

        assert {node.path for node in nodes if node.is_dir} == set(fake.directories())
        assert {node.path for node in nodes if not node.is_dir} == set(fake.tree)
        assert {node.path: node.size for node in nodes if not node.is_dir} == {
            logical: len(content) for logical, content in fake.tree.items()
        }
        # Directories have no size on either backend — neither invents a 0.
        assert all(node.size is None for node in nodes if node.is_dir)

    def test_recursion_cost_is_one_request_per_directory_on_http_only(
        self,
        library_client: Client,
        platform: FakePlatform,
        monkeypatch: pytest.MonkeyPatch,
        via: str,
    ) -> None:
        """T2: the documented asymmetry in ``eodata/http.py::list``.

        http walks the Nodes hierarchy — one request per directory, root
        included; s3 walks a key prefix in a single paginated ListObjectsV2.
        Asserting it keeps the "documented, not hidden" claim honest.
        """
        fake = platform.products[0]
        recorder = S3CallRecorder().install(monkeypatch)
        product = product_of(fake)

        if via == "s3":
            library_client.list(product, via=via, recursive=True)  # warm the keys/boto client
        platform.reset_calls()
        recorder.calls.clear()

        library_client.list(product, via=via, recursive=True)

        if via == "http":
            assert platform.call_count("eodata_http", "GET") == 1 + len(fake.directories())
            assert recorder.operations("ListObjectsV2") == []
        else:
            assert len(recorder.operations("ListObjectsV2")) == 1
            assert platform.call_count("eodata_http") == 0


# -- T3: open + ranged reads ---------------------------------------------------

#: a file deep inside the full SAFE tree, big enough to read across the
#: RangedS3File read-ahead boundary is not needed — but it must not be tiny
BAND = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"


@via_backend
class TestT3Open:
    def test_ranged_reads_match_the_underlying_bytes(
        self,
        library_client: Client,
        platform: FakePlatform,
        via: str,
    ) -> None:
        """T3: ``open`` where RandomAccess is advertised (s3), refused where not."""
        fake = platform.products[0]
        product = product_of(fake)
        content = fake.tree[BAND]

        if via == "http":
            platform.reset_calls()
            with pytest.raises(UnsupportedCapability) as excinfo:
                library_client.open(product, BAND, via=via)
            error = excinfo.value
            assert error.backend == "http"
            assert error.capability == "open"
            # No http strategy in BUILTIN_MATRIX declares `open`, so
            # select_strategy has no name to offer: the message stops at
            # backend + capability instead of inventing an alternative.
            assert error.alternative is None
            assert str(error) == "backend 'http' does not support the 'open' capability"
            # Capability dispatch happens before any network work.
            assert platform.call_count("eodata_http") == 0
            return

        with library_client.open(product, BAND, via=via) as handle:
            assert isinstance(handle, RangedS3File)
            assert handle.size == len(content)

            assert handle.read(4) == content[:4]
            assert handle.tell() == 4

            assert handle.seek(1000) == 1000
            assert handle.read(500) == content[1000:1500]

            # backwards seek: the read-ahead buffer must not be reused blindly
            handle.seek(12)
            assert handle.read(8) == content[12:20]

            assert handle.seek(-9, 2) == len(content) - 9
            assert handle.read() == content[-9:]

            # reading past the end yields b"" rather than raising
            assert handle.read() == b""

            handle.seek(0)
            assert handle.read() == content

    def test_open_names_the_missing_file_inside_the_product(
        self,
        library_client: Client,
        platform: FakePlatform,
        via: str,
    ) -> None:
        """T3: the s3 backend diagnoses a bad path; http still refuses first."""
        product = product_of(platform.products[0])

        if via == "http":
            with pytest.raises(UnsupportedCapability):
                library_client.open(product, "no/such/file", via=via)
            return

        with pytest.raises(DownloadError) as excinfo:
            library_client.open(product, "no/such/file", via=via)
        assert "no/such/file" in str(excinfo.value)
        assert excinfo.value.backend == "s3"


# -- T4: progress --------------------------------------------------------------


class ProgressCollector:
    """Progress events arrive from worker threads; collect them under a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            self.events.append(event)

    def of(self, product_id: str) -> list[ProgressEvent]:
        with self._lock:
            return [event for event in self.events if event.product_id == product_id]


@via_backend
class TestT4Progress:
    def test_progress_contract_across_a_concurrent_batch(
        self,
        library_client: Client,
        platform: FakePlatform,
        workspace: Workspace,
        via: str,
    ) -> None:
        """T4: monotonic bytes_done, one terminal event, totals where advertised."""
        products = batch(platform)
        collector = ProgressCollector()

        library_client.download(
            products,
            workspace.downloads,
            via=via,
            concurrency=4,
            checksum=True,
            progress=collector,
        )

        assert {event.product_id for event in collector.events} == {p.id for p in products}

        for fake in platform.products[:4]:
            events = collector.of(fake.uuid)
            kinds = [event.kind for event in events]
            total = expected_bytes(fake, via)

            assert kinds[0] == "start"
            assert kinds.count("done") == 1
            assert kinds[-1] == "done"
            assert "error" not in kinds
            assert "retry" not in kinds

            progressed = [event.bytes_done for event in events]
            assert progressed == sorted(progressed)
            assert events[0].bytes_done == 0
            assert events[-1].bytes_done == total

            assert events[0].bytes_total == total
            assert events[-1].bytes_total == total
            chunk_totals = {e.bytes_total for e in events if e.kind == "chunk"}
            if via == "http":
                # the service sends Content-Length, so every event can carry it
                assert chunk_totals == {total}
            else:
                # s3 sums the object sizes once, at "start"; per-range progress
                # deliberately passes None rather than re-deriving it
                assert chunk_totals == {None}
            assert [e.bytes_done for e in events if e.kind == "chunk"][-1] == total


# -- T5: ephemeral keys around an s3 transfer ----------------------------------


@pytest.mark.parametrize(
    "via",
    [
        "s3",
        pytest.param(
            "http",
            marks=pytest.mark.skip(
                reason="S3 key pairs are an s3-only concept by design: the http backend "
                "authenticates every request with the Keycloak bearer token and never "
                "reaches the credentials service (SPEC §6.4)"
            ),
        ),
    ],
)
class TestT5EphemeralKeys:
    def test_key_lifecycle_around_a_transfer(
        self,
        library_client: Client,
        platform: FakePlatform,
        workspace: Workspace,
        via: str,
    ) -> None:
        """T5: mint, list while live, no re-mint on a second call, revoke on exit."""
        product = product_of(platform.products[1])
        platform.reset_calls()

        with library_client.keys.ephemeral() as credentials:
            # `ephemeral()` mints eagerly on __enter__, not on first S3 use.
            assert platform.call_count("keys", "POST") == 1
            assert credentials.access_key in platform.keys

            listed = library_client.keys.list()
            assert credentials.access_key in {entry.access_key for entry in listed}
            assert [entry.secret_key for entry in listed] == [None] * len(listed)

            library_client.download([product], workspace.downloads, via=via)
            minted_by_the_transfer = platform.call_count("keys", "POST")

            library_client.list(product, via=via)
            library_client.download([product], workspace.downloads, via=via)
            # The downloader caches its boto3 client, so nothing is re-minted.
            assert platform.call_count("keys", "POST") == minted_by_the_transfer

            live = {entry.access_key for entry in library_client.keys.list()}
            assert credentials.access_key in live

        assert credentials.access_key not in platform.keys
        assert platform.call_count("keys", "DELETE") == 1

    def test_transfer_mints_its_own_labelled_key_beside_the_ephemeral_one(
        self,
        library_client: Client,
        platform: FakePlatform,
        workspace: Workspace,
        via: str,
    ) -> None:
        """T5, the deviation: ``Client.download`` ignores the ephemeral key.

        The plan reads the block as "minted once on first use". In reality
        ``Client._s3_downloader`` is always wired to the *provider*, which
        applies the labelled-reuse policy (``get_or_create("eosdk")``), so a
        second key pair is created inside the block and survives the exit —
        there is no public way to hand a specific ``S3Credentials`` to
        ``Client.download``. Asserting the real shape keeps the leak visible.
        """
        product = product_of(platform.products[1])
        platform.reset_calls()

        with library_client.keys.ephemeral() as credentials:
            library_client.download([product], workspace.downloads, via=via)
            assert platform.call_count("keys", "POST") == 2
            assert len(platform.keys) == 2

        surviving = set(platform.keys)
        assert credentials.access_key not in surviving
        assert len(surviving) == 1  # the "eosdk"-labelled key the transfer minted

        # It is reused, not re-minted, by the next client on the same cache dir.
        stored = workspace.keys / "e2e.json"
        assert stored.exists()
        assert set(surviving) == {entry["access_id"] for entry in _labels(stored).values()}


def _labels(path: Path) -> dict[str, dict[str, Any]]:
    import json

    return dict(json.loads(path.read_text()))
