"""``eo search`` — catalogue search; ``--json`` emits JSON Lines for piping."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from eosdk.cli._state import build_client, friendly_errors, get_state, stderr, stdout
from eosdk.exceptions import ConfigError

_FORMATS = ("table", "json", "id", "s3")


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if raw is None:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise typer.BadParameter("bbox must be minx,miny,maxx,maxy")
    try:
        minx, miny, maxx, maxy = (float(p) for p in parts)
    except ValueError as exc:
        raise typer.BadParameter(f"bbox has a non-numeric part: {raw!r}") from exc
    return (minx, miny, maxx, maxy)


def _parse_filters(raw: list[str]) -> dict[str, str]:
    filters: dict[str, str] = {}
    for entry in raw:
        key, sep, value = entry.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"--filter expects key=value, got {entry!r}")
        filters[key] = value
    return filters


def _interval(date_from: str | None, date_to: str | None) -> str | None:
    if date_from is None and date_to is None:
        return None
    return f"{date_from or '..'}/{date_to or '..'}"


def search(
    ctx: typer.Context,
    collection: Annotated[str | None, typer.Option("--collection", "-c")] = None,
    bbox: Annotated[str | None, typer.Option("--bbox", help="minx,miny,maxx,maxy (WGS84).")] = None,
    date_from: Annotated[str | None, typer.Option("--from", help="Start date/time.")] = None,
    date_to: Annotated[str | None, typer.Option("--to", help="End date/time.")] = None,
    filters: Annotated[
        list[str] | None,
        typer.Option("--filter", "-f", help='Attribute filter, e.g. "cloudCover=<20".'),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", "-n")] = None,
    sort: Annotated[str | None, typer.Option("--sort", help="'+field' or '-field'.")] = None,
    protocol: Annotated[str, typer.Option("--protocol", help="stac | odata")] = "stac",
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one product per line (JSON Lines) for piping."),
    ] = False,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-F",
            help="table | json | id | s3 — id/s3 print one uuid or S3 path per line.",
        ),
    ] = "table",
) -> None:
    """Search the catalogue; human table by default, --json for pipelines."""
    if fmt not in _FORMATS:
        raise typer.BadParameter(f"--format must be one of: {', '.join(_FORMATS)}")
    if as_json:
        if fmt not in ("table", "json"):
            raise typer.BadParameter("--json and --format conflict; give only one")
        fmt = "json"
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        if bbox is None and collection is None and not (filters or date_from or date_to):
            raise ConfigError(
                "refusing an unbounded search",
                hint="give at least --collection, --bbox, --from/--to, or --filter",
            )
        results = client.search(
            collection=collection,
            bbox=_parse_bbox(bbox),
            datetime=_interval(date_from, date_to),
            filters=_parse_filters(filters or []),
            limit=limit,
            sort=sort,
            protocol=protocol,
        )
        if fmt == "json":
            for product in results:  # streams page by page
                print(product.model_dump_json())
            return
        if fmt == "id":
            for product in results:
                print(product.id)
            return
        if fmt == "s3":
            skipped = 0
            for product in results:
                if product.s3_path:
                    print(product.s3_path)
                else:
                    skipped += 1
            if skipped:
                stderr.print(f"[yellow]skipped {skipped} product(s) without an S3 path[/yellow]")
            return
        table = Table(title="Products")
        table.add_column("name", overflow="fold")
        table.add_column("datetime")
        table.add_column("size", justify="right")
        table.add_column("cloud %", justify="right")
        count = 0
        for product in results:
            table.add_row(
                product.name,
                product.datetime.isoformat() if product.datetime else "-",
                f"{product.size:,}" if product.size else "-",
                f"{product.cloud_cover:.1f}" if product.cloud_cover is not None else "-",
            )
            count += 1
        stdout.print(table)
        stdout.print(f"{count} product(s)")
