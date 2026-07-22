"""CLI verbs that mirror read/inspect Client methods: get, collections,
queryables, list, cat. Each is a thin wrapper, so these check wiring and
output shape, not backend behaviour (covered in tests/catalogue and tests/eodata)."""

import json

import httpx
import respx

from tests.cli.conftest import Invoke
from tests.conftest import CATALOGUE, EODATA_HTTP


def login(invoke: Invoke) -> None:
    invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")


class TestGet:
    def test_get_table(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        # get() POSTs to /search (mocked to return PRODUCT_A) and prints its fields.
        login(invoke)
        result = invoke("get", "PRODUCT_A")
        assert result.exit_code == 0, result.output
        assert "PRODUCT_A" in result.output

    def test_get_json(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        login(invoke)
        result = invoke("get", "PRODUCT_A", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["name"] == "PRODUCT_A"


class TestCollections:
    def _mock(self, router: respx.Router) -> None:
        router.get(f"{CATALOGUE}/collections").mock(
            return_value=httpx.Response(
                200,
                json={
                    "collections": [
                        {"id": "sentinel-2-l2a", "title": "Sentinel-2 L2A"},
                        {"id": "sentinel-1-grd", "title": "Sentinel-1 GRD"},
                    ],
                    "links": [],
                },
            )
        )

    def test_collections_table(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        self._mock(platform_mocks)
        login(invoke)
        result = invoke("collections")
        assert result.exit_code == 0, result.output
        assert "sentinel-2-l2a" in result.output
        assert "2 collection(s)" in result.output

    def test_collections_json(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        self._mock(platform_mocks)
        login(invoke)
        result = invoke("collections", "--json")
        assert result.exit_code == 0, result.output
        assert [c["id"] for c in json.loads(result.output)] == [
            "sentinel-2-l2a",
            "sentinel-1-grd",
        ]


class TestQueryables:
    def test_queryables_table(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        platform_mocks.get(f"{CATALOGUE}/collections/sentinel-2-l2a/queryables").mock(
            return_value=httpx.Response(
                200,
                json={
                    "properties": {
                        "eo:cloud_cover": {"type": "number"},
                        "datetime": {"type": "string", "format": "date-time"},
                    }
                },
            )
        )
        login(invoke)
        result = invoke("queryables", "sentinel-2-l2a")
        assert result.exit_code == 0, result.output
        assert "eo:cloud_cover" in result.output


class TestList:
    def test_list_http_root(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        platform_mocks.get(f"{EODATA_HTTP}/odata/v1/Products(uuid-a)/Nodes").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": [
                        {"Id": "uuid-a/MTD.xml", "Name": "MTD.xml", "ContentLength": 1234},
                        {"Id": "uuid-a/GRANULE", "Name": "GRANULE", "ChildrenNumber": 3},
                    ]
                },
            )
        )
        login(invoke)
        result = invoke("list", "uuid-a", "--via", "http")
        assert result.exit_code == 0, result.output
        assert "MTD.xml" in result.output
        assert "GRANULE" in result.output


class TestCat:
    def test_cat_over_http_is_capability_refused(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        # ranged reads need the s3 backend; asking http refuses before any network.
        login(invoke)
        result = invoke("cat", "uuid-a", "some/file.jp2", "--via", "http")
        assert result.exit_code == 1
        assert "error" in result.output.lower()

    def test_cat_s3_path_rejected_over_http(self, invoke: Invoke) -> None:
        # an s3 path only works with --via s3 (shared _refs guard)
        result = invoke("cat", "s3://eodata/x/y", "file", "--via", "http")
        assert result.exit_code != 0
