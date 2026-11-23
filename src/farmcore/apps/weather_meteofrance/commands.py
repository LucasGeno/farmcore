"""`farmcore weather-meteofrance import-etp FILE --farm ID --last-update YYYY-MM-DD [--url URL]`
stores the cells of a Météo-France daily ETP grid file (`etp`) that hold the farm pin and its
parcels: the official ET0 that `weather.et0` prefers. `--last-update` is required: the date
data.gouv.fr gives for the file's last update, which Licence Ouverte asks to credit. A local
file's date (when it was downloaded or copied) is not the publisher's, so it is never used.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from farmcore.apps.weather_meteofrance.etp import EtpFileError, import_file
from farmcore.core.weather.store import WeatherValueError
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings


def register(cli: typer.Typer) -> None:
    group = typer.Typer(no_args_is_help=True, help="Météo-France data files.")
    cli.add_typer(group, name="weather-meteofrance")

    @group.command("import-etp")
    def import_etp(
        path: Path = typer.Argument(..., exists=True, dir_okay=False, help="QUOT_ETPgrille file."),
        farm: int = typer.Option(..., "--farm", help="The farm (id); its pin picks the cell."),
        last_update: str = typer.Option(
            ..., "--last-update", help="The file's last update on data.gouv.fr (YYYY-MM-DD)."
        ),
        url: str = typer.Option(None, "--url", help="Where the file was downloaded from."),
    ) -> None:
        """Import the farm's cell of a daily ETP grid file (official ET0)."""
        try:
            updated = date.fromisoformat(last_update)
        except ValueError:
            typer.echo(f"weather-meteofrance import-etp failed: {last_update!r} is not YYYY-MM-DD")
            raise typer.Exit(code=1) from None
        engine = make_engine(get_settings())
        try:
            with session_scope(make_session_factory(engine)) as session:
                done = import_file(session, farm, path, last_update=updated, url=url)
        except (EtpFileError, WeatherValueError) as exc:
            typer.echo(f"weather-meteofrance import-etp failed: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            engine.dispose()
        typer.echo(
            f"{done.values} daily value(s) read for grid cell(s) {', '.join(done.stations)} "
            f"(receipt {done.receipt_id}; a value already held keeps its first receipt)"
        )
        if done.uncovered:
            typer.echo(f"{done.uncovered} point(s) of the farm lie outside every cell of the file")
