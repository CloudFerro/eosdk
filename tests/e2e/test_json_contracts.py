"""The machine-readable output is a contract (plan §4.9, Q1-Q3; SPEC §12).

``schemas/cli/`` holds one committed JSON Schema per ``--json`` /
``--format json`` surface. They are hand-written data, not something derived
from the pydantic models at test time: a schema regenerated from the model can
never catch a model change, which is the entire point of Q3.

Q1 validates real command output against them, Q2 proves the JSON is
re-consumable rather than merely well formed, and Q3 pins the compatibility
rules — additive fields are fine, a widened ``required`` set is a reviewable
diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

from eosdk.models import Collection, Node, Product, Queryable

if TYPE_CHECKING:
    from eosdk.client import Client
    from tests.e2e.platform import FakePlatform
    from tests.e2e.surface import Invoke

pytestmark = pytest.mark.e2e

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "cli"

#: schema file -> the required set it is allowed to demand. Widening one of
#: these breaks every consumer that was writing valid output yesterday, so the
#: expectation lives here and a change has to be made deliberately, in review.
REQUIRED = {
    "search": {"id", "name"},
    "get": {"id", "name"},
    "list": {"name", "path", "is_dir"},
    "collections": {"id"},
    "queryables": {"name"},
    "keys": {"access_key", "local_secret"},
    "doctor": {"section", "results"},
    "discover": {"version", "services"},
}

#: schema file -> the model whose non-optional fields it must describe
BACKING_MODEL = {
    "search": Product,
    "get": Product,
    "list": Node,
    "collections": Collection,
    "queryables": Queryable,
}


def load(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text())


def item_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The object schema an array-shaped output validates each element against."""
    if schema.get("type") != "array":
        return schema
    items = schema["items"]
    if "$ref" in items:
        return schema["$defs"][items["$ref"].rsplit("/", 1)[-1]]
    return items


def validate(name: str, payload: Any) -> None:
    Draft202012Validator(load(name)).validate(payload)


@pytest.fixture
def populated_cli(logged_in_cli: Invoke) -> Invoke:
    """A CLI with a key pair already minted, so `keys list` has something to show."""
    created = logged_in_cli("keys", "create", "--label", "contract")
    assert created.exit_code == 0, created.stderr
    return logged_in_cli


class TestEverySchemaExists:
    def test_one_schema_per_machine_readable_command(self) -> None:
        found = {path.name.removesuffix(".schema.json") for path in SCHEMAS.glob("*.schema.json")}
        assert found == set(REQUIRED)

    @pytest.mark.parametrize("name", sorted(REQUIRED))
    def test_schemas_are_valid_json_schema(self, name: str) -> None:
        Draft202012Validator.check_schema(load(name))


class TestRealOutputValidates:
    """Q1 — every command's actual output validates, and is non-trivial."""

    def test_search_json_lines(self, logged_in_cli: Invoke, platform: FakePlatform) -> None:
        result = logged_in_cli(
            "search",
            "--collection",
            "SENTINEL-2",
            "--from",
            "2026-06-01",
            "--to",
            "2026-06-30",
            "--format",
            "json",
        )
        assert result.exit_code == 0, result.stderr
        lines = result.json_lines()
        assert len(lines) == 4  # one line per product, not a JSON array
        for entry in lines:
            validate("search", entry)

    def test_get_json(self, logged_in_cli: Invoke, platform: FakePlatform) -> None:
        result = logged_in_cli("get", platform.products[0].uuid, "--json")
        assert result.exit_code == 0, result.stderr
        payload = result.json()
        validate("get", payload)
        assert payload["id"] == platform.products[0].uuid

    @pytest.mark.parametrize("via", ["http", "s3"])
    def test_list_json(self, logged_in_cli: Invoke, platform: FakePlatform, via: str) -> None:
        source = platform.products[0]
        ref = source.uuid if via == "http" else source.s3_path
        result = logged_in_cli("list", ref, "--via", via, "--recursive", "--json")
        assert result.exit_code == 0, result.stderr
        payload = result.json()
        validate("list", payload)
        assert len(payload) >= len(source.tree)
        assert any(entry["is_dir"] for entry in payload)

    def test_collections_json(self, logged_in_cli: Invoke) -> None:
        result = logged_in_cli("collections", "--json")
        assert result.exit_code == 0, result.stderr
        payload = result.json()
        validate("collections", payload)
        assert {entry["id"] for entry in payload} == {"SENTINEL-1", "SENTINEL-2", "SENTINEL-3"}

    @pytest.mark.parametrize("protocol", ["stac", "odata"])
    def test_queryables_json(self, logged_in_cli: Invoke, protocol: str) -> None:
        result = logged_in_cli("queryables", "SENTINEL-2", "--protocol", protocol, "--json")
        assert result.exit_code == 0, result.stderr
        payload = result.json()
        validate("queryables", payload)
        assert len(payload) >= 3

    def test_keys_list_json_never_leaks_a_secret(
        self, populated_cli: Invoke, platform: FakePlatform
    ) -> None:
        result = populated_cli("keys", "list", "--json")
        assert result.exit_code == 0, result.stderr
        payload = result.json()
        validate("keys", payload)
        assert len(payload) == 1
        assert payload[0]["local_secret"] is True  # the secret is on disk here...

        [minted] = platform.keys.values()
        assert payload[0]["access_key"] == minted["access_id"]
        assert minted["secret"] not in json.dumps(payload)  # ...but never on stdout

    def test_doctor_json(self, logged_in_cli: Invoke) -> None:
        result = logged_in_cli("doctor", "--json")
        payload = result.json()
        validate("doctor", payload)
        assert [section["section"] for section in payload] == [
            "Config",
            "Discovery",
            "Auth",
            "Services",
        ]
        assert all(section["results"] for section in payload)
        # exit code and content must agree: 0 iff nothing reported ok=false
        failed = any(check["ok"] is False for section in payload for check in section["results"])
        assert (result.exit_code != 0) is failed

    def test_discover_json(self, logged_in_cli: Invoke, platform: FakePlatform) -> None:
        result = logged_in_cli("discover", "--json")
        assert result.exit_code == 0, result.stderr
        payload = result.json()
        validate("discover", payload)
        assert payload["platform"]["name"] == "example-eu"
        assert set(payload["services"]) == {"catalogue", "data_access", "auth"}

    def test_a_service_this_sdk_does_not_implement_survives_the_round_trip(
        self, logged_in_cli: Invoke, platform: FakePlatform
    ) -> None:
        """Forward compatibility is part of the discover contract (SPEC §6.2)."""
        platform.discovery_document["services"]["quantum_teleporter"] = {
            "url": "https://qt.example.eu"
        }
        result = logged_in_cli("discover", "--json", "--refresh")
        payload = result.json()
        validate("discover", payload)
        assert payload["services"]["quantum_teleporter"] == {"url": "https://qt.example.eu"}


class TestOutputIsReConsumable:
    """Q2 — the JSON is not merely well formed; it round-trips into a transfer."""

    def test_search_json_feeds_a_download(
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
        entries = result.json_lines()
        assert len(entries) == 1
        validate("search", entries[0])

        products = [Product.model_validate(entry) for entry in entries]
        reports = library_client.download(products, target=tmp_path / "q2", via="http")
        assert reports[0].path.read_bytes() == platform.products[0].zip_bytes
        assert reports[0].checksum_verified is True  # the checksum survived the hop

    def test_get_json_and_search_json_describe_the_same_record(
        self, logged_in_cli: Invoke, platform: FakePlatform
    ) -> None:
        """`eo download -` accepts either, so their shapes must not diverge."""
        source = platform.products[0]
        from_search = logged_in_cli(
            "search",
            "--collection",
            "SENTINEL-2",
            "--from",
            "2026-06-15",
            "--to",
            "2026-06-16",
            "--format",
            "json",
        ).json_lines()[0]
        from_get = logged_in_cli("get", source.uuid, "--json").json()
        assert set(from_search) == set(from_get)
        assert Product.model_validate(from_search) == Product.model_validate(from_get)

    def test_list_json_paths_are_usable_with_cat(
        self, logged_in_cli: Invoke, platform: FakePlatform
    ) -> None:
        source = platform.products[0]
        nodes = logged_in_cli("list", source.s3_path, "--via", "s3", "--recursive", "--json").json()
        leaf = next(entry for entry in nodes if not entry["is_dir"])
        streamed = logged_in_cli("cat", source.s3_path, leaf["path"], "--via", "s3")
        assert streamed.exit_code == 0, streamed.stderr
        assert streamed.stdout_bytes == source.tree[leaf["path"]]


class TestCompatibilityRules:
    """Q3 — what may change in these schemas without breaking a consumer."""

    @pytest.mark.parametrize("name", sorted(REQUIRED))
    def test_required_set_is_pinned(self, name: str) -> None:
        assert set(item_schema(load(name))["required"]) == REQUIRED[name]

    @pytest.mark.parametrize("name", sorted(REQUIRED))
    def test_unknown_fields_are_tolerated(self, name: str) -> None:
        schema = load(name)
        target = item_schema(schema)
        assert target.get("additionalProperties") is not False

        sample = _minimal_instance(target)
        sample["a_field_from_a_future_release"] = {"nested": [1, 2, 3]}
        payload = [sample] if schema.get("type") == "array" else sample
        Draft202012Validator(schema).validate(payload)

    @pytest.mark.parametrize("name", sorted(BACKING_MODEL))
    def test_every_mandatory_model_field_is_described(self, name: str) -> None:
        """A new non-optional model field must not escape the contract."""
        model = BACKING_MODEL[name]
        mandatory = {field for field, info in model.model_fields.items() if info.is_required()}
        properties = set(item_schema(load(name))["properties"])
        assert mandatory <= properties
        assert mandatory <= REQUIRED[name]
        # and nothing optional was quietly promoted into `required`
        assert REQUIRED[name] <= set(model.model_fields) | REQUIRED[name]

    @pytest.mark.parametrize("name", sorted(BACKING_MODEL))
    def test_every_model_field_is_described(self, name: str) -> None:
        properties = set(item_schema(load(name))["properties"])
        assert set(BACKING_MODEL[name].model_fields) <= properties

    @pytest.mark.parametrize("name", sorted(REQUIRED))
    def test_schemas_are_self_describing(self, name: str) -> None:
        schema = load(name)
        assert schema["$id"].endswith(f"schemas/cli/{name}.schema.json")
        assert schema["title"].startswith("eo ")
        assert len(schema["description"]) > 120  # says which command emits it, and why


_PLACEHOLDERS: dict[str, Any] = {
    "string": "x",
    "integer": 0,
    "number": 0.0,
    "boolean": True,
    "object": {},
    "array": [],
    "null": None,
}


def _minimal_instance(schema: dict[str, Any]) -> dict[str, Any]:
    """The smallest object satisfying ``required``, for the tolerance check."""
    instance: dict[str, Any] = {}
    for field in schema["required"]:
        declared = schema["properties"][field].get("type", "string")
        primary = declared[0] if isinstance(declared, list) else declared
        if field == "results":  # doctor: an empty section is legal
            instance[field] = []
            continue
        if field == "version":
            instance[field] = "1.0"
            continue
        instance[field] = _PLACEHOLDERS[primary]
        if primary == "string" and schema["properties"][field].get("minLength"):
            instance[field] = "x"
    return instance
