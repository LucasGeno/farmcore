"""`farmcore catalog-fr-ephy fetch [--out]` downloads the weekly E-Phy archive; `import
[--zip | --dir]` imports it idempotently. Without `--zip`/`--dir`, `import` downloads first.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import httpx
import typer

from farmcore.apps.catalog_fr_ephy import service, sources
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

EPHY_DIR = "ephy-fr"

# A failed download or a truncated archive must print `failed:` and exit 1, not traceback.
_DOWNLOAD_ERRORS = (
    sources.SourceError,
    httpx.HTTPError,
    OSError,
    ValueError,
    KeyError,
    zipfile.BadZipFile,
)


def register(cli: typer.Typer) -> None:
    group = typer.Typer(
        no_args_is_help=True,
        help="France's official plant-protection product register (E-Phy).",
    )
    cli.add_typer(group, name="catalog-fr-ephy")

    @group.command("fetch")
    def fetch(
        out: Path | None = typer.Option(
            None, "--out", help="Extract folder (default DATA_DIR/ephy-fr)."
        ),
    ) -> None:
        """Download the weekly E-Phy CSV archive from data.gouv.fr (the only network step)."""
        settings = get_settings()
        dest = out or settings.data_dir / EPHY_DIR
        try:
            products, uses, meta = sources.download(dest)
        except _DOWNLOAD_ERRORS as exc:
            typer.echo(f"catalog-fr-ephy fetch failed: {exc}")
            raise typer.Exit(code=1) from None
        typer.echo(f"downloaded {products.name} and {uses.name} into {dest}")

    @group.command("import")
    def import_(
        zip_path: Path | None = typer.Option(
            None, "--zip", help="A local E-Phy CSV archive (instead of downloading)."
        ),
        folder: Path | None = typer.Option(
            None, "--dir", help="An extracted E-Phy archive folder."
        ),
    ) -> None:
        """Import products, AMM numbers and registered uses (idempotent on the content hash)."""
        settings = get_settings()
        engine = make_engine(settings)
        try:
            meta: sources.SourceMeta | None = None
            if folder is not None:
                products, uses = sources.find_files(folder)
            elif zip_path is not None:
                products, uses = sources.extract(zip_path, settings.data_dir / EPHY_DIR)
            else:
                products, uses, meta = sources.download(settings.data_dir / EPHY_DIR)
            with session_scope(make_session_factory(engine)) as session:
                report = service.import_ephy(
                    session, products_path=products, uses_path=uses, meta=meta
                )
        except _DOWNLOAD_ERRORS as exc:
            typer.echo(f"catalog-fr-ephy import failed: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            engine.dispose()
        if report.changed:
            typer.echo(
                f"wrote {report.products} products, {report.uses} uses, "
                f"skipped {report.skipped} rows"
            )
            for reason in report.reasons[:10]:
                typer.echo(f"  {reason}")
        else:
            typer.echo("unchanged: nothing to import")
