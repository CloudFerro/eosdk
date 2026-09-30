from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from eosdk.cli._state import CliState
from eosdk.cli.main import app
from tests.conftest import write_profile_config

Invoke = Callable[..., Result]


@pytest.fixture(autouse=True)
def plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force plain (no-ANSI) console output so assertions don't depend on the
    dev shell: FORCE_COLOR in the environment makes the module-level Consoles
    emit escape codes even under CliRunner. Usage errors go through typer's own
    Console instead, which is forced into terminal mode at import time whenever
    GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set."""
    import typer.rich_utils

    from eosdk.cli import _state

    for console in (_state.stdout, _state.stderr):
        monkeypatch.setattr(console, "_force_terminal", False)
        monkeypatch.setattr(console, "_color_system", None)
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", False)
    monkeypatch.setattr(typer.rich_utils, "COLOR_SYSTEM", None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def invoke(runner: CliRunner, tmp_path: Path) -> Invoke:
    """Invoke `eo` with config/tokens redirected into tmp_path."""
    config = write_profile_config(tmp_path / "config.toml")
    state = CliState(
        cwd=tmp_path,
        user_config=config,
        token_cache_dir=tmp_path / "tokens",
        keys_cache_dir=tmp_path / "s3keys",
        discovery_cache_dir=tmp_path / "discovery",
        readiness_cache_dir=tmp_path / "readiness",
    )

    def _invoke(*args: str, **kwargs: object) -> Result:
        return runner.invoke(app, list(args), obj=state, **kwargs)  # type: ignore[arg-type]

    return _invoke


@pytest.fixture
def invoke_bare(runner: CliRunner, tmp_path: Path) -> Invoke:
    """Invoke `eo` with an empty config (no profiles)."""
    state = CliState(
        cwd=tmp_path / "empty",
        user_config=tmp_path / "empty" / "config.toml",
        token_cache_dir=tmp_path / "tokens",
        keys_cache_dir=tmp_path / "s3keys",
        discovery_cache_dir=tmp_path / "discovery",
        readiness_cache_dir=tmp_path / "readiness",
    )
    (tmp_path / "empty").mkdir()

    def _invoke(*args: str, **kwargs: object) -> Result:
        return runner.invoke(app, list(args), obj=state, **kwargs)  # type: ignore[arg-type]

    return _invoke
