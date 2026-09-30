"""``eo config`` — init, show, set, use, profiles."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout
from eosdk.config import loader, profiles
from eosdk.config.settings import URL_FIELDS
from eosdk.exceptions import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

    from eosdk.cli._state import CliState

config_app = typer.Typer(no_args_is_help=True)


def _config_path(ctx: typer.Context) -> Path:
    state = get_state(ctx)
    return state.user_config or loader.user_config_path()


def _init_from_platform(
    state: CliState, path: Path, platform: str, *, name: str | None, force: bool
) -> tuple[str, str]:
    """Fetch the platform's discovery document now, then pin every resolved
    endpoint into a profile named after the platform.

    Returns ``(profile_name, status)``. A discovery-managed profile of the same
    name is resynced wholesale without ``--force``; a user-owned one is a
    conflict that ``--force`` overwrites. Hard-fails (the raised error propagates
    through ``friendly_errors``) when the platform is unreachable or the document
    is malformed — the profile is written only if discovery succeeds.
    """
    from eosdk.discovery.models import project_endpoints
    from eosdk.discovery.resolver import DiscoveryResolver
    from eosdk.transport import Transport

    with Transport() as transport:
        resolver = DiscoveryResolver(
            platform=platform,
            discovery_url=None,
            transport=transport,
            cache_dir=state.discovery_cache_dir,
        )
        document = resolver.document()
        info = document.platform
        resolved_name = name or (info.profile_name if info is not None else None)
        if resolved_name is None:
            raise ConfigError(
                "platform discovery document advertises no usable name",
                hint="pass an explicit --name",
            )
        values = dict(project_endpoints(document))
        values["platform"] = platform
        if info is not None and info.description:
            values["description"] = info.description
        discovered_from = resolver.url or ""

    status = profiles.save_discovered_profile(
        path, resolved_name, values=values, discovered_from=discovered_from
    )
    if status == "conflict":
        if not force:
            raise ConfigError(
                f"profile {resolved_name!r} already exists and was not created from discovery",
                hint="pass --force to overwrite it, or --name to pick another name",
            )
        values["discovered_from"] = discovered_from
        profiles.init_profile(path, resolved_name, values, force=True)
        status = "updated"
    profiles.set_default_profile(path, resolved_name)
    return resolved_name, status


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
    profile: Annotated[
        str | None,
        typer.Option("--name", help="Profile name; defaults to the platform's advertised name."),
    ] = None,
    platform: Annotated[
        str | None,
        typer.Option("--platform", help="Platform root URL; endpoints discovered from it."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing profile.")] = False,
) -> None:
    """Create a profile: discover a platform's endpoints, or enter them manually."""
    state = get_state(ctx)
    with friendly_errors(state):
        path = _config_path(ctx)
        if platform is None and typer.confirm("Use a single platform root URL?", default=True):
            platform = typer.prompt("Platform URL")
        if platform is not None:
            name, status = _init_from_platform(state, path, platform, name=profile, force=force)
            stdout.print(
                f"Discovered platform; profile [bold]{name}[/bold] {status} "
                f"(now default) in {path}\n"
                "run [bold]eo config show[/bold] to see the resolved endpoints, "
                "then [bold]eo doctor[/bold] to check they are reachable"
            )
            return
        name = profile or typer.prompt("Profile name", default="default")
        stdout.print("Manual endpoint entry (leave empty to skip):")
        values: dict[str, str] = {}
        for fieldname in sorted(URL_FIELDS - {"discovery_url"}):
            value = typer.prompt(fieldname, default="", show_default=False)
            if value:
                values[fieldname] = value
        profiles.init_profile(path, name, values, force=force)
        stdout.print(f"Profile [bold]{name}[/bold] written to {path}")


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
