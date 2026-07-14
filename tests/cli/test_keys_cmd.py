import json
import shlex

import httpx
import respx

from tests.cli.conftest import Invoke
from tests.conftest import KEYS_MANAGER

SECRET = "SUPER-SECRET-KEY-MATERIAL"
ACCESS_ID = "AKIAEXAMPLE"
FOREIGN_ACCESS_ID = "AKIAFOREIGN"  # created outside eosdk: no local secret
CREDENTIALS_URL = f"{KEYS_MANAGER}/credentials"


def install_keys_routes(router: respx.Router, *, include_foreign: bool = False) -> None:
    router.post(CREDENTIALS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_id": ACCESS_ID,
                "secret": SECRET,
                "expiration_date": "2027-07-08T00:00:00Z",
            },
        )
    )
    credentials = [
        {
            "access_id": ACCESS_ID,
            "user_name": "alice",
            "organization": "org-1",
            "expiration_date": "2027-07-08T00:00:00Z",
        }
    ]
    if include_foreign:
        credentials.append(
            {
                "access_id": FOREIGN_ACCESS_ID,
                "user_name": "alice",
                "organization": "org-1",
                "expiration_date": "2027-07-08T00:00:00Z",
            }
        )
    router.get(url__startswith=CREDENTIALS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "credentials": credentials,
                "count": len(credentials),
                "offset": 0,
                "limit": 100,
            },
        )
    )
    router.delete(f"{CREDENTIALS_URL}/access_id/{ACCESS_ID}").mock(return_value=httpx.Response(204))


def login(invoke: Invoke) -> None:
    invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")


class TestCreate:
    def test_secret_hidden_by_default(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        result = invoke("keys", "create", "--label", "my-pipeline")
        assert result.exit_code == 0, result.output
        assert SECRET not in result.output  # the security regression test
        assert ACCESS_ID in result.output  # access id is public, shown in full

    def test_export_emits_eval_safe_env(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        result = invoke("keys", "create", "--label", "my-pipeline", "--export")
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if "=" in line]
        parsed = dict(pair.split("=", 1) for pair in lines)
        assert parsed["AWS_ACCESS_KEY_ID"] == ACCESS_ID
        assert parsed["AWS_SECRET_ACCESS_KEY"] == SECRET
        assert "AWS_ENDPOINT_URL" in parsed
        for line in lines:  # eval-safe: shlex round-trips each assignment
            assert shlex.split(line) == [line]


class TestListAndRevoke:
    def test_list_json_has_no_secret(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        result = invoke("keys", "list", "--json")
        assert result.exit_code == 0, result.output
        assert ACCESS_ID in result.output  # ids are fine in --json
        assert SECRET not in result.output

    def test_list_table_shows_full_access_key(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        result = invoke("keys", "list")
        assert result.exit_code == 0
        assert ACCESS_ID in result.output  # full id: needed to copy into `eo keys revoke`
        assert SECRET not in result.output

    def test_list_marks_locally_stored_secrets(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        install_keys_routes(platform_mocks, include_foreign=True)
        login(invoke)
        invoke("keys", "create", "--label", "my-pipeline")
        result = invoke("keys", "list", "--json")
        assert result.exit_code == 0, result.output
        by_id = {entry["access_id"]: entry for entry in json.loads(result.output)}
        assert by_id[ACCESS_ID]["label"] == "my-pipeline"
        assert by_id[ACCESS_ID]["local_secret"] is True
        assert by_id[FOREIGN_ACCESS_ID]["local_secret"] is False
        assert by_id[FOREIGN_ACCESS_ID]["label"] is None
        table = invoke("keys", "list")
        assert "my-pipeline" in table.output  # shown in the "local secret" column
        assert SECRET not in table.output

    def test_revoke_prompts_without_yes(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        aborted = invoke("keys", "revoke", ACCESS_ID, input="n\n")
        assert aborted.exit_code != 0
        confirmed = invoke("keys", "revoke", ACCESS_ID, "--yes")
        assert confirmed.exit_code == 0, confirmed.output
        assert "revoked" in confirmed.output
