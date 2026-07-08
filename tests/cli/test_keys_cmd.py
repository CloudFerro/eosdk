import re
import shlex

import httpx
import respx

from tests.cli.conftest import Invoke
from tests.conftest import KEYS_MANAGER

SECRET = "SUPER-SECRET-KEY-MATERIAL"


def install_keys_routes(router: respx.Router) -> None:
    router.post(f"{KEYS_MANAGER}/v1/keys").mock(
        return_value=httpx.Response(
            200,
            json={
                "key_id": "kid-1",
                "access_key": "AKIAEXAMPLE",
                "secret_key": SECRET,
                "label": "my-pipeline",
                "created_at": "2026-07-08T09:00:00Z",
            },
        )
    )
    router.get(f"{KEYS_MANAGER}/v1/keys").mock(
        return_value=httpx.Response(
            200,
            json={
                "keys": [
                    {
                        "key_id": "kid-1",
                        "access_key": "AKIAEXAMPLE",
                        "label": "my-pipeline",
                        "created_at": "2026-07-08T09:00:00Z",
                    }
                ]
            },
        )
    )
    router.delete(f"{KEYS_MANAGER}/v1/keys/kid-1").mock(return_value=httpx.Response(204))


def login(invoke: Invoke) -> None:
    invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")


class TestCreate:
    def test_masked_by_default(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        result = invoke("keys", "create", "--label", "my-pipeline")
        assert result.exit_code == 0, result.output
        assert SECRET not in result.output  # the security regression test
        assert not re.search(r"AKIAEXAMPLE", result.output)

    def test_export_emits_eval_safe_env(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        result = invoke("keys", "create", "--label", "my-pipeline", "--export")
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if "=" in line]
        parsed = dict(pair.split("=", 1) for pair in lines)
        assert parsed["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
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
        assert "kid-1" in result.output
        assert SECRET not in result.output

    def test_list_table_masks_access_key(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        result = invoke("keys", "list")
        assert result.exit_code == 0
        assert "AKIAEXAMPLE" not in result.output
        assert "MPLE" in result.output  # masked suffix

    def test_revoke_prompts_without_yes(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        install_keys_routes(platform_mocks)
        login(invoke)
        aborted = invoke("keys", "revoke", "kid-1", input="n\n")
        assert aborted.exit_code != 0
        confirmed = invoke("keys", "revoke", "kid-1", "--yes")
        assert confirmed.exit_code == 0, confirmed.output
        assert "revoked" in confirmed.output
