"""``eo get`` — fetch and print one product's normalized metadata by id."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout


def get(
    ctx: typer.Context,
    product_id: Annotated[str, typer.Argument(help="Catalogue product id.")],
    protocol: Annotated[str, typer.Option("--protocol", help="stac | odata")] = "stac",
    as_json: Annotated[bool, typer.Option("--json", help="Print the product as JSON.")] = False,
) -> None:
    """Fetch one product by id (mirrors client.get())."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        product = client.get(product_id, protocol=protocol)
        if as_json:
            print(product.model_dump_json())
            return
        table = Table(show_header=False, title=f"Product {product.id}")
        table.add_row("name", product.name)
        table.add_row("collection", product.collection or "-")
        table.add_row("datetime", product.datetime.isoformat() if product.datetime else "-")
        table.add_row("size", f"{product.size:,}" if product.size else "-")
        table.add_row(
            "cloud %", f"{product.cloud_cover:.1f}" if product.cloud_cover is not None else "-"
        )
        table.add_row("s3 path", product.s3_path or "-")
        stdout.print(table)
