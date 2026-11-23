"""`farmcore et0 --farm ID --from YYYY-MM-DD --to YYYY-MM-DD [--elevation M] [--lat --lon]`
prints one JSON line per day: the daily ET0 at the farm pin (or the point given), its method
and source, as `weather.et0.daily` reads it now. Read only.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import typer

from farmcore.core.access import repo_for_job
from farmcore.core.farm_location import farm_location
from farmcore.core.models.canonical import Farm
from farmcore.core.weather.contract import Point
from farmcore.core.weather.et0 import daily
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings


def register(cli: typer.Typer) -> None:
    @cli.command("et0")
    def et0(
        farm: int = typer.Option(..., "--farm", help="The farm (id)."),
        first: str = typer.Option(..., "--from", help="First UTC day (YYYY-MM-DD)."),
        last: str = typer.Option(..., "--to", help="Last UTC day (YYYY-MM-DD)."),
        elevation: float = typer.Option(None, "--elevation", help="Site elevation (m)."),
        lat: float = typer.Option(None, "--lat", help="Latitude (default: the farm pin)."),
        lon: float = typer.Option(None, "--lon", help="Longitude (default: the farm pin)."),
    ) -> None:
        """Daily reference evapotranspiration: official where published, else FAO-56."""
        engine = make_engine(get_settings())
        try:
            with session_scope(make_session_factory(engine)) as session:
                row = session.get(Farm, farm)
                if row is None:
                    typer.echo(f"et0 failed: farm {farm} not found")
                    raise typer.Exit(code=1)
                pin = farm_location(row)
                point = (
                    Point(lat, lon)
                    if lat is not None and lon is not None
                    else Point(pin["latitude"], pin["longitude"])
                    if pin
                    else None
                )
                repo = repo_for_job(session, farm, "cli.et0")
                days = daily(
                    repo,
                    date.fromisoformat(first),
                    date.fromisoformat(last),
                    point,
                    datetime.now(UTC),
                    elevation,
                )
        finally:
            engine.dispose()
        for day in days:
            typer.echo(json.dumps(day.as_json(), ensure_ascii=False))
