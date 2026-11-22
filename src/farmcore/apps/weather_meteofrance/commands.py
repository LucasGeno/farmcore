"""`farmcore weather-meteofrance import-etp FILE --farm ID [--last-update YYYY-MM-DD] [--url URL]`
stores the farm's cell of a Météo-France daily ETP grid file (`etp`), the official ET0 that
`weather.et0` prefers. `--last-update` is the date data.gouv.fr gives for the file's last update
(credited in the attribution); without it, the file's own modification date is used.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
            None, "--last-update", help="The file's last update on data.gouv.fr (YYYY-MM-DD)."
        ),
        url: str = typer.Option(None, "--url", help="Where the file was downloaded from."),
    ) -> None:
        """Import the farm's cell of a daily ETP grid file (official ET0)."""
        updated = (
            date.fromisoformat(last_update)
            if last_update
            else datetime.fromtimestamp(path.stat().st_mtime, UTC).date()
        )
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
            f"{done.days} day(s) of ETP read for grid cell {done.station} (receipt "
            f"{done.receipt_id}; a value already held keeps its first receipt)"
        )
