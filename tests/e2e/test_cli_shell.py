"""The installed ``eo`` in a subprocess (plan §4.8, C1-C6).

Everything else in this suite runs the CLI in-process through ``CliRunner``,
which cannot answer the questions a user actually hits: does the console script
work at all, do real file descriptors keep data and diagnostics apart, is
``eo search --format json | eo download -`` a working pipe, what exit code does
Ctrl+C produce, and does ``eo cat > file`` write bytes rather than text.

Those need a real process, so this is the one file that talks to a socket:
``tests/e2e/httpstub.py`` serves the same FakePlatform over ``http.server`` and
``moto.server`` provides S3.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.e2e.httpstub import s3_server, seed_remote_s3, serve
from tests.e2e.platform import FakeProduct, default_collections, default_products

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from tests.e2e.platform import FakePlatform

# Only `e2e`: this file is hermetic (a local http.server plus moto's standalone
# S3), so it belongs in the default job. It is deliberately *not* `slow` —
# that marker means "transfers real data" and both nightly jobs select it with
# `-m "smoke or smoke_auth or slow"`, which would drag this suite into the
# live-platform run.
pytestmark = pytest.mark.e2e

TIMEOUT = 60
B04 = "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"

#: big enough that streaming it takes several chunks, so C3 has a window in
#: which to deliver SIGINT without racing the transfer to completion
SLOW_PRODUCT = FakeProduct(
    uuid="77777777-7777-4777-8777-777777777777",
    name="S2B_MSIL2A_20260625T095029_N0511_R079_T34UEZ_20260625T105512.SAFE",
    datetime="2026-06-25T09:50:29Z",
    attributes={"cloudCover": 1.0, "productType": "S2MSI2A"},
    tree={"big.dat": b"0123456789abcdef" * 65536},  # 1 MiB, incompressible enough
)


def eo_command() -> list[str]:
    """The installed console script; that is what the wheel actually ships."""
    script = shutil.which("eo") or str(Path(sys.executable).parent / "eo")
    if Path(script).exists():
        return [script]
    # Fallback for an environment where the script was not linked (e.g. a bare
    # `pip install -e .` without scripts); the app object is the same.
    return [sys.executable, "-c", "from eosdk.cli.main import app; app()"]


@pytest.fixture(scope="module")
def stub() -> Iterator[tuple[FakePlatform, str, Any]]:
    """One server pair for the whole module: starting them is the slow part."""
    with s3_server() as s3_endpoint:
        products: Sequence[FakeProduct] = [*default_products(), SLOW_PRODUCT]
        with serve(products, default_collections(), s3_endpoint=s3_endpoint) as (
            platform,
            base_url,
            server,
        ):
            seed_remote_s3(platform)
            yield platform, base_url, server


@pytest.fixture
def env(stub: tuple[FakePlatform, str, Any], tmp_path: Path) -> dict[str, str]:
    """The subprocess environment, with every ``EOSDK_*`` inherited pin removed.

    Endpoint env vars outrank the profile in the config precedence chain
    (SPEC §6.1), so a developer who exports ``EOSDK_CATALOGUE_STAC_URL`` or
    ``EOSDK_EODATA_HTTP_URL`` for their own work would otherwise send this
    suite at a real host. Scrubbing the whole namespace and re-adding only what
    the stub needs is what keeps these tests hermetic.
    """
    _, base_url, _ = stub
    config_home = tmp_path / "xdg"
    config_home.mkdir()
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("EOSDK_")},
        "EOSDK_PLATFORM": base_url,
        "XDG_CONFIG_HOME": str(config_home),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def run(
    env: dict[str, str], *args: str, cwd: Path | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*eo_command(), *args],
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        **kwargs,
    )


@pytest.fixture
def session(env: dict[str, str], stub: tuple[FakePlatform, str, Any]) -> dict[str, str]:
    """The documented onboarding order: bootstrap the profile, *then* log in.

    The order matters — see
    ``TestExitCodes.test_login_before_the_profile_exists_is_lost``.
    """
    platform, base_url, _ = stub
    bootstrap = run(env, "config", "init", "--platform", base_url)
    assert bootstrap.returncode == 0, bootstrap.stderr
    result = run(
        env, "auth", "login", "--username", platform.username, "--password", platform.password
    )
    assert result.returncode == 0, result.stderr
    return env


class TestPipelines:
    """C1 — the documented composition, over real file descriptors."""

    def test_search_json_piped_into_download(
        self, session: dict[str, str], stub: tuple[FakePlatform, str, Any], tmp_path: Path
    ) -> None:
        platform, _, _ = stub
        target = tmp_path / "data"

        search = subprocess.Popen(
            [
                *eo_command(),
                "search",
                "--collection",
                "SENTINEL-2",
                "--from",
                "2026-06-15",
                "--to",
                "2026-06-16",
                "--format",
                "json",
            ],
            env=session,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert search.stdout is not None
        download = subprocess.Popen(
            [*eo_command(), "download", "-", "-o", str(target), "--via", "http", "--quiet"],
            env=session,
            stdin=search.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        search.stdout.close()  # let `search` see EPIPE if `download` dies first
        out, err = download.communicate(timeout=TIMEOUT)
        search.wait(timeout=TIMEOUT)

        assert search.returncode == 0
        assert download.returncode == 0, err
        source = platform.products[0]
        assert (target / source.zip_name).read_bytes() == source.zip_bytes
        assert "done" in out.decode()


class TestStreamSeparation:
    """C2 — data on stdout, diagnostics on stderr, or pipelines corrupt."""

    def test_json_on_stdout_nothing_parseable_on_stderr(
        self, session: dict[str, str], tmp_path: Path
    ) -> None:
        out_file = tmp_path / "out.json"
        err_file = tmp_path / "err.txt"
        with out_file.open("w") as out, err_file.open("w") as err:
            code = subprocess.run(
                [
                    *eo_command(),
                    "search",
                    "--collection",
                    "SENTINEL-2",
                    "--from",
                    "2026-06-01",
                    "--to",
                    "2026-06-30",
                    "--format",
                    "json",
                ],
                env=session,
                stdout=out,
                stderr=err,
                timeout=TIMEOUT,
            ).returncode
        assert code == 0, err_file.read_text()

        lines = [line for line in out_file.read_text().splitlines() if line.strip()]
        # four default Sentinel-2 products plus the one C3 needs for pacing
        assert len(lines) == 5
        for line in lines:
            json.loads(line)
        assert err_file.read_text().strip() == ""

    def test_progress_never_reaches_stdout(
        self, session: dict[str, str], stub: tuple[FakePlatform, str, Any], tmp_path: Path
    ) -> None:
        platform, _, _ = stub
        result = run(
            session,
            "download",
            platform.products[1].uuid,
            "-o",
            str(tmp_path / "d"),
            "--via",
            "http",
        )
        assert result.returncode == 0, result.stderr
        # the "downloading N product(s)" banner is diagnostics
        assert "downloading" in result.stderr
        assert "downloading" not in result.stdout


class TestExitCodes:
    """C3 — cron and CI read exit codes, not prose."""

    def test_success_is_zero(self, session: dict[str, str]) -> None:
        assert run(session, "collections", "--json").returncode == 0

    def test_login_before_the_profile_exists_is_lost(
        self, env: dict[str, str], stub: tuple[FakePlatform, str, Any]
    ) -> None:
        """A defect, pinned rather than papered over.

        With only ``EOSDK_PLATFORM`` set, the very first command has no profile
        yet, so the session is cached under ``default``. That same command's
        discovery call then writes the platform profile and makes it the
        default, so the *next* command looks for a session under the new profile
        name and finds none. Bootstrapping the profile first (``eo config init``,
        as the docs instruct) avoids it — but a user who runs ``eo auth login``
        first is silently logged out.
        """
        platform, _, _ = stub
        first = run(
            env, "auth", "login", "--username", platform.username, "--password", platform.password
        )
        assert first.returncode == 0, first.stderr

        status = run(env, "auth", "status")
        assert status.returncode == 1
        assert "not logged in" in status.stderr

        # logging in again — now that the profile exists — sticks
        again = run(
            env, "auth", "login", "--username", platform.username, "--password", platform.password
        )
        assert again.returncode == 0, again.stderr
        assert run(env, "auth", "status").returncode == 0

    def test_taxonomy_failure_is_non_zero(self, session: dict[str, str]) -> None:
        result = run(session, "get", "00000000-0000-4000-8000-000000000000", "--json")
        assert result.returncode == 1
        assert "not found" in result.stderr
        assert result.stdout.strip() == ""

    @pytest.mark.skipif(os.name == "nt", reason="SIGINT delivery differs on Windows")
    def test_sigint_mid_download_exits_130_with_a_summary(
        self, session: dict[str, str], stub: tuple[FakePlatform, str, Any], tmp_path: Path
    ) -> None:
        _, _, server = stub
        target = tmp_path / "interrupted"
        server.chunk_delay = 0.05  # ~16 chunks of pacing on the 1 MiB product
        try:
            process = subprocess.Popen(
                [*eo_command(), "download", SLOW_PRODUCT.uuid, "-o", str(target), "--via", "http"],
                env=session,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _wait_for_part_file(target, process)
            process.send_signal(signal.SIGINT)
            _, err = process.communicate(timeout=TIMEOUT)
        finally:
            server.chunk_delay = 0.0

        assert process.returncode == 130
        assert "stopped" in err
        assert "0 of 1 product(s) downloaded" in err
        assert "re-run the same command" in err  # the resume hint
        assert not list(target.glob("*.zip"))  # nothing half-finished was promoted


class TestBinaryPassthrough:
    """C4 — ``eo cat`` writes bytes, not text."""

    def test_cat_to_a_file_is_byte_identical(
        self, session: dict[str, str], stub: tuple[FakePlatform, str, Any], tmp_path: Path
    ) -> None:
        platform, _, _ = stub
        source = platform.products[0]
        destination = tmp_path / "band.jp2"
        with destination.open("wb") as sink:
            code = subprocess.run(
                [*eo_command(), "cat", source.s3_path, B04, "--via", "s3"],
                env=session,
                stdout=sink,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT,
            ).returncode
        assert code == 0
        assert destination.read_bytes() == source.tree[B04]


class TestKeyExportInterop:
    """C5 — the aws-cli/rclone interop claim in SPEC §7.4."""

    def test_exported_credentials_are_evalable_and_work(
        self, session: dict[str, str], stub: tuple[FakePlatform, str, Any]
    ) -> None:
        import boto3

        platform, _, _ = stub
        result = run(session, "keys", "create", "--label", "shell", "--export")
        assert result.returncode == 0, result.stderr

        exported = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.strip())
        assert set(exported) == {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ENDPOINT_URL",
        }
        # eval-able means: no prose, no shell metacharacters, one assignment per line
        for line in result.stdout.splitlines():
            assert line.count("=") >= 1
            assert not line.startswith(" ")

        s3 = boto3.client(
            "s3",
            endpoint_url=exported["AWS_ENDPOINT_URL"],
            aws_access_key_id=exported["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=exported["AWS_SECRET_ACCESS_KEY"],
            region_name=platform.urls.s3_region,
        )
        source = platform.products[0]
        body = s3.get_object(Bucket=source.bucket, Key=f"{source.s3_prefix}/{B04}")["Body"].read()
        assert body == source.tree[B04]


class TestHelpInDumbTerminals:
    """C6 — rich must not need a terminal to render help."""

    def _commands(self) -> list[list[str]]:
        import click
        from typer.main import get_command

        from eosdk.cli.main import app

        found: list[list[str]] = []

        def walk(command: click.Command, path: list[str]) -> None:
            found.append(path)
            for name, child in getattr(command, "commands", {}).items():
                walk(child, [*path, name])

        walk(get_command(app), [])
        return found

    def test_every_help_exits_zero(self, env: dict[str, str]) -> None:
        failures = []
        for path in self._commands():
            result = run(env, *path, "--help")
            if result.returncode != 0 or not result.stdout.strip():
                failures.append((path, result.returncode, result.stderr[:200]))
        assert failures == []

    def test_no_args_prints_help_without_a_traceback(self, env: dict[str, str]) -> None:
        result = run(env)
        assert "Usage" in result.stdout
        assert "Traceback" not in result.stderr


def _wait_for_part_file(
    target: Path, process: subprocess.Popen[str], *, deadline: float = 20.0
) -> None:
    """Block until the transfer has actually started, or fail loudly.

    Sending SIGINT before the first byte is written would test nothing, and
    sending it after the transfer finished would test something else.
    """
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if target.exists() and any(target.glob("*.part")):
            return
        if process.poll() is not None:
            raise AssertionError(f"the download exited early ({process.returncode})")
        time.sleep(0.05)
    raise AssertionError("no .part file appeared: the transfer never started")
