"""``eo auth`` — login, logout, status (SPEC §6.4 CLI surface)."""

from __future__ import annotations

import datetime as dt
import sys
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stderr, stdout

auth_app = typer.Typer(no_args_is_help=True)


@auth_app.command()
def login(
    ctx: typer.Context,
    username: Annotated[str | None, typer.Option("--username", "-u")] = None,
    password: Annotated[
        str | None,
        typer.Option("--password", help="Prefer --password-stdin or the interactive prompt."),
    ] = None,
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help="Read the password from stdin.")
    ] = False,
    device: Annotated[
        bool, typer.Option("--device", help="Force the device flow (default when available).")
    ] = False,
) -> None:
    """Log in via device flow (default) or username/password."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        auth = client.auth
        use_device = device or (
            username is None and auth._endpoints().device_authorization_endpoint is not None
        )
        if use_device:

            def prompt(info) -> None:  # type: ignore[no-untyped-def]
                url = info.verification_uri_complete or info.verification_uri
                stdout.print(
                    Panel(
                        f"Open [bold link={url}]{url}[/bold link]\n"
                        f"and enter code [bold]{info.user_code}[/bold]",
                        title="Device login",
                    )
                )

            auth.login_device(prompt)
        else:
            if username is None:
                username = typer.prompt("Username")
            if password is None:
                password = (
                    sys.stdin.readline().strip()
                    if password_stdin
                    else typer.prompt("Password", hide_input=True)
                )
            auth.login(username, password)
        stdout.print("[green]Logged in.[/green]")


@auth_app.command()
def logout(ctx: typer.Context) -> None:
    """Drop the cached session for the active profile."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        client.auth.logout()
        stdout.print("Logged out.")


@auth_app.command()
def status(ctx: typer.Context) -> None:
    """Show session status; exits non-zero when not logged in."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        info = client.auth.status()
        table = Table(show_header=False)
        table.add_row("profile", info.profile)
        table.add_row("realm", info.realm)
        table.add_row("logged in", "yes" if info.logged_in else "no")
        if info.refresh_valid_until is not None:
            expires = dt.datetime.fromtimestamp(info.refresh_valid_until, tz=dt.timezone.utc)
            table.add_row("session valid until", expires.isoformat())
        stdout.print(table)
        if not info.logged_in:
            stderr.print("not logged in — run [bold]eo auth login[/bold]")
            raise typer.Exit(1)
