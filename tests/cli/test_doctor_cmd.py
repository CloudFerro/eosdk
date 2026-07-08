import json

import httpx
import respx

from tests.cli.conftest import Invoke
from tests.conftest import ZIPPER


class TestDoctor:
    def test_all_reachable_exit_zero(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        from moto import mock_aws

        from tests.cli.test_keys_cmd import install_keys_routes

        platform_mocks.head(ZIPPER).mock(return_value=httpx.Response(200))
        install_keys_routes(platform_mocks)  # the exos probe mints keys on first use
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        with mock_aws():
            result = invoke("doctor")
        assert result.exit_code == 0, result.output
        assert "Config" in result.output
        assert "✓" in result.output

    def test_unreachable_service_fails_with_hint(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        platform_mocks.head(ZIPPER).mock(side_effect=httpx.ConnectError("refused"))
        result = invoke("doctor")
        assert result.exit_code == 1
        assert "✗" in result.output
        assert "hint" in result.output

    def test_nothing_configured_all_skips_exit_zero(self, invoke_bare: Invoke) -> None:
        result = invoke_bare("doctor")
        assert result.exit_code == 0, result.output
        assert "-" in result.output  # skips, not failures
        assert "✗" not in result.output

    def test_json_shape(self, invoke_bare: Invoke) -> None:
        result = invoke_bare("doctor", "--json")
        assert result.exit_code == 0
        sections = json.loads(result.output)
        names = [s["section"] for s in sections]
        assert names == ["Config", "Discovery", "Auth", "Services"]
        discovery = next(s for s in sections if s["section"] == "Discovery")
        assert all(r["ok"] is None for r in discovery["results"])  # skipped pre-Phase-3

    def test_version_segment_in_base_flagged(
        self, invoke_bare: Invoke, platform_mocks: respx.Router
    ) -> None:
        import os

        env_result = None
        os.environ["EOSDK_ZIPPER_URL"] = "https://zipper.example.eu/v1"
        try:
            env_result = invoke_bare("doctor")
        finally:
            del os.environ["EOSDK_ZIPPER_URL"]
        assert env_result is not None
        assert env_result.exit_code == 1
        assert "version segment" in env_result.output
