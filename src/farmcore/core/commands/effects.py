"""`farmcore effects [enable|pause]`: show or switch whether this installation acts outside
itself (the Telegram advice push; the bot's polling).

Switching runs as the database owner (`docker compose run --rm migrate farmcore effects
enable`): the setting is a database-level one that the runtime role cannot change.
"""

from __future__ import annotations

import typer
from sqlalchemy import Connection, create_engine
from sqlalchemy.exc import DBAPIError

from farmcore.core.startup import (
    EFFECTS_ENABLED,
    EFFECTS_PAUSED,
    EFFECTS_SETTING,
    read_external_effects,
)
from farmcore.settings import get_settings


def set_external_effects(conn: Connection, value: str) -> None:
    assert value in (EFFECTS_ENABLED, EFFECTS_PAUSED)
    database = conn.dialect.identifier_preparer.quote(
        conn.exec_driver_sql("SELECT current_database()").scalar_one()
    )
    # A fixed literal from the two above; ALTER DATABASE takes no bind parameters.
    conn.exec_driver_sql(f"ALTER DATABASE {database} SET {EFFECTS_SETTING} = '{value}'")


def _switch(value: str) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.begin() as conn:
            set_external_effects(conn, value)
    except DBAPIError:
        typer.echo(
            "Only the database owner can switch external effects: run this with the owner's "
            "DATABASE_URL (docker compose run --rm migrate farmcore effects ...).",
            err=True,
        )
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    typer.echo(f"external effects: {value}")


def register(cli: typer.Typer) -> None:
    effects = typer.Typer(help="Show or switch this installation's external effects.")
    cli.add_typer(effects, name="effects")

    @effects.callback(invoke_without_command=True)
    def status(ctx: typer.Context) -> None:
        """Print `external effects: enabled|paused`."""
        if ctx.invoked_subcommand is not None:
            return
        engine = create_engine(get_settings().database_url)
        try:
            with engine.connect() as conn:
                typer.echo(f"external effects: {read_external_effects(conn)}")
        finally:
            engine.dispose()

    @effects.command("enable")
    def enable() -> None:
        """Let this installation send (advice push) and poll its bot token."""
        _switch(EFFECTS_ENABLED)

    @effects.command("pause")
    def pause() -> None:
        """Stop the advice push; the bot stops polling at its next start."""
        _switch(EFFECTS_PAUSED)
