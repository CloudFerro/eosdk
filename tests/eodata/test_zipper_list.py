"""Zipper Listable via the Nodes hierarchy, incl. the cross-backend parity test."""

from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import respx

from eosdk.eodata.zipper import ZipperDownloader
from eosdk.models import Product
from eosdk.transport import RetryPolicy, Transport
from tests.eodata import safe_tree
from tests.eodata.test_zipper import BASE, FakeAuth

PRODUCT = Product(id="uuid-safe", name=safe_tree.PRODUCT_NAME)


def _tree() -> dict[str, list[tuple[str, bool, int | None]]]:
    """directory logical path -> [(name, is_dir, size)] from the shared fixture."""
    levels: dict[str, dict[str, tuple[bool, int | None]]] = {"": {}}
    for directory in safe_tree.DIRECTORIES:
        parent, _, name = directory.rpartition("/")
        levels.setdefault(parent, {})[name] = (True, None)
        levels.setdefault(directory, {})
    for logical, content in safe_tree.FILES.items():
        parent, _, name = logical.rpartition("/")
        levels.setdefault(parent, {})[name] = (False, len(content))
    return {
        path: sorted((n, d, s) for n, (d, s) in children.items())
        for path, children in levels.items()
    }


TREE = _tree()


def nodes_payload(directory: str) -> dict[str, Any]:
    return {
        "result": [
            {
                "Id": f"{directory}/{name}".lstrip("/"),
                "Name": name,
                "ContentLength": size or 0,
                "ChildrenNumber": len(TREE.get(f"{directory}/{name}".lstrip("/"), []))
                if is_dir
                else 0,
            }
            for name, is_dir, size in TREE[directory]
        ]
    }


def install_nodes_routes(router: respx.Router) -> dict[str, respx.Route]:
    """Register one route per directory, keyed by exact quoted URL."""
    routes: dict[str, respx.Route] = {}
    for directory in TREE:
        url = f"{BASE}/odata/v1/Products(uuid-safe)/Nodes"
        for segment in (s for s in directory.split("/") if s):
            url += f"('{segment}')/Nodes"
        routes[directory] = router.get(url__eq=url).mock(
            return_value=httpx.Response(200, json=nodes_payload(directory))
        )
    return routes


@pytest.fixture
def downloader() -> Iterator[ZipperDownloader]:
    with Transport(retry=RetryPolicy(jitter=False), sleep=lambda _: None) as transport:
        yield ZipperDownloader(BASE, transport=transport, auth=FakeAuth())


class TestSingleLevel:
    @respx.mock
    def test_root_children_one_request(self, downloader: ZipperDownloader) -> None:
        routes = install_nodes_routes(respx.mock)
        nodes = downloader.list(PRODUCT)
        assert sorted(n.path for n in nodes) == sorted(safe_tree.ROOT_CHILDREN)
        assert routes[""].call_count == 1
        assert sum(r.call_count for r in routes.values()) == 1  # exactly one request

    @respx.mock
    def test_subdirectory_addressing_quoted(self, downloader: ZipperDownloader) -> None:
        routes = install_nodes_routes(respx.mock)
        directory = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m"
        nodes = downloader.list(PRODUCT, path=directory)
        assert sorted(n.name for n in nodes) == ["T34UEE_B04_10m.jp2", "T34UEE_B08_10m.jp2"]
        request_url = unquote(str(routes[directory].calls.last.request.url))
        assert "Nodes('GRANULE')/Nodes(" in request_url  # every segment key-quoted


class TestAdversarialNames:
    @respx.mock
    def test_names_with_spaces_parens_quotes(self, downloader: ZipperDownloader) -> None:
        """The OData-key-encoding regression test (SPEC §6.6)."""
        weird_dir = "S2B_MSIL2A (1).SAFE"
        weird_file = "o'brien report.xml"
        root_url = f"{BASE}/odata/v1/Products(uuid-safe)/Nodes"
        respx.get(url__eq=root_url).mock(
            return_value=httpx.Response(
                200,
                json={"result": [{"Name": weird_dir, "ChildrenNumber": 1, "ContentLength": 0}]},
            )
        )
        # ' -> '' (OData), space -> %20, parens kept, quotes around the literal
        child_url = f"{root_url}('S2B_MSIL2A%20(1).SAFE')/Nodes"
        child = respx.get(url__eq=child_url).mock(
            return_value=httpx.Response(
                200,
                json={"result": [{"Name": weird_file, "ChildrenNumber": 0, "ContentLength": 42}]},
            )
        )
        nodes = downloader.list(PRODUCT, path=weird_dir)
        assert child.call_count == 1
        assert nodes[0].name == weird_file
        assert nodes[0].path == f"{weird_dir}/{weird_file}"

    def test_quote_doubling_in_url(self, downloader: ZipperDownloader) -> None:
        url = downloader._nodes_url(PRODUCT, "o'brien")
        assert "('o''brien')" in unquote(url)


class TestRecursive:
    @respx.mock
    def test_bfs_one_request_per_directory(self, downloader: ZipperDownloader) -> None:
        routes = install_nodes_routes(respx.mock)
        nodes = downloader.list(PRODUCT, recursive=True)
        # every directory fetched exactly once
        assert all(route.call_count == 1 for route in routes.values())
        assert sum(route.call_count for route in routes.values()) == len(TREE)
        files = {n.path for n in nodes if not n.is_dir}
        assert files == set(safe_tree.FILES)

    @respx.mock
    def test_non_recursive_never_fetches_children(self, downloader: ZipperDownloader) -> None:
        routes = install_nodes_routes(respx.mock)
        downloader.list(PRODUCT, recursive=False)
        child_requests = sum(r.call_count for path, r in routes.items() if path)
        assert child_requests == 0


class TestCrossBackendParity:
    """SPEC §6.6 exit criterion: identical Node lists from Zipper and Exos."""

    @respx.mock
    def test_zipper_equals_exos_tree(self, downloader: ZipperDownloader) -> None:
        install_nodes_routes(respx.mock)
        zipper_nodes = {
            (n.path, n.name, n.is_dir, n.size) for n in downloader.list(PRODUCT, recursive=True)
        }

        import boto3
        from moto import mock_aws
        from pydantic import SecretStr

        from eosdk.auth.s3_keys import S3Credentials
        from eosdk.eodata.exos import ExosDownloader

        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket=safe_tree.BUCKET)
            for logical, content in safe_tree.FILES.items():
                client.put_object(
                    Bucket=safe_tree.BUCKET, Key=f"{safe_tree.PREFIX}/{logical}", Body=content
                )
            exos = ExosDownloader(
                "https://s3.us-east-1.amazonaws.com",
                region="us-east-1",
                credentials=S3Credentials(key_id="k", access_key="AK", secret_key=SecretStr("SK")),
            )
            exos_product = PRODUCT.model_copy(update={"s3_path": safe_tree.S3_PATH})
            exos_nodes = {
                (n.path, n.name, n.is_dir, n.size) for n in exos.list(exos_product, recursive=True)
            }

        assert zipper_nodes == exos_nodes
