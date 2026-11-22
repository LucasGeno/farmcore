"""`farmcore weather-push add-device --farm ID --owner LOGIN --name NAME --lat LAT --lon LON`
registers an on-farm sensor: its station and its token, printed once (store it on the device;
it is never shown again). Revoke it on Sharing.
"""

from __future__ import annotations

import typer
from sqlalchemy import select

from farmcore.apps.weather_push.devices import VALID_DAYS, register_device
from farmcore.core.models.identity import LocalIdentity
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings


def register(cli: typer.Typer) -> None:
    group = typer.Typer(no_args_is_help=True, help="Readings pushed by on-farm sensors.")
    cli.add_typer(group, name="weather-push")

    @group.command("add-device")
    def add_device(
        farm: int = typer.Option(..., "--farm", help="The farm the device belongs to (id)."),
        owner: str = typer.Option(..., "--owner", help="Login of a current owner of the farm."),
        name: str = typer.Option(..., "--name", help="The device's name, shown on Sharing."),
        lat: float = typer.Option(..., "--lat", help="Latitude of the device (WGS 84)."),
        lon: float = typer.Option(..., "--lon", help="Longitude of the device (WGS 84)."),
        days: int = typer.Option(VALID_DAYS, "--days", help="Days the token lasts."),
    ) -> None:
        """Register a device: prints its token once."""
        engine = make_engine(get_settings())
        try:
            with session_scope(make_session_factory(engine)) as session:
                identity = session.scalar(select(LocalIdentity).where(LocalIdentity.login == owner))
                if identity is None:
                    raise PermissionError("Only a farm owner registers a device.")
                grant, raw = register_device(
                    session,
                    farm,
                    created_by=identity.id,
                    name=name,
                    latitude=lat,
                    longitude=lon,
                    valid_days=days,
                )
                until = grant.valid_to
        except (PermissionError, ValueError) as exc:
            typer.echo(f"weather-push add-device failed: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            engine.dispose()
        typer.echo(f"device registered until {until:%Y-%m-%d}; its token (shown once):")
        typer.echo(raw)
