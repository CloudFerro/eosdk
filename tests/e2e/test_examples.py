"""``examples/`` as a test suite (plan §4.10).

Eight Python and five shell examples are referenced from the docs and executed
by nothing, so they rot silently. Running them against the FakePlatform turns
the documentation into something CI defends — and it already found one: example
07 called ``ODataCatalogue.collections()``, which that backend refuses by design.

The Python examples run in-process (``runpy``) because respx patches httpx
inside *this* process; a subprocess would reach the real network. The shell
examples are not executed — they need a live account, a real shell and ``jq`` —
but every ``eo`` invocation they contain is checked against the CLI's actual
command and option set, which is the drift that would matter.
"""

from __future__ import annotations

import os
import re
import runpy
import shlex
import subprocess
from itertools import takewhile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.e2e.platform import (
    FakeCollection,
    FakePlatform,
    FakeProduct,
    default_collections,
    default_products,
)
from tests.eodata import safe_tree

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.e2e

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
PYTHON_EXAMPLES = sorted(EXAMPLES.glob("python/*.py"))
SHELL_EXAMPLES = sorted(EXAMPLES.glob("cli/*.sh"))

#: the examples talk CDSE vocabulary: STAC product-level ids, OData mission names
STAC_COLLECTION = "sentinel-2-l2a"
ODATA_COLLECTION = "SENTINEL-2"


def example_products() -> list[FakeProduct]:
    """The default products, re-labelled with the two-vocabulary split.

    On CDSE a STAC collection id (``sentinel-2-l2a``) and an OData collection
    name (``SENTINEL-2``) are different strings for overlapping things, and the
    examples say so. The default fixture deliberately makes them equal so that
    protocol-parity assertions mean something; here they must differ.
    """
    products = default_products()
    for product in products:
        if product.collection == "SENTINEL-2":
            product.stac_collection = STAC_COLLECTION
    return products


def example_collections() -> list[FakeCollection]:
    collections = default_collections()
    sentinel2 = next(c for c in collections if c.id == "SENTINEL-2")
    stac_view = FakeCollection(
        id=STAC_COLLECTION,
        title="Sentinel-2 Level-2A",
        description="Surface reflectance; the STAC-side id for the L2A products.",
        queryables=dict(sentinel2.queryables),
    )
    return [stac_view, *collections]


@pytest.fixture
def example_platform(make_platform: Callable[..., FakePlatform]) -> FakePlatform:
    return make_platform(products=example_products(), collections=example_collections())


@pytest.fixture
def example_env(
    example_platform: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Make a bare ``Client()`` inside an example resolve to the fake platform.

    Everything an example could reach for is redirected: the platform root comes
    from ``EOSDK_PLATFORM``, all four caches and the config file follow
    ``XDG_CONFIG_HOME``, and the working directory is a scratch dir — several
    examples download into ``./data``. Nothing is passed as a kwarg, because the
    examples construct ``Client()`` with no arguments and the point is that the
    *default* locations are what they use.
    """
    from eosdk.client import Client

    config_home = tmp_path / "xdg"
    (config_home / "eosdk").mkdir(parents=True, exist_ok=True)

    # Endpoint env vars outrank the profile (SPEC §6.1), so a developer's own
    # EOSDK_* pins would redirect an example at a real host: clear the whole
    # namespace before setting the only two the examples are allowed to see.
    for name in [key for key in os.environ if key.startswith("EOSDK_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("EOSDK_PLATFORM", example_platform.urls.platform)
    monkeypatch.chdir(tmp_path)

    # Two steps on purpose. The first client has no config yet, so it runs under
    # profile "default"; fetching the document writes the managed "example-eu"
    # profile and makes it the default. Only a client built *after* that resolves
    # to the profile the examples will use — and the session has to land in that
    # profile's token cache, not in "default"'s.
    with Client(platform=example_platform.urls.platform, cwd=tmp_path) as bootstrap:
        bootstrap.discovery.document()
    with Client(cwd=tmp_path) as warm:
        assert warm.config.profile == "example-eu"
        warm.auth.login(example_platform.username, example_platform.password)
    return config_home


@pytest.fixture(autouse=True)
def _restore_plugin_registry() -> Iterator[None]:
    """Example 08 replaces ``eosdk.plugins.entry_points`` in place."""
    import eosdk.plugins as plugins_module

    original = plugins_module.entry_points
    yield
    plugins_module.entry_points = original


def run_example(path: Path) -> None:
    runpy.run_path(str(path), run_name="__main__")


class TestPythonExamplesRun:
    """Every example completes against the fake platform."""

    @pytest.mark.parametrize("example", PYTHON_EXAMPLES, ids=lambda p: p.stem)
    def test_example_runs_clean(
        self, example: Path, example_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_example(example)
        out = capsys.readouterr().out
        assert out.strip(), f"{example.name} printed nothing — did it do anything?"

    def test_01_search_basic_prints_both_vocabularies(
        self, example_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_example(EXAMPLES / "python" / "01_search_basic.py")
        out = capsys.readouterr().out
        assert "matched:" in out
        assert "first product id:" in out
        assert f"queryables for {STAC_COLLECTION} (stac)" in out
        assert "eo:cloud_cover" in out  # STAC vocabulary
        assert "[odata]" in out  # the same query over the other protocol
        assert "cloudCover" in out

    def test_02_download_products_writes_files(
        self, example_env: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        run_example(EXAMPLES / "python" / "02_download_products.py")
        out = capsys.readouterr().out
        assert "checksum ok" in out
        downloaded = sorted(path.name for path in (tmp_path / "data").iterdir())
        assert any(name.endswith(".zip") for name in downloaded)  # the http backend
        assert any(name.endswith(".SAFE") for name in downloaded)  # the s3 backend

    def test_03_list_and_open_reads_a_band_header(
        self, example_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_example(EXAMPLES / "python" / "03_list_and_open.py")
        out = capsys.readouterr().out
        band = safe_tree.FILES[
            "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2"
        ]
        assert f"first 1024 bytes -> {band[:16].hex()}" in out
        # the capability gate is part of what the example teaches
        assert "as expected: backend 'http' does not support the 'open' capability" in out

    def test_04_auth_and_s3_keys_uses_the_key_lifecycle(
        self,
        example_env: Path,
        example_platform: FakePlatform,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        run_example(EXAMPLES / "python" / "04_auth_and_s3_keys.py")
        out = capsys.readouterr().out
        assert "logged_in=True" in out
        assert example_platform.call_count("keys", "POST") >= 1
        # the ephemeral key was revoked on context exit, the labeled one kept
        assert len(example_platform.keys) == 1

    def test_06_error_handling_demonstrates_the_taxonomy(
        self, example_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_example(EXAMPLES / "python" / "06_error_handling.py")
        out = capsys.readouterr().out
        assert "not found:" in out
        assert "bad filter:" in out
        assert "config: unknown profile" in out
        assert "hint:" in out

    def test_07_raw_queries_uses_both_escape_hatches(
        self, example_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_example(EXAMPLES / "python" / "07_raw_queries.py")
        out = capsys.readouterr().out
        assert "platform-specific keys:" in out
        assert "queryable attributes on SENTINEL-1" in out
        assert "[stac raw]" in out

    def test_08_plugin_backend_registers_both_kinds(
        self, example_env: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        run_example(EXAMPLES / "python" / "08_plugin_backend.py")
        out = capsys.readouterr().out
        assert "static catalogue matched 2" in out
        assert "DEMO_PRODUCT_1" in out
        assert "as expected:" in out
        assert (tmp_path / "data" / "DEMO_PRODUCT_1.placeholder").exists()


# -- shell examples ------------------------------------------------------------

_EO_INVOCATION = re.compile(r"^\s*(?:\|\s*)?eo\s+(?P<rest>.*)$")


def eo_invocations(script: str) -> list[list[str]]:
    """Every uncommented ``eo ...`` command line in a script, as argv lists.

    Continuation lines are joined first; a command split across ``\\`` newlines
    is one invocation, and the pipeline forms (``| eo download -``) count too.
    """
    joined = script.replace("\\\n", " ")
    found = []
    for raw in joined.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        for fragment in line.split("|"):
            match = _EO_INVOCATION.match(" " + fragment.strip())
            if match is None:
                continue
            try:
                argv = shlex.split(match.group("rest"))
            except ValueError:
                continue  # an unbalanced quote inside a comment-ish fragment
            if argv:
                found.append(argv)
    return found


def cli_surface() -> dict[str, dict[str, bool]]:
    """command path -> {option: takes a value}, straight from the typer app."""
    import click
    from typer.main import get_command

    from eosdk.cli.main import app

    surface: dict[str, dict[str, bool]] = {}

    def walk(command: click.Command, prefix: str) -> None:
        options: dict[str, bool] = {}
        for param in command.params:
            wants_value = not getattr(param, "is_flag", False)
            for opt in param.opts:
                options[opt] = wants_value
            for opt in getattr(param, "secondary_opts", ()):  # --no-checksum et al
                options[opt] = False
        surface[prefix] = options
        # duck-typed rather than `isinstance(..., click.Group)`: typer's
        # TyperGroup is not recognized as one under click 8.4
        for name, child in getattr(command, "commands", {}).items():
            walk(child, f"{prefix} {name}".strip())

    walk(get_command(app), "")
    return surface


class TestShellExamplesMatchTheCli:
    """The scripts are documentation; the CLI is the contract they document."""

    # A bare "bash" on Windows resolves to System32's WSL launcher before any Git
    # Bash on PATH, and the syntax check is OS-independent: Linux covers it.
    @pytest.mark.skipif(os.name == "nt", reason="no reliable bash on Windows")
    @pytest.mark.parametrize("script", SHELL_EXAMPLES, ids=lambda p: p.stem)
    def test_script_is_valid_bash(self, script: Path) -> None:
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("script", SHELL_EXAMPLES, ids=lambda p: p.stem)
    def test_every_eo_invocation_exists_in_the_cli(self, script: Path) -> None:
        surface = cli_surface()
        commands = {name for name in surface if name}
        problems: list[str] = []

        for argv in eo_invocations(script.read_text()):
            # the command path is the leading run of non-option words; anything
            # after the first option may be that option's value ("--sort -datetime")
            words = list(takewhile(lambda word: not word.startswith("-"), argv))
            command = _longest_known_prefix(words, commands)
            if command is None:
                problems.append(f"unknown command: eo {' '.join(argv)}")
                continue
            declared = {**surface[""], **surface[command]}
            problems.extend(
                f"eo {command}: unknown option {name}"
                for name in _options_used(argv, declared)
                if name not in declared
            )
        assert problems == []


def _options_used(argv: list[str], declared: dict[str, bool]) -> list[str]:
    """Option names in ``argv``, skipping the values the options consume."""
    used: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-") and token != "-":
            name, separator, _ = token.partition("=")
            used.append(name)
            if not separator and declared.get(name, False):
                index += 1  # the next token is this option's value
        index += 1
    return used


def _longest_known_prefix(words: list[str], commands: set[str]) -> str | None:
    for length in range(min(2, len(words)), 0, -1):
        candidate = " ".join(words[:length])
        if candidate in commands:
            return candidate
    return None


def test_examples_readme_lists_every_example() -> None:
    """A new example that the README does not mention is an invisible example."""
    readme = (EXAMPLES / "README.md").read_text()
    for example in [*PYTHON_EXAMPLES, *SHELL_EXAMPLES]:
        relative = example.relative_to(EXAMPLES).as_posix()
        assert relative in readme, f"{relative} is not listed in examples/README.md"


def test_no_example_writes_outside_its_working_directory() -> None:
    """Examples download into ``./data``; an absolute path would be hostile."""
    offenders: list[str] = []
    for example in PYTHON_EXAMPLES:
        text = example.read_text()
        for match in re.finditer(r'target=["\'](?P<path>[^"\']+)', text):
            path = match.group("path")
            if Path(path).is_absolute() or path.startswith("~"):
                offenders.append(f"{example.name}: {path}")
    assert offenders == []
