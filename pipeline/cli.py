"""CLI entry point: ``nts``.

A thin Typer wrapper exposing a handful of useful sub-commands. The
business logic lives in the modules; the CLI just wires them together.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer

from .common.logging import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """News-to-Socials CLI."""
    configure_logging()


@app.command()
def run(
    brand: str = typer.Option("icon", help="Brand slug (Wave 1: only 'icon')"),
    source_id: str = typer.Option(..., "--source-id", help="Source identifier"),
    source_url: str = typer.Option(..., "--source-url", help="Feed URL (RSS for Wave 1)"),
    language: str = typer.Option("en", help="Language: ru/uk/en/pl"),
    limit: int = typer.Option(3, help="Max items to process this run"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip Sanity write + image generation"),
) -> None:
    """One-shot run: source → score → generate → image → Sanity draft.

    For Wave 1 the only output channel is Sanity (blog at /:lang/insights).
    Wave 2 (Meta) and Wave 3 (Telegram) are separate publishers.
    """
    from .run import run_pipeline
    from .common.models import Language

    results = asyncio.run(
        run_pipeline(
            brand_slug=brand,
            source_id=source_id,
            source_url=source_url,
            language=Language(language),
            limit=limit,
            dry_run=dry_run,
        )
    )
    typer.echo(f"\nProcessed {len(results)} topics:")
    for r in results:
        typer.echo(f"  {r}")


if __name__ == "__main__":
    app()
