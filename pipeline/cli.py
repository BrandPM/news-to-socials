"""CLI entry point: ``nts``.

A thin Typer wrapper exposing a handful of useful sub-commands. The
business logic lives in the modules; the CLI just wires them together.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer

from .common.config import get_settings
from .common.logging import configure_logging, get_logger

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = get_logger(__name__)


@app.callback()
def main() -> None:
    """News-to-Socials CLI."""
    configure_logging()


@app.command()
def poll() -> None:
    """Poll every active source once. Idempotent."""
    from .scheduler.poll_sources import main as poll_main

    asyncio.run(poll_main())


@app.command()
def dispatch() -> None:
    """Dispatch one tick of the publish queue."""
    from .scheduler.dispatch_queue import main as dispatch_main

    asyncio.run(dispatch_main())


@app.command()
def stale() -> None:
    """Report stale (>24h pending, >48h scheduled) posts to monitoring TG."""
    from .scheduler.stale_posts import main as stale_main

    asyncio.run(stale_main())


@app.command()
def summary() -> None:
    """Send the daily summary to monitoring TG."""
    from .monitoring.daily_summary import main as summary_main

    asyncio.run(summary_main())


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
    from scripts.run_pipeline import run_pipeline
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


@app.command()
def health(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8080),
) -> None:
    """Start the /health HTTP server (needs the [api] extra)."""
    import uvicorn

    from .monitoring.health_check import app as health_app

    if health_app is None:
        raise typer.Exit("FastAPI not installed. Install with: pip install '.[api]'")
    uvicorn.run(health_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
