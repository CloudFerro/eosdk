import json
import runpy
import sys
import types
from pathlib import Path

import httpx
import pytest
import respx

from eosdk.models import Product
from tests.cli.conftest import Invoke
from tests.conftest import FIXTURES, KEYCLOAK, PAYLOAD

DEVICE_URL = f"{KEYCLOAK}/realms/eodata/protocol/openid-connect/auth/device"


class TestAuth:
    def test_login_password_path(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        result = invoke(
            "auth", "login", "--username", "alice", "--password-stdin", input="s3cret\n"
        )
        assert result.exit_code == 0, result.output
        assert "Logged in" in result.output
        token_calls = [c for c in platform_mocks.calls if c.request.url.path.endswith("/token")]
        assert b"username=alice" in token_calls[-1].request.content

    def test_device_flow_is_default_and_prints_code(
        self, invoke: Invoke, platform_mocks: respx.Router, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eosdk.auth.keycloak import KeycloakAuth

        # the flow sleeps `interval` seconds before each poll; don't in tests
        monkeypatch.setitem(KeycloakAuth.__init__.__kwdefaults__, "sleep", lambda _s: None)
        platform_mocks.post(DEVICE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "device_code": "DEV123",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth.example.eu/device",
                    "verification_uri_complete": (
                        "https://auth.example.eu/device?user_code=ABCD-EFGH"
                    ),
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        )
        result = invoke("auth", "login")  # no --username -> device flow
        assert result.exit_code == 0, result.output
        assert "Device login" in result.output
        assert "ABCD-EFGH" in result.output
        assert "Logged in" in result.output

    def test_password_login_prompts_for_username(
        self, invoke: Invoke, platform_mocks: respx.Router
    ) -> None:
        # without a device endpoint the realm forces the username/password path
        oidc = json.loads((FIXTURES / "keycloak_openid_configuration.json").read_text())
        del oidc["device_authorization_endpoint"]
        platform_mocks.get(f"{KEYCLOAK}/realms/eodata/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=oidc)
        )
        result = invoke("auth", "login", "--password", "pw", input="alice\n")
        assert result.exit_code == 0, result.output
        assert "Username" in result.output
        token_calls = [c for c in platform_mocks.calls if c.request.url.path.endswith("/token")]
        assert b"username=alice" in token_calls[-1].request.content

    def test_status_exit_codes(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        assert invoke("auth", "status").exit_code == 1  # logged out -> pipeline-unfriendly
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("auth", "status")
        assert result.exit_code == 0
        assert "yes" in result.output

    def test_logout(self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        assert (tmp_path / "tokens" / "test.json").exists()
        assert invoke("auth", "logout").exit_code == 0
        assert not (tmp_path / "tokens" / "test.json").exists()


class TestSearch:
    def test_json_lines_round_trip(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke(
            "search",
            "--collection",
            "SENTINEL-2",
            "--bbox",
            "22.5,52.9,24.0,53.5",
            "--from",
            "2026-06-01",
            "--to",
            "2026-06-30",
            "--filter",
            "cloudCover=<20",
            "--format",
            "json",
        )
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.splitlines() if line.strip()]
        products = [Product.model_validate_json(line) for line in lines]
        assert [p.name for p in products] == ["PRODUCT_A", "PRODUCT_B"]

    def test_human_table(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("search", "--collection", "SENTINEL-2")
        assert result.exit_code == 0
        assert "PRODUCT_A" in result.output
        assert "2 product(s)" in result.output

    def test_format_id_feeds_download_args(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        """eo download $(eo search --format id ...) — no jq required."""
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("search", "--collection", "SENTINEL-2", "--format", "id")
        assert result.exit_code == 0, result.output
        ids = result.output.split()
        assert ids == ["uuid-a", "uuid-b"]

        out_dir = tmp_path / "data"
        download = invoke("download", *ids, "-o", str(out_dir))
        assert download.exit_code == 0, download.output
        assert sorted(p.name for p in out_dir.iterdir()) == ["uuid-a.zip", "uuid-b.zip"]

    def test_format_s3_prints_paths(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("search", "--collection", "SENTINEL-2", "--format", "s3")
        assert result.exit_code == 0, result.output
        paths = result.output.split()
        assert len(paths) == 2
        # the exact depth is fixture-shaped; the CLI just prints Product.s3_path
        assert all(p.startswith("s3://eodata/Sentinel-2/") for p in paths)

    def test_unknown_format_rejected(self, invoke: Invoke) -> None:
        result = invoke("search", "--collection", "X", "--format", "yaml")
        assert result.exit_code == 2
        assert "--format" in result.output

    def test_removed_json_flag_rejected(self, invoke: Invoke) -> None:
        result = invoke("search", "--collection", "X", "--json")
        assert result.exit_code == 2

    def test_bad_bbox_usage_error(self, invoke: Invoke) -> None:
        result = invoke("search", "--collection", "X", "--bbox", "1,2,3")
        assert result.exit_code == 2
        assert "bbox" in result.output

    def test_unbounded_search_refused(self, invoke: Invoke) -> None:
        result = invoke("search")
        assert result.exit_code == 1
        assert "unbounded" in result.output

    def test_non_numeric_bbox_usage_error(self, invoke: Invoke) -> None:
        result = invoke("search", "--collection", "X", "--bbox", "a,2,3,4")
        assert result.exit_code == 2
        assert "non-numeric" in result.output

    def test_filter_without_equals_rejected(self, invoke: Invoke) -> None:
        result = invoke("search", "--collection", "X", "--filter", "cloudCover")
        assert result.exit_code == 2
        assert "key=value" in result.output

    def test_format_s3_warns_about_missing_paths(
        self, invoke: Invoke, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eosdk.client import Client

        products = [
            Product(id="uuid-a", name="PRODUCT_A", s3_path="s3://eodata/x/PRODUCT_A.SAFE"),
            Product(id="uuid-b", name="PRODUCT_B"),  # no S3 path
        ]
        monkeypatch.setattr(Client, "search", lambda self, **kwargs: products)
        result = invoke("search", "--collection", "SENTINEL-2", "--format", "s3")
        assert result.exit_code == 0, result.output
        assert "s3://eodata/x/PRODUCT_A.SAFE" in result.output
        assert "skipped 1 product(s) without an S3 path" in result.output


class TestDownload:
    def test_pipe_search_into_download(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        """The CLI exit-criterion test: eo search --format json | eo download -"""
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        search_result = invoke("search", "--collection", "SENTINEL-2", "--format", "json")
        assert search_result.exit_code == 0

        out_dir = tmp_path / "data"
        result = invoke(
            "download", "-", "-o", str(out_dir), "--via", "http", input=search_result.output
        )
        assert result.exit_code == 0, result.output
        files = sorted(p.name for p in out_dir.iterdir())
        assert files == ["PRODUCT_A.zip", "PRODUCT_B.zip"]
        assert (out_dir / "PRODUCT_A.zip").read_bytes() == PAYLOAD

    def test_download_by_id(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        out_dir = tmp_path / "data"
        result = invoke("download", "uuid-a", "-o", str(out_dir))
        assert result.exit_code == 0, result.output
        assert (out_dir / "uuid-a.zip").read_bytes() == PAYLOAD

    def test_missing_product_exit_code(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        import httpx

        platform_mocks.get("https://download.example.eu/odata/v1/Products(uuid-nope)/$value").mock(
            return_value=httpx.Response(404)
        )
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("download", "uuid-nope", "-o", str(tmp_path / "d"))
        assert result.exit_code == 1
        assert "uuid-nope" in result.output

    def test_s3_path_arg_requires_via_s3(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("download", "s3://eodata/some/PRODUCT_A.SAFE", "-o", str(tmp_path / "d"))
        assert result.exit_code == 2
        assert "--via s3" in result.output

    def test_output_missing_value_is_rejected(self, invoke: Invoke) -> None:
        # `--output --via s3 ./data` must not download `s3` and `./data` as products.
        result = invoke("download", "uuid-a", "--output", "--via", "s3", "./data")
        assert result.exit_code == 2
        assert "missing its value" in result.output

    def test_relative_path_as_id_is_rejected(self, invoke: Invoke, tmp_path: Path) -> None:
        result = invoke("download", "./data", "-o", str(tmp_path / "d"), "--via", "s3")
        assert result.exit_code == 2
        assert "looks like a local path" in result.output

    def test_option_looking_id_is_rejected(self, invoke: Invoke) -> None:
        # `--` makes click pass the token through as a positional product id
        result = invoke("download", "--", "--oops")
        assert result.exit_code == 2
        assert "looks like an option, not a product id" in result.output

    def test_no_ids_is_usage_error(self, invoke: Invoke) -> None:
        result = invoke("download")
        assert result.exit_code == 2
        assert "give product ids or '-' for stdin" in result.output

    def test_empty_stdin_is_usage_error(self, invoke: Invoke) -> None:
        result = invoke("download", "-", input="")
        assert result.exit_code == 2
        assert "nothing on stdin" in result.output

    def test_s3_path_args_build_product_stubs(
        self, invoke: Invoke, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from eosdk.client import Client

        captured: dict[str, object] = {}

        def fake_download(self: Client, products: list[Product], **kwargs: object) -> list[object]:
            captured["products"] = list(products)
            captured["via"] = kwargs.get("via")
            return []

        monkeypatch.setattr(Client, "download", fake_download)
        result = invoke(
            "download",
            "s3://eodata/x/PRODUCT_A.SAFE",
            "/eodata/y/PRODUCT_B.SAFE/",
            "--via",
            "s3",
            "-o",
            str(tmp_path / "d"),
        )
        assert result.exit_code == 0, result.output
        assert captured["via"] == "s3"
        products = captured["products"]
        assert isinstance(products, list)
        assert [(p.id, p.name, p.s3_path) for p in products] == [
            ("PRODUCT_A.SAFE", "PRODUCT_A.SAFE", "s3://eodata/x/PRODUCT_A.SAFE"),
            ("PRODUCT_B.SAFE", "PRODUCT_B.SAFE", "/eodata/y/PRODUCT_B.SAFE/"),
        ]

    def test_interactive_progress_on_tty(
        self,
        invoke: Invoke,
        platform_mocks: respx.Router,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tty stderr flips on the Rich progress bar; the download still lands."""
        from eosdk.cli.commands import download as download_module

        monkeypatch.setattr(
            download_module,
            "sys",
            types.SimpleNamespace(stderr=types.SimpleNamespace(isatty=lambda: True)),
        )
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        out_dir = tmp_path / "data"
        result = invoke("download", "uuid-a", "-o", str(out_dir))
        assert result.exit_code == 0, result.output
        assert (out_dir / "uuid-a.zip").read_bytes() == PAYLOAD
        assert "done" in result.output

    def test_stdin_accepts_json_array(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        array = json.dumps([{"id": "uuid-a", "name": "PRODUCT_A"}])
        result = invoke("download", "-", "-o", str(tmp_path / "d"), input=array)
        assert result.exit_code == 0, result.output

    def test_start_message_says_how_to_stop(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("download", "uuid-a", "-o", str(tmp_path / "d"))
        assert result.exit_code == 0, result.output
        assert "press Ctrl+C to stop" in result.output

    def test_quiet_suppresses_stop_hint(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("download", "uuid-a", "-o", str(tmp_path / "d"), "--quiet")
        assert result.exit_code == 0, result.output
        assert "Ctrl+C" not in result.output

    def test_keyboard_interrupt_reports_progress_and_exits_130(
        self,
        invoke: Invoke,
        platform_mocks: respx.Router,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from eosdk.client import Client

        def interrupted(self: Client, *args: object, **kwargs: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(Client, "download", interrupted)
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        result = invoke("download", "uuid-a", "-o", str(tmp_path / "d"))
        assert result.exit_code == 130
        assert "stopped" in result.output
        assert "0 of 1 product(s)" in result.output


class TestEntryPoint:
    # runpy warns that the module is already imported; expected when re-running it
    @pytest.mark.filterwarnings("ignore:.*eosdk.cli.main.*:RuntimeWarning")
    def test_python_m_runs_the_app(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`python -m eosdk.cli.main --help` reaches the __main__ guard."""
        monkeypatch.setattr(sys, "argv", ["eo", "--help"])
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("eosdk.cli.main", run_name="__main__")
        assert excinfo.value.code == 0
        assert "search" in capsys.readouterr().out


class TestErrors:
    def test_friendly_error_no_traceback(self, invoke_bare: Invoke) -> None:
        result = invoke_bare("search", "--collection", "X")
        assert result.exit_code == 1
        assert "error:" in result.output
        assert "Traceback" not in result.output

    def test_verbose_shows_traceback(self, invoke_bare: Invoke) -> None:
        result = invoke_bare("-v", "search", "--collection", "X")
        assert result.exit_code != 0
        assert result.exception is not None
