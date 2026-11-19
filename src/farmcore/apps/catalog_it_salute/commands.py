"""`farmcore catalog-it-salute fetch [--out]` downloads the weekly Fitosanitari file; `import
[--file]` imports it idempotently. Without `--file`, `import` downloads first.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import typer

from farmcore.apps.catalog_it_salute import service, sources
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

SALUTE_DIR = "it-salute"

_DOWNLOAD_ERRORS = (
    sources.SourceError,
    httpx.HTTPError,
    OSError,
    ValueError,
    KeyError,
)


def register(cli: typer.Typer) -> None:
    group = typer.Typer(
        no_args_is_help=True,
        help="Italy's plant-protection product register (Ministero della Salute).",
    )
    cli.add_typer(group, name="catalog-it-salute")

    @group.command("fetch")
    def fetch(
        out: Path | None = typer.Option(
            None, "--out", help="Download folder (default DATA_DIR/it-salute)."
        ),
    ) -> None:
        """Download the weekly Fitosanitari JSON file from dati.salute.gov.it."""
        settings = get_settings()
        dest = out or settings.data_dir / SALUTE_DIR
        try:
            path, _meta = sources.download(dest)
        except _DOWNLOAD_ERRORS as exc:
            typer.echo(f"catalog-it-salute fetch failed: {exc}")
            raise typer.Exit(code=1) from None
        typer.echo(f"downloaded {path.name} into {dest}")

    @group.command("import")
    def import_(
        file: Path | None = typer.Option(
            None, "--file", help="A local Fitosanitari JSON file (instead of downloading)."
        ),
    ) -> None:
        """Import products, IT registration numbers and substance links (idempotent)."""
        settings = get_settings()
        engine = make_engine(settings)
        try:
            meta: sources.SourceMeta | None = None
            if file is not None:
                path = file
            else:
                path, meta = sources.download(settings.data_dir / SALUTE_DIR)
            with session_scope(make_session_factory(engine)) as session:
                report = service.import_salute(session, path=path, meta=meta)
        except _DOWNLOAD_ERRORS as exc:
            typer.echo(f"catalog-it-salute import failed: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            engine.dispose()
        if report.changed:
            typer.echo(f"wrote {report.products} products, skipped {report.skipped} rows")
            for reason in report.reasons[:10]:
                typer.echo(f"  {reason}")
        else:
            typer.echo("unchanged: nothing to import")
