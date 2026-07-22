"""``eo`` CLI entry point — a thin typer wrapper over the library (SPEC §7.4)."""

from __future__ import annotations

import dataclasses
from typing import Annotated

import typer

from eosdk.cli import _state
from eosdk.cli.commands.auth import auth_app
from eosdk.cli.commands.cat import cat
from eosdk.cli.commands.collections import collections
from eosdk.cli.commands.config import config_app
from eosdk.cli.commands.discover import discover
from eosdk.cli.commands.doctor import doctor
from eosdk.cli.commands.download import download
from eosdk.cli.commands.get import get
from eosdk.cli.commands.keys import keys_app
from eosdk.cli.commands.listing import list_files
from eosdk.cli.commands.queryables import queryables
from eosdk.cli.commands.search import search

app = typer.Typer(
    name="eo",
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Earth Observation SDK: search the catalogue, download products, auth handled invisibly.",
    pretty_exceptions_enable=False,
)


@app.callback()
def main(
    ctx: typer.Context,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Configuration profile to use.")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show full tracebacks on errors.")
    ] = False,
) -> None:
    ctx.obj = dataclasses.replace(
        ctx.obj if isinstance(ctx.obj, _state.CliState) else _state.CliState(),
        profile=profile,
        verbose=verbose,
    )


app.add_typer(auth_app, name="auth", help="Login, logout, session status.")
app.add_typer(config_app, name="config", help="Profiles and resolved endpoints.")
app.add_typer(keys_app, name="keys", help="S3 key lifecycle (create, list, revoke).")
app.command("search")(search)
app.command("get")(get)
app.command("collections")(collections)
app.command("queryables")(queryables)
app.command("download")(download)
app.command("list")(list_files)
app.command("cat")(cat)
app.command("doctor")(doctor)
app.command("discover")(discover)


if __name__ == "__main__":
    app()
