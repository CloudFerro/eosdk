"""CLI ↔ library parity (plan §4.5, PS1-PS3).

``eo`` and ``Client`` are specified as 1:1 (SPEC §7.4), but until now each
surface was tested separately against different fixtures — so a filter parsed
differently, a default that diverged, or a product field dropped in JSON output
would have been invisible. These tests drive one platform through both surfaces
and compare the results to each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from eosdk.exceptions import (
    AuthError,
    EndpointUnreachable,
    ProductNotFound,
    QuotaExceeded,
    UnsupportedCapability,
)
from eosdk.models import Product
from tests.e2e.surface import (
    CliSurface,
    LibrarySurface,
    Surface,
    normalized_nodes,
    normalized_products,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from eosdk.client import Client
    from tests.e2e.platform import FakePlatform
    from tests.e2e.surface import Invoke

pytestmark = pytest.mark.e2e

B04 = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"
QUERY: dict[str, Any] = {
    "collection": "SENTINEL-2",
    "bbox": (22.5, 52.9, 24.0, 53.5),
    "datetime": "2026-06-01/2026-06-30",
    "filters": {"cloudCover": "<50"},
    "protocol": "stac",
    "limit": 10,
}


class TestTheJourneyAgrees:
    """PS1 — the same script, both surfaces, compared to each other."""

    def test_each_surface_completes_the_journey(
        self, surface: Surface, platform: FakePlatform, tmp_path: Path
    ) -> None:
        products = surface.search(**QUERY)
        assert [p.name for p in products] == [platform.products[i].name for i in (0, 1, 2)]

        transfers = surface.download(products, tmp_path / surface.name, via="http")
        assert {t.path.name for t in transfers} == {p.name + ".zip" for p in products}
        assert all(t.checksum_verified for t in transfers)
        for transfer in transfers:
            source = next(p for p in platform.products if transfer.path.name == p.zip_name)
            assert transfer.path.read_bytes() == source.zip_bytes
            assert transfer.bytes == source.size

        nodes = surface.list(products[0], via="http", recursive=True)
        assert B04 in {node.path for node in nodes}
        assert surface.read(products[0], B04, via="s3") == platform.products[0].tree[B04]

    def test_both_surfaces_return_the_same_thing(
        self,
        make_client: Callable[..., Client],
        cli: Invoke,
        platform: FakePlatform,
        tmp_path: Path,
    ) -> None:
        """The actual agreement assertion: one test, both adapters, compared.

        The parametrized test above only proves each surface works; two surfaces
        can both "work" while disagreeing about what they return.
        """
        library = LibrarySurface(make_client())
        command_line = CliSurface(cli)
        library.login(platform.username, platform.password)

        found = {name: adapter.search(**QUERY) for name, adapter in _both(library, command_line)}
        assert normalized_products(found["library"]) == normalized_products(found["cli"])

        transfers = {
            name: adapter.download(found[name], tmp_path / name, via="http")
            for name, adapter in _both(library, command_line)
        }
        assert sorted(t.path.name for t in transfers["library"]) == sorted(
            t.path.name for t in transfers["cli"]
        )
        assert sorted(t.bytes for t in transfers["library"]) == sorted(
            t.bytes for t in transfers["cli"]
        )
        assert {t.checksum_verified for t in transfers["library"]} == {True}
        assert {t.checksum_verified for t in transfers["cli"]} == {True}
        for name in ("library", "cli"):
            for transfer in transfers[name]:
                assert (tmp_path / name / transfer.path.name).exists()

        listings = {
            name: adapter.list(found[name][0], via="http", recursive=True)
            for name, adapter in _both(library, command_line)
        }
        assert normalized_nodes(listings["library"]) == normalized_nodes(listings["cli"])

        reads = {
            name: adapter.read(found[name][0], B04, via="s3")
            for name, adapter in _both(library, command_line)
        }
        assert reads["library"] == reads["cli"] == platform.products[0].tree[B04]

    def test_collections_and_queryables_agree(
        self, make_client: Callable[..., Client], cli: Invoke, platform: FakePlatform
    ) -> None:
        library = LibrarySurface(make_client())
        command_line = CliSurface(cli)
        library.login(platform.username, platform.password)

        assert [c.model_dump() for c in library.collections()] == [
            c.model_dump() for c in command_line.collections()
        ]
        assert [q.model_dump() for q in library.queryables("SENTINEL-2")] == [
            q.model_dump() for q in command_line.queryables("SENTINEL-2")
        ]
        assert (
            library.get(platform.products[0].uuid).model_dump()
            == command_line.get(platform.products[0].uuid).model_dump()
        )


class TestProductRepresentationRoundTrips:
    """PS2 — the two surfaces exchange the same product representation."""

    def test_cli_json_feeds_the_library(
        self, logged_in_cli: Invoke, library_client: Client, platform: FakePlatform, tmp_path: Path
    ) -> None:
        result = logged_in_cli(
            "search",
            "--collection",
            "SENTINEL-2",
            "--from",
            "2026-06-15",
            "--to",
            "2026-06-16",
            "--format",
            "json",
        )
        assert result.exit_code == 0, result.stderr
        products = [Product.model_validate(entry) for entry in result.json_lines()]
        assert len(products) == 1

        reports = library_client.download(products, target=tmp_path / "from-cli", via="http")
        assert reports[0].path.read_bytes() == platform.products[0].zip_bytes

    def test_library_products_feed_eo_download(
        self, library_client: Client, cli: Invoke, platform: FakePlatform, tmp_path: Path
    ) -> None:
        products = list(
            library_client.search(collection="SENTINEL-2", datetime="2026-06-15/2026-06-16")
        )
        payload = "\n".join(product.model_dump_json() for product in products)
        result = cli(
            "download", "-", "--output", str(tmp_path / "from-lib"), "--via", "http", input=payload
        )
        assert result.exit_code == 0, result.stderr
        landed = tmp_path / "from-lib" / platform.products[0].zip_name
        assert landed.read_bytes() == platform.products[0].zip_bytes

    def test_json_lines_round_trip_is_lossless(self, library_client: Client) -> None:
        """Every field ``Product`` declares survives the JSON Lines hop.

        ``eo download -`` reconstructs products with ``Product.model_validate``,
        so anything the serializer drops silently degrades the piped workflow.
        """
        products = list(
            library_client.search(collection="SENTINEL-2", datetime="2026-06-01/2026-06-30")
        )
        assert products
        for product in products:
            restored = Product.model_validate_json(product.model_dump_json())
            assert restored.model_dump() == product.model_dump()
            assert set(restored.model_dump()) == set(Product.model_fields)


class TestErrorTaxonomyIsShared:
    """PS3 — one failure, one exception class, one exit code and one hint."""

    def test_product_not_found(self, library_client: Client, logged_in_cli: Invoke) -> None:
        unknown = "00000000-0000-4000-8000-000000000000"
        with pytest.raises(ProductNotFound):
            library_client.get(unknown)
        result = logged_in_cli("get", unknown, "--json")
        _assert_cli_failed(result, "not found")

    def test_auth_error(
        self, client: Client, cli: Invoke, platform: FakePlatform, tmp_path: Path
    ) -> None:
        product = next(
            iter(client.search(collection="SENTINEL-2", datetime="2026-06-15/2026-06-16"))
        )
        with pytest.raises(AuthError):
            client.download(product, target=tmp_path / "lib", via="http")
        result = cli("download", product.id, "--output", str(tmp_path / "cli"), "--quiet")
        _assert_cli_failed(result, "eo auth login")

    def test_endpoint_unreachable(
        self, library_client: Client, logged_in_cli: Invoke, platform: FakePlatform
    ) -> None:
        platform.take_offline("stac")
        with pytest.raises(EndpointUnreachable) as exc_info:
            list(library_client.search(collection="SENTINEL-2", datetime="2026-06-01/2026-06-30"))
        assert exc_info.value.service == "catalogue_stac"
        result = logged_in_cli(
            "search", "--collection", "SENTINEL-2", "--from", "2026-06-01", "--to", "2026-06-30"
        )
        _assert_cli_failed(result, "unreachable")
        platform.bring_online("stac")

    def test_quota_exceeded(
        self, library_client: Client, logged_in_cli: Invoke, platform: FakePlatform, tmp_path: Path
    ) -> None:
        product = next(
            iter(library_client.search(collection="SENTINEL-2", datetime="2026-06-15/2026-06-16"))
        )
        platform.fail_next("eodata_http", 429, times=99, retry_after=1)
        with pytest.raises(QuotaExceeded) as exc_info:
            library_client.download(product, target=tmp_path / "lib", via="http")
        assert exc_info.value.service == "eodata_http"

        platform.clear_faults("eodata_http")
        platform.fail_next("eodata_http", 429, times=99, retry_after=1)
        result = logged_in_cli("download", product.id, "--output", str(tmp_path / "cli"), "--quiet")
        _assert_cli_failed(result, "quota exceeded")
        platform.clear_faults("eodata_http")

    def test_unsupported_capability(
        self, library_client: Client, logged_in_cli: Invoke, platform: FakePlatform
    ) -> None:
        product = next(
            iter(library_client.search(collection="SENTINEL-2", datetime="2026-06-15/2026-06-16"))
        )
        with pytest.raises(UnsupportedCapability):
            library_client.open(product, path=B04, via="http")
        result = logged_in_cli("cat", product.id, B04, "--via", "http")
        _assert_cli_failed(result, "does not support the 'open' capability")


def _both(library: LibrarySurface, command_line: CliSurface) -> list[tuple[str, Surface]]:
    return [("library", library), ("cli", command_line)]


def _assert_cli_failed(result: Any, hint: str) -> None:
    """The CLI's half of the contract: non-zero exit, hint on stderr only.

    Diagnostics on stdout would corrupt `eo search --format json | eo download -`,
    so the stream separation is part of the error contract, not cosmetics.
    """
    assert result.exit_code != 0
    assert hint in result.stderr
    assert hint not in result.stdout
