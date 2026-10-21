"""`farmcore farms create NAME` / `farmcore farms list`: farms of a fresh installation.

An installation without demo data starts with no farms; the operator creates them here and
then gives members access with `farmcore users create LOGIN --farm-id ID`.
"""

from __future__ import annotations

import typer
from sqlalchemy import select

from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings


def register(cli: typer.Typer) -> None:
    farms = typer.Typer(help="Farms of this installation.")
    cli.add_typer(farms, name="farms")

    @farms.command("create")
    def create(name: str) -> None:
        """Create a farm (with its owner grant); prints its id."""
        if not name.strip():
            raise typer.BadParameter("A farm needs a name.")
        engine = make_engine(get_settings())
        try:
            with session_scope(make_session_factory(engine)) as session:
                farm = Farm(name=name.strip())
                session.add(farm)
                session.flush()
                # The owner grant makes the farm selectable (farm switcher, `?farm=`), as for
                # the demo farm; members still need `farmcore users create --farm-id`.
                ensure_owner_grant(session, farm)
                farm_id = farm.id
        finally:
            engine.dispose()
        typer.echo(f"farm {farm_id} created")

    @farms.command("list")
    def list_farms() -> None:
        """Print every farm: id and name."""
        engine = make_engine(get_settings())
        try:
            with make_session_factory(engine)() as session:
                rows = session.execute(select(Farm.id, Farm.name).order_by(Farm.id)).all()
        finally:
            engine.dispose()
        for farm_id, name in rows:
            typer.echo(f"{farm_id}  {name}")
        if not rows:
            typer.echo("no farms")
