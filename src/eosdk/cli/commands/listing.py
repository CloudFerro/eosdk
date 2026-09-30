"""``eo list`` — list the files inside a product."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._refs import product_from_ref
from eosdk.cli._state import build_client, friendly_errors, get_state, stdout


def list_files(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Product id (http) or S3 path (with --via s3).")],
    path: Annotated[str, typer.Argument(help="Sub-path inside the product.")] = "",
    via: Annotated[str, typer.Option("--via", help="http | s3")] = "http",
    recursive: Annotated[
        bool, typer.Option("--recursive", "-r", help="Recurse into sub-directories.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List a product's internal file tree (mirrors client.list())."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        product = product_from_ref(ref, via)
        nodes = client.list(product, path, via=via, recursive=recursive)
        if as_json:
            print(json.dumps([n.model_dump(mode="json") for n in nodes], indent=2))
            return
        table = Table(title=f"Files in {product.name}")
        table.add_column("type")
        table.add_column("path", overflow="fold")
        table.add_column("size", justify="right")
        for node in nodes:
            table.add_row(
                "dir" if node.is_dir else "file",
                node.path,
                f"{node.size:,}" if node.size is not None else "-",
            )
        stdout.print(table)
        stdout.print(f"{len(nodes)} node(s)")
