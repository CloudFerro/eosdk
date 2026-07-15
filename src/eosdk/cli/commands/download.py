"""``eo download`` — download products by id or from a piped ``eo search --format json``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TransferSpeedColumn,
)

from eosdk.cli._state import build_client, friendly_errors, get_state, stderr, stdout
from eosdk.models import Product

if TYPE_CHECKING:
    from eosdk.eodata.base import ProgressEvent


def _check_output(value: Path) -> Path:
    # Click happily eats a following option as the value (`--output --via s3`),
    # turning the remaining tokens into product ids; catch that before downloading.
    if str(value).startswith("-"):
        raise typer.BadParameter(
            f"{value} looks like an option, not a directory — is --output missing its value?"
        )
    return value


def _check_ids(ids: list[str]) -> None:
    # Absolute and s3:// paths are valid S3 references; relative paths never are.
    for pid in ids:
        if pid != "-" and pid.startswith("-"):
            raise typer.BadParameter(f"{pid!r} looks like an option, not a product id")
        if pid.startswith(("./", "../", "~")):
            raise typer.BadParameter(
                f"{pid!r} looks like a local path, not a product id — "
                f"did you mean `--output {pid}`?"
            )


def _products_from_stdin() -> list[Product]:
    """Parse JSON Lines (or a single JSON array) from stdin."""
    text = sys.stdin.read().strip()
    if not text:
        raise typer.BadParameter(
            "nothing on stdin; pipe `eo search --format json` into `eo download -`"
        )
    if text[0] == "[":
        return [Product.model_validate(entry) for entry in json.loads(text)]
    return [Product.model_validate_json(line) for line in text.splitlines() if line.strip()]


def download(
    ctx: typer.Context,
    ids: Annotated[
        list[str] | None,
        typer.Argument(
            help="Product ids, S3 paths (with --via s3), or '-' to read "
            "`eo search --format json` from stdin."
        ),
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Target directory.", callback=_check_output)
    ] = Path(),
    via: Annotated[str, typer.Option("--via", help="http | s3")] = "http",
    concurrency: Annotated[int, typer.Option("--concurrency", "-c")] = 4,
    checksum: Annotated[
        bool, typer.Option("--checksum/--no-checksum", help="Verify checksums when available.")
    ] = True,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="No progress output.")] = False,
) -> None:
    """Download products to a directory; pipes: `eo search --format json | eo download -`."""
    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        if not ids:
            raise typer.BadParameter("give product ids or '-' for stdin")
        _check_ids(ids)
        if ids == ["-"]:
            products = _products_from_stdin()
        else:
            # Bare ids build minimal stubs; enough for the HTTP $value route.
            # S3 paths (`s3://…` or `/eodata/…`, e.g. from `eo search --format s3`)
            # carry the address the S3 backend needs instead.
            products = []
            for ref in ids:
                if ref.startswith(("s3://", "/")):
                    if via != "s3":
                        raise typer.BadParameter(
                            f"{ref!r} is an S3 path and only works with --via s3; "
                            "the http route needs product ids (eo search --format id)"
                        )
                    name = ref.rstrip("/").rsplit("/", 1)[-1]
                    products.append(Product(id=name, name=name, s3_path=ref))
                else:
                    products.append(Product(id=ref, name=ref))

        show_progress = not quiet and sys.stderr.isatty()
        if not quiet:
            stderr.print(
                f"downloading {len(products)} product(s) to {output} — press Ctrl+C to stop",
                soft_wrap=True,
            )

        finished: set[str] = set()

        def track(event: ProgressEvent) -> None:
            if event.kind == "done":
                finished.add(event.product_id)

        try:
            if show_progress:
                with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    transient=True,
                ) as progress:
                    tasks: dict[str, TaskID] = {}

                    def on_event(event: ProgressEvent) -> None:
                        track(event)
                        task = tasks.get(event.product_id)
                        if task is None:
                            task = progress.add_task(event.product_id, total=event.bytes_total)
                            tasks[event.product_id] = task
                        progress.update(task, completed=event.bytes_done, total=event.bytes_total)

                    reports = client.download(
                        products,
                        target=output,
                        via=via,
                        concurrency=concurrency,
                        checksum=checksum,
                        progress=on_event,
                    )
            else:
                reports = client.download(
                    products,
                    target=output,
                    via=via,
                    concurrency=concurrency,
                    checksum=checksum,
                    progress=track,
                )
        except KeyboardInterrupt:
            resume_hint = (
                "re-run the same command to resume"
                if via == "s3"
                else "re-run the same command to restart unfinished files"
            )
            stderr.print(
                f"[yellow]stopped[/yellow] — {len(finished)} of {len(products)} product(s) "
                f"downloaded; {resume_hint}"
            )
            raise typer.Exit(130) from None

        for report in reports:
            verified = "verified" if report.checksum_verified else "no checksum"
            stdout.print(f"[green]done[/green] {report.path} ({report.bytes:,} B, {verified})")
