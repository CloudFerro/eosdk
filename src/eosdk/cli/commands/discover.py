"""``eo discover`` — fetch and print the platform discovery document (SPEC §7.4)."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout
from eosdk.exceptions import ConfigError


def discover(
    ctx: typer.Context,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bust the TTL cache and re-fetch.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the raw document.")] = False,
) -> None:
    """Show the platform's service discovery document."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        if not client.discovery.configured:
            raise ConfigError(
                "no platform root configured for discovery",
                hint="set platform=... in your profile or EOSDK_PLATFORM",
            )
        document = client.discovery.refresh() if refresh else client.discovery.document()
        if as_json:
            print(json.dumps(document.model_dump(), indent=2, default=str))
            return
        stdout.print(f"discovery document [bold]{client.discovery.url}[/bold]")
        stdout.print(f"schema version: {document.version}")
        table = Table()
        table.add_column("service")
        table.add_column("strategy")
        table.add_column("url", overflow="fold")
        table.add_column("api", justify="center")
        table.add_column("capabilities")
        table.add_column("status")
        for service_name, service in sorted(document.services.items()):
            if not isinstance(service, dict):
                continue
            strategies = {
                key: value
                for key, value in service.items()
                if isinstance(value, dict) and "url" in value
            }
            if strategies:
                for strategy_name, info in strategies.items():
                    status = ""
                    if info.get("deprecated"):
                        status = f"[yellow]deprecated (sunset {info.get('sunset', '?')})[/yellow]"
                    table.add_row(
                        service_name,
                        strategy_name,
                        str(info.get("url", "")),
                        str(info.get("api_version") or "-"),
                        ", ".join(info.get("capabilities") or []) or "-",
                        status or "[green]ok[/green]",
                    )
            else:
                url = service.get("url") or service.get("endpoint") or service.get("issuer", "")
                table.add_row(service_name, "-", str(url), "-", "-", "[green]ok[/green]")
        stdout.print(table)
