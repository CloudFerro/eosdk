from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from eosdk.cli._state import CliState
from eosdk.cli.main import app
from tests.conftest import write_profile_config

Invoke = Callable[..., Result]


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
    )
    (tmp_path / "empty").mkdir()

    def _invoke(*args: str, **kwargs: object) -> Result:
        return runner.invoke(app, list(args), obj=state, **kwargs)  # type: ignore[arg-type]

    return _invoke
