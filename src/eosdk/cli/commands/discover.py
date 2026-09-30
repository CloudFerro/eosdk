"""``eo discover`` — fetch and print the platform discovery document."""

from __future__ import annotations

import json
from typing import Annotated, Any

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
        if document.platform is not None:
            line = f"platform: [bold]{document.platform.name}[/bold]"
            if document.platform.description:
                line += f" — {document.platform.description}"
            stdout.print(line)
        stdout.print(f"schema version: {document.version}")
        table = Table()
        table.add_column("service")
        table.add_column("strategy", overflow="fold")
        table.add_column("url", overflow="fold")
        table.add_column("api", justify="center")
        table.add_column("capabilities")
        table.add_column("status")

        def add_service_rows(service_name: str, service: dict[str, Any], prefix: str = "") -> None:
            """One row per strategy; groups without a url (e.g. data_access.http)
            recurse with the group path shown in the strategy column."""
            strategies = {
                key: value
                for key, value in service.items()
                if isinstance(value, dict) and "url" in value
            }
            groups = {
                key: value
                for key, value in service.items()
                if isinstance(value, dict) and "url" not in value
            }
            if service.get("endpoint"):
                table.add_row(
                    service_name,
                    prefix or "-",
                    str(service["endpoint"]),
                    "-",
                    "-",
                    "[green]ok[/green]",
                )
            for strategy_name, info in strategies.items():
                status = ""
                if info.get("deprecated"):
                    status = f"[yellow]deprecated (sunset {info.get('sunset', '?')})[/yellow]"
                table.add_row(
                    service_name,
                    f"{prefix}/{strategy_name}" if prefix else strategy_name,
                    str(info.get("url", "")),
                    str(info.get("api_version") or "-"),
                    ", ".join(info.get("capabilities") or []) or "-",
                    status or "[green]ok[/green]",
                )
            if not strategies and not groups and not service.get("endpoint"):
                url = service.get("url") or service.get("issuer", "")
                table.add_row(service_name, prefix or "-", str(url), "-", "-", "[green]ok[/green]")
            for group_name, group in sorted(groups.items()):
                child_prefix = f"{prefix}/{group_name}" if prefix else group_name
                add_service_rows(service_name, group, prefix=child_prefix)

        for service_name, service in sorted(document.services.items()):
            if not isinstance(service, dict):
                continue
            add_service_rows(service_name, service)
        stdout.print(table)
        if client.discovered_profile is not None:
            name, status = client.discovered_profile
            if status == "conflict":
                stdout.print(
                    f"[yellow]profile '{name}' already exists and is not discovery-managed; "
                    "platform snapshot not saved — rename or delete that profile to let "
                    "eosdk manage it[/yellow]"
                )
            elif status in {"created", "updated"}:
                stdout.print(
                    f"platform configuration saved as profile [bold]{name}[/bold] ({status})"
                )
