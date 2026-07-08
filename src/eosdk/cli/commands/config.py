"""``eo config`` — init, show, set, use, profiles (SPEC §7.4)."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout
from eosdk.config import loader, profiles
from eosdk.config.settings import URL_FIELDS

config_app = typer.Typer(no_args_is_help=True)


def _config_path(ctx: typer.Context):  # type: ignore[no-untyped-def]
    state = get_state(ctx)
    return state.user_config or loader.user_config_path()


@config_app.command()
def show(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Print every endpoint with its resolved value and source."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        resolved = client.config.resolved()
        if as_json:
            print(
                json.dumps(
                    {k: {"value": v.display, "source": v.source} for k, v in resolved.items()},
                    indent=2,
                )
            )
            return
        table = Table(title=f"Resolved configuration (profile: {client.config.profile or '-'})")
        table.add_column("endpoint")
        table.add_column("value", overflow="fold")
        table.add_column("source")
        for fieldname, value in resolved.items():
            style = "dim" if value.value is None else ""
            table.add_row(fieldname, value.display, value.source, style=style)
        stdout.print(table)


@config_app.command()
def init(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--name", prompt="Profile name")] = "default",
    platform: Annotated[
        str | None,
        typer.Option("--platform", help="Platform root URL; endpoints discovered from it."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing profile.")] = False,
) -> None:
    """Create a profile: platform root, or manual endpoints when none is given."""
    state = get_state(ctx)
    with friendly_errors(state):
        values: dict[str, str] = {}
        if platform is None and typer.confirm("Use a single platform root URL?", default=True):
            platform = typer.prompt("Platform URL")
        if platform is not None:
            values["platform"] = platform
        else:
            stdout.print("Manual endpoint entry (leave empty to skip):")
            for fieldname in sorted(URL_FIELDS - {"discovery_url"}):
                value = typer.prompt(fieldname, default="", show_default=False)
                if value:
                    values[fieldname] = value
        path = _config_path(ctx)
        profiles.init_profile(path, profile, values, force=force)
        stdout.print(f"Profile [bold]{profile}[/bold] written to {path}")


@config_app.command("set")
def set_(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="profiles.<name>.<field> or default_profile")],
    value: Annotated[str, typer.Argument()],
) -> None:
    """Set one config value, preserving file comments and layout."""
    state = get_state(ctx)
    with friendly_errors(state):
        profiles.set_value(_config_path(ctx), key, value)
        stdout.print(f"{key} = {value}")


@config_app.command()
def use(ctx: typer.Context, profile: Annotated[str, typer.Argument()]) -> None:
    """Select the default profile."""
    state = get_state(ctx)
    with friendly_errors(state):
        profiles.set_default_profile(_config_path(ctx), profile)
        stdout.print(f"default profile: [bold]{profile}[/bold]")


@config_app.command("profiles")
def profiles_(ctx: typer.Context) -> None:
    """List profiles; the default is marked."""
    state = get_state(ctx)
    with friendly_errors(state):
        path = _config_path(ctx)
        found = profiles.list_profiles(path)
        default = profiles.get_default_profile(path)
        if not found:
            stdout.print(f"no profiles in {path} — run [bold]eo config init[/bold]")
            return
        table = Table(title=str(path))
        table.add_column("profile")
        table.add_column("platform / pinned endpoints", overflow="fold")
        for name, profile in found.items():
            pins = [
                f"{field}={value}" for field, value in profile.model_dump(exclude_none=True).items()
            ]
            marker = " [green](default)[/green]" if name == default else ""
            table.add_row(f"{name}{marker}", ", ".join(pins) or "-")
        stdout.print(table)
