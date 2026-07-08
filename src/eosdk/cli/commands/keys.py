"""``eo keys`` — S3 key lifecycle (SPEC §6.4).

Secrets are masked everywhere except the explicit ``--export`` opt-in, which
emits ``AWS_*`` assignment lines for interop with aws-cli / rclone:
``eval "$(eo keys create --export)"``.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout

keys_app = typer.Typer(no_args_is_help=True)


def _mask(value: str | None) -> str:
    if not value:
        return "-"
    return f"****{value[-4:]}" if len(value) > 4 else "****"


@keys_app.command()
def create(
    ctx: typer.Context,
    label: Annotated[str | None, typer.Option("--label", "-l")] = None,
    export: Annotated[
        bool,
        typer.Option("--export", help="Print AWS_* env assignments (includes the secret)."),
    ] = False,
) -> None:
    """Create a key pair (reuses the labeled pair if one exists)."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        provider = client.keys
        credentials = provider.get_or_create(label) if label else provider.create()
        if export:
            endpoint = client.config.endpoints.exos_endpoint
            print(f"AWS_ACCESS_KEY_ID={credentials.access_key}")
            print(f"AWS_SECRET_ACCESS_KEY={credentials.require_secret()}")
            if endpoint:
                print(f"AWS_ENDPOINT_URL={endpoint}")
            return
        stdout.print(
            f"created key [bold]{credentials.key_id}[/bold]"
            f" (access key {_mask(credentials.access_key)}, secret {_mask('x' * 8)})"
        )
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
                            "key_id": e.key_id,
                            "label": e.label,
                            "created_at": e.created_at,
                        }
                        for e in entries
                    ],
                    indent=2,
                )
            )
            return
        table = Table(title="S3 keys")
        table.add_column("key id")
        table.add_column("access key")
        table.add_column("label")
        table.add_column("created")
        for entry in entries:
            table.add_row(
                entry.key_id, _mask(entry.access_key), entry.label or "-", entry.created_at or "-"
            )
        stdout.print(table)


@keys_app.command()
def revoke(
    ctx: typer.Context,
    key_id: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Revoke a key pair."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        if not yes:
            typer.confirm(f"Revoke S3 key {key_id!r}?", abort=True)
        client.keys.revoke(key_id)
        stdout.print(f"revoked [bold]{key_id}[/bold]")
