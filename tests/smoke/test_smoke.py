"""Small, idempotent end-to-end checks against the live platform."""

from __future__ import annotations

from pathlib import Path

import pytest

from eosdk import Client
from tests.smoke.conftest import skip_on_key_quota

pytestmark = pytest.mark.smoke

BBOX = (22.5, 52.9, 24.0, 53.5)


class TestAnonymous:
    def test_stac_search(self, client: Client) -> None:
        products = list(
            client.search(collection="sentinel-2-l2a", bbox=BBOX, limit=2, protocol="stac")
        )
        assert products
        product = products[0]
        assert product.id and product.name
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
        assert len(products) == 2
        assert len(results) > 0  # @odata.count

    def test_stac_and_odata_agree_on_a_product(self, client: Client) -> None:
        (stac_product,) = list(
            client.search(collection="sentinel-2-l2a", bbox=BBOX, limit=1, protocol="stac")
        )
        odata_product = client._odata_catalogue().get(stac_product.id)
        assert odata_product.name.startswith(stac_product.name[:60])

    def test_doctor_reports_catalogues_reachable(self, client: Client) -> None:
        from eosdk.doctor import run_doctor

        sections = {s.name: s for s in run_doctor(client)}
        services = {r.name: r for r in sections["Services"].results}
        assert services["STAC catalogue"].ok is True
        assert services["OData catalogue"].ok is True


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

    def test_zipper_list_product_root(self, logged_in_client: Client) -> None:
        (product,) = list(
            logged_in_client.search(
                collection="sentinel-2-l2a", bbox=BBOX, limit=1, protocol="stac"
            )
        )
        nodes = logged_in_client.list(product, via="zipper")
        assert nodes
        assert any(n.is_dir for n in nodes) or any(n.name.endswith(".xml") for n in nodes)

    @pytest.mark.slow
    def test_exos_open_ranged_read(self, logged_in_client: Client, tmp_path: Path) -> None:
        (product,) = list(
            logged_in_client.search(
                collection="sentinel-2-l2a", bbox=BBOX, limit=1, protocol="stac"
            )
        )
        with skip_on_key_quota():
            nodes = logged_in_client.list(product, via="exos", recursive=True)
        target = next(n for n in nodes if not n.is_dir and n.name.endswith(".xml"))
        with logged_in_client.open(product, path=target.path, via="exos") as fh:
            head = fh.read(256)
        assert head  # ranged read, no full-product transfer
