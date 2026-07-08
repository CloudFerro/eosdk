"""Shared CLI state and the single error-rendering path for all commands."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from eosdk.exceptions import EosdkError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from eosdk.client import Client

stdout = Console()
stderr = Console(stderr=True)


@dataclass
class CliState:
    profile: str | None = None
    verbose: bool = False
    # test seams: redirect config/token locations without touching $HOME
    cwd: Path | None = None
    user_config: Path | None = None
    token_cache_dir: Path | None = None
    keys_cache_dir: Path | None = None


def get_state(ctx: typer.Context) -> CliState:
    return ctx.obj if isinstance(ctx.obj, CliState) else CliState()


def build_client(state: CliState) -> Client:
    from eosdk.client import Client

    return Client(
        profile=state.profile,
        cwd=state.cwd,
        user_config=state.user_config,
        token_cache_dir=state.token_cache_dir,
        keys_cache_dir=state.keys_cache_dir,
    )


@contextmanager
def friendly_errors(state: CliState) -> Iterator[None]:
    """One error path for every command: styled message, exit 1, -v for traceback."""
    try:
        yield
    except EosdkError as exc:
        if state.verbose:
            raise
        stderr.print(f"[bold red]error:[/bold red] {exc}")
        raise typer.Exit(1) from exc
