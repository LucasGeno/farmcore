"""`farmcore effects [enable|pause]`: show or switch whether this installation acts outside
itself (the Telegram advice push; the bot's polling).

Switching runs as the database owner (`docker compose run --rm migrate farmcore effects
enable`): the setting is a database-level one that the runtime role cannot change.
"""

from __future__ import annotations

import typer
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError

from farmcore.core.commands.backup import database_error
from farmcore.core.startup import (
    EFFECTS_ENABLED,
    EFFECTS_PAUSED,
    EFFECTS_SETTING,
    read_external_effects,
)
from farmcore.settings import get_settings

# Who may switch: the database owner or a superuser (ALTER DATABASE ... SET needs one).
_MAY_SWITCH = text(
    "SELECT current_user, r.rolsuper OR d.datdba = r.oid FROM pg_roles r, pg_database d "
    "WHERE r.rolname = current_user AND d.datname = current_database()"
)


def set_external_effects(conn: Connection, value: str) -> None:
    if value not in (EFFECTS_ENABLED, EFFECTS_PAUSED):
        raise ValueError(f"external effects are {EFFECTS_ENABLED} or {EFFECTS_PAUSED}")
    database = conn.dialect.identifier_preparer.quote(
        conn.exec_driver_sql("SELECT current_database()").scalar_one()
    )
    # A fixed literal from the two above; ALTER DATABASE takes no bind parameters.
    conn.exec_driver_sql(f"ALTER DATABASE {database} SET {EFFECTS_SETTING} = '{value}'")


def _fail(message: str) -> None:
    typer.echo(f"effects: {message}", err=True)
    raise typer.Exit(1)


def _switch(value: str) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.begin() as conn:
            me, allowed = conn.execute(_MAY_SWITCH).one()
            if not allowed:
                _fail(
                    f"only the database owner can switch external effects; connected as {me}. "
                    "Use the owner's DATABASE_URL (docker compose run --rm --no-deps migrate "
                    "farmcore effects ...)."
                )
            set_external_effects(conn, value)
    except DBAPIError as exc:
        _fail(f"cannot read the database: {database_error(exc)}")
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
                state = read_external_effects(conn)
        except DBAPIError as exc:
            _fail(f"cannot read the database: {database_error(exc)}")
        finally:
            engine.dispose()
        typer.echo(f"external effects: {state}")

    @effects.command("enable")
    def enable() -> None:
        """Let this installation send (advice push) and poll its bot token."""
        _switch(EFFECTS_ENABLED)

    @effects.command("pause")
    def pause() -> None:
        """Stop the advice push from its next run; the bot stops polling at its next start."""
        _switch(EFFECTS_PAUSED)
