"""``eo queryables`` — list the filterable attributes a collection advertises."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout


def queryables(
    ctx: typer.Context,
    collection: Annotated[str, typer.Argument(help="Collection id to inspect.")],
    protocol: Annotated[str, typer.Option("--protocol", help="stac | odata")] = "stac",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List a collection's filterable attributes (mirrors client.queryables()).

    The names printed are backend-native and usable directly as ``--filter``
    keys for the same protocol.
    """
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        items = client.queryables(collection, protocol=protocol)
        if as_json:
            print(json.dumps([q.model_dump(mode="json") for q in items], indent=2))
            return
        table = Table(title=f"Queryables for {collection}")
        table.add_column("name", overflow="fold")
        table.add_column("type")
        for queryable in items:
            table.add_row(queryable.name, queryable.type or "-")
        stdout.print(table)
        stdout.print(f"{len(items)} queryable(s)")
