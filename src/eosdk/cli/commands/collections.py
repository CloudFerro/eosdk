"""``eo collections`` — list the collections a catalogue offers."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout


def collections(
    ctx: typer.Context,
    protocol: Annotated[str, typer.Option("--protocol", help="stac | odata")] = "stac",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List catalogue collections (mirrors client.collections())."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        items = client.collections(protocol=protocol)
        if as_json:
            print(json.dumps([c.model_dump(mode="json") for c in items], indent=2))
            return
        table = Table(title="Collections")
        table.add_column("id", overflow="fold")
        table.add_column("title", overflow="fold")
        for collection in items:
            table.add_row(collection.id, collection.title or "-")
        stdout.print(table)
        stdout.print(f"{len(items)} collection(s)")
