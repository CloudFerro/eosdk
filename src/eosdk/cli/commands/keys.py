"""``eo keys`` — S3 key lifecycle (SPEC §6.4).

Access ids are public identifiers and printed in full (``revoke`` takes one as
argument). Secrets are never printed except via the explicit ``--export``
opt-in, which emits ``AWS_*`` assignment lines for interop with aws-cli /
rclone: ``eval "$(eo keys create --export)"``.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout

keys_app = typer.Typer(no_args_is_help=True)


@keys_app.command()
def create(
    ctx: typer.Context,
    label: Annotated[str | None, typer.Option("--label", "-l")] = None,
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Always mint a new key (client.keys.create) instead of reusing the labeled one.",
        ),
    ] = False,
    export: Annotated[
        bool,
        typer.Option("--export", help="Print AWS_* env assignments (includes the secret)."),
    ] = False,
) -> None:
    """Create a key pair.

    With ``--label`` the labeled pair is reused if it still exists
    (``client.keys.get_or_create``); ``--fresh`` forces a new key
    (``client.keys.create``), which may hit the account key cap.
    """
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        provider = client.keys
        credentials = (
            provider.get_or_create(label) if label and not fresh else provider.create(label=label)
        )
        if export:
            endpoint = client.config.endpoints.s3_endpoint
            print(f"AWS_ACCESS_KEY_ID={credentials.access_key}")
            print(f"AWS_SECRET_ACCESS_KEY={credentials.require_secret()}")
            if endpoint:
                print(f"AWS_ENDPOINT_URL={endpoint}")
            return
        expires = credentials.expiration_date or "-"
        stdout.print(f"created key [bold]{credentials.access_key}[/bold] (expires {expires})")
        stdout.print("use [bold]--export[/bold] to print credentials for aws/rclone")


@keys_app.command("list")
def list_(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List key pairs (no secrets)."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        entries = client.keys.list()
        if as_json:
            print(
                json.dumps(
                    [
                        {
                            "access_key": e.access_key,
                            "organization": e.organization,
                            "expiration_date": e.expiration_date,
                            "label": e.label,
                            "local_secret": e.label is not None,
                        }
                        for e in entries
                    ],
                    indent=2,
                )
            )
            return
        table = Table(title="S3 keys", caption="local secret: created by eosdk, secret on disk")
        table.add_column("access key")
        table.add_column("organization")
        table.add_column("expires")
        table.add_column("local secret")
        for entry in entries:
            table.add_row(
                entry.access_key,
                entry.organization or "-",
                entry.expiration_date or "-",
                f"✓ {entry.label}" if entry.label else "-",
            )
        stdout.print(table)


@keys_app.command()
def revoke(
    ctx: typer.Context,
    access_key: Annotated[str, typer.Argument(help="The key's access key.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Revoke a key pair."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        if not yes:
            typer.confirm(f"Revoke S3 key {access_key!r}?", abort=True)
        client.keys.revoke(access_key)
        stdout.print(f"revoked [bold]{access_key}[/bold]")
