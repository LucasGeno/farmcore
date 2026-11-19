"""`farmcore catalog-eu-pesticides fetch [--out]` downloads the active-substances file; `import
[--file]` imports it idempotently. Without `--file`, `import` downloads first.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import typer

from farmcore.apps.catalog_eu_pesticides import service, sources
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

EU_DIR = "eu-pesticides"

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
        help="The EU's active-substance approval register (EU Pesticides Database).",
    )
    cli.add_typer(group, name="catalog-eu-pesticides")

    @group.command("fetch")
    def fetch(
        out: Path | None = typer.Option(
            None, "--out", help="Download folder (default DATA_DIR/eu-pesticides)."
        ),
    ) -> None:
        """Download the active-substances file from the DG SANTE API (the only network step)."""
        settings = get_settings()
        dest = out or settings.data_dir / EU_DIR
        try:
            path, _meta = sources.download(dest)
        except _DOWNLOAD_ERRORS as exc:
            typer.echo(f"catalog-eu-pesticides fetch failed: {exc}")
            raise typer.Exit(code=1) from None
        typer.echo(f"downloaded {path.name} into {dest}")

    @group.command("import")
    def import_(
        file: Path | None = typer.Option(
            None, "--file", help="A local active-substances NDJSON file (instead of downloading)."
        ),
    ) -> None:
        """Import active substances and their approval status (idempotent on the content hash)."""
        settings = get_settings()
        engine = make_engine(settings)
        try:
            meta: sources.SourceMeta | None = None
            if file is not None:
                path = file
            else:
                path, meta = sources.download(settings.data_dir / EU_DIR)
            with session_scope(make_session_factory(engine)) as session:
                report = service.import_substances(session, path=path, meta=meta)
        except _DOWNLOAD_ERRORS as exc:
            typer.echo(f"catalog-eu-pesticides import failed: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            engine.dispose()
        if report.changed:
            typer.echo(f"wrote {report.substances} substances, skipped {report.skipped} rows")
            for reason in report.reasons[:10]:
                typer.echo(f"  {reason}")
        else:
            typer.echo("unchanged: nothing to import")
