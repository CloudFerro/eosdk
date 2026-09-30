"""``eo doctor`` — health checks with pipeline-friendly exit codes."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from eosdk.cli._state import build_client, friendly_errors, get_state, stdout


def doctor(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="probe eodata readiness live, bypassing the rate limit"),
    ] = False,
) -> None:
    """Check config, auth, and service health; exit non-zero on any failure."""
    from eosdk.doctor import run_doctor

    state = get_state(ctx)
    with friendly_errors(state), build_client(state) as client:
        sections = run_doctor(client, force_ready=force)

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "section": section.name,
                        "results": [
                            {
                                "name": r.name,
                                "ok": r.ok,
                                "detail": r.detail,
                                "hint": r.hint,
                            }
                            for r in section.results
                        ],
                    }
                    for section in sections
                ],
                indent=2,
            )
        )
    else:
        for section in sections:
            stdout.print(f"\n[bold]{section.name}[/bold]")
            for result in section.results:
                mark = {True: "[green]✓[/green]", False: "[red]✗[/red]", None: "[dim]-[/dim]"}[
                    result.ok
                ]
                line = f"  {mark} {result.name}: {result.detail}"
                if result.ok is False and result.hint:
                    line += f"\n      [yellow]hint:[/yellow] {result.hint}"
                stdout.print(line)

    if any(section.failed for section in sections):
        raise typer.Exit(1)
