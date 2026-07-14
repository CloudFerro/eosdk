import json

import httpx
import respx

from tests.cli.conftest import Invoke
from tests.conftest import EXOS_ENDPOINT, ZIPPER


def install_ready_routes(router: respx.Router, status: int = 200) -> None:
    router.get(f"{ZIPPER}/ready").mock(return_value=httpx.Response(status))
    router.get(f"{EXOS_ENDPOINT}/ready").mock(return_value=httpx.Response(status))


class TestDoctor:
    def test_all_reachable_exit_zero(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        from moto import mock_aws

        from tests.cli.test_keys_cmd import install_keys_routes

        platform_mocks.head(ZIPPER).mock(return_value=httpx.Response(200))
        install_ready_routes(platform_mocks)
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
        install_ready_routes(platform_mocks)
        result = invoke("doctor")
        assert result.exit_code == 1
        assert "✗" in result.output
        assert "hint" in result.output

    def test_eodata_not_ready_fails_without_retries(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        platform_mocks.head(ZIPPER).mock(return_value=httpx.Response(200))
        ready = platform_mocks.get(f"{ZIPPER}/ready").mock(return_value=httpx.Response(503))
        platform_mocks.get(f"{EXOS_ENDPOINT}/ready").mock(return_value=httpx.Response(200))
        result = invoke("doctor")
        assert result.exit_code == 1
        assert "not ready" in result.output
        # 503 means "not ready", an answer — the transport backoff loop must not kick in
        assert ready.call_count == 1

    def test_ready_probe_rate_limited_across_runs(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        platform_mocks.head(ZIPPER).mock(return_value=httpx.Response(200))
        install_ready_routes(platform_mocks)
        invoke("doctor")
        invoke("doctor")
        ready = platform_mocks.get(f"{ZIPPER}/ready")
        assert ready.call_count == 1  # second run served from the on-disk verdict
        result = invoke("doctor", "--json")
        sections = json.loads(result.output)
        services = next(s for s in sections if s["section"] == "Services")
        zipper_ready = next(r for r in services["results"] if r["name"] == "Zipper eodata")
        assert zipper_ready["ok"] is True
        assert "cached" in zipper_ready["detail"]

    def test_force_probes_live_despite_fresh_cache(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        platform_mocks.head(ZIPPER).mock(return_value=httpx.Response(200))
        install_ready_routes(platform_mocks)
        invoke("doctor")
        result = invoke("doctor", "--force", "--json")
        ready = platform_mocks.get(f"{ZIPPER}/ready")
        assert ready.call_count == 2
        sections = json.loads(result.output)
        services = next(s for s in sections if s["section"] == "Services")
        zipper_ready = next(r for r in services["results"] if r["name"] == "Zipper eodata")
        assert "cached" not in zipper_ready["detail"]  # live verdict, not the stored one

    def test_odata_error_status_fails(
        self, invoke: Invoke, platform_mocks: respx.Router, monkeypatch
    ) -> None:
        """A non-2xx from the OData Products endpoint must report ✗, not ✓."""
        odata = "https://odata.example.eu"
        monkeypatch.setenv("EOSDK_CATALOGUE_ODATA_URL", odata)
        install_ready_routes(platform_mocks)
        platform_mocks.get(f"{odata}/odata/v1/Products").mock(return_value=httpx.Response(404))
        result = invoke("doctor", "--json")
        sections = json.loads(result.output)
        services = next(s for s in sections if s["section"] == "Services")
        check = next(r for r in services["results"] if r["name"] == "OData catalogue")
        assert check["ok"] is False
        assert "404" in check["detail"]
        assert result.exit_code == 1

    def test_odata_reachable_ok(
        self, invoke: Invoke, platform_mocks: respx.Router, monkeypatch
    ) -> None:
        odata = "https://odata.example.eu"
        monkeypatch.setenv("EOSDK_CATALOGUE_ODATA_URL", odata)
        install_ready_routes(platform_mocks)
        platform_mocks.get(f"{odata}/odata/v1/Products").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        result = invoke("doctor", "--json")
        sections = json.loads(result.output)
        services = next(s for s in sections if s["section"] == "Services")
        check = next(r for r in services["results"] if r["name"] == "OData catalogue")
        assert check["ok"] is True

    def test_exos_rejected_key_hints_keys_manager_not_endpoint(
        self, invoke: Invoke, platform_mocks: respx.Router, monkeypatch
    ) -> None:
        """InvalidAccessKeyId is a credentials problem (e.g. too many keys in the
        keys manager) — the hint must point there, not at the endpoint."""
        from botocore.exceptions import ClientError

        from eosdk.eodata.exos import ExosDownloader

        platform_mocks.head(ZIPPER).mock(return_value=httpx.Response(200))
        install_ready_routes(platform_mocks)

        def reject(self: ExosDownloader) -> None:
            raise ClientError(
                {"Error": {"Code": "InvalidAccessKeyId", "Message": "Unknown"}}, "ListBuckets"
            )

        monkeypatch.setattr(ExosDownloader, "_s3", reject)
        result = invoke("doctor", "--json")
        sections = json.loads(result.output)
        services = next(s for s in sections if s["section"] == "Services")
        check = next(r for r in services["results"] if r["name"] == "Exos (S3)")
        assert check["ok"] is False
        assert "InvalidAccessKeyId" in check["detail"]
        assert "eo keys" in check["hint"]
        assert "exos_endpoint" not in check["hint"]
        assert result.exit_code == 1

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

    def test_stac_version_segment_not_flagged(
        self, invoke_bare: Invoke, platform_mocks: respx.Router
    ) -> None:
        """The STAC URL is a self-describing landing page — /v1 there is valid."""
        import os

        os.environ["EOSDK_CATALOGUE_STAC_URL"] = "https://stac.example.eu/v1"
        try:
            result = invoke_bare("doctor", "--json")
        finally:
            del os.environ["EOSDK_CATALOGUE_STAC_URL"]
        sections = json.loads(result.output)
        config = next(s for s in sections if s["section"] == "Config")
        stac = next(r for r in config["results"] if r["name"] == "catalogue_stac URL")
        assert stac["ok"] is True
