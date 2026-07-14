import json
from pathlib import Path

import pytest
import respx

from eosdk.models import Product
from tests.cli.conftest import Invoke
from tests.conftest import PAYLOAD


class TestAuth:
    def test_login_password_path(self, invoke: Invoke, platform_mocks: respx.Router) -> None:
        result = invoke(
            "auth", "login", "--username", "alice", "--password-stdin", input="s3cret\n"
        )
        assert result.exit_code == 0, result.output
        assert "Logged in" in result.output
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
            "--json",
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

    def test_bad_bbox_usage_error(self, invoke: Invoke) -> None:
        result = invoke("search", "--collection", "X", "--bbox", "1,2,3")
        assert result.exit_code == 2
        assert "bbox" in result.output

    def test_unbounded_search_refused(self, invoke: Invoke) -> None:
        result = invoke("search")
        assert result.exit_code == 1
        assert "unbounded" in result.output


class TestDownload:
    def test_pipe_search_into_download(
        self, invoke: Invoke, platform_mocks: respx.Router, tmp_path: Path
    ) -> None:
        """The CLI exit-criterion test: eo search --json | eo download -"""
        invoke("auth", "login", "--username", "alice", "--password-stdin", input="pw\n")
        search_result = invoke("search", "--collection", "SENTINEL-2", "--json")
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
