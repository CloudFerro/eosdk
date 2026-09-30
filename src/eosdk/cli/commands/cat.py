"""``eo cat`` — stream one file inside a product to stdout via a ranged read."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from eosdk.cli._refs import product_from_ref
from eosdk.cli._state import build_client, friendly_errors, get_state

_CHUNK = 1 << 16


def cat(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Product id or S3 path (with --via s3).")],
    path: Annotated[str, typer.Argument(help="File path inside the product.")],
    via: Annotated[
        str, typer.Option("--via", help="s3 | http — ranged reads need the s3 backend")
    ] = "s3",
) -> None:
    """Stream one file inside a product to stdout (mirrors client.open()).

    No whole-product download: bytes are read on demand and written raw to
    stdout, so it pipes cleanly (e.g. ``eo cat <id> preview.jpg > preview.jpg``).
    """
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        product = product_from_ref(ref, via)
        buffer = sys.stdout.buffer
        with client.open(product, path, via=via) as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                buffer.write(chunk)
        buffer.flush()
