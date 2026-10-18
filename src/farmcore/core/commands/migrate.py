"""`farmcore migrate`: upgrade the database to the latest revision."""

from __future__ import annotations

from importlib.resources import files

import typer
from alembic import command
from alembic.config import Config


def alembic_config(url: str | None = None) -> Config:
    """Alembic config built in code (works without `alembic.ini`, e.g. in the container)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(files("farmcore") / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def register(cli: typer.Typer) -> None:
    @cli.command()
    def migrate() -> None:
        """Upgrade the database schema to head (DATABASE_URL)."""
        command.upgrade(alembic_config(), "head")
        typer.echo("migrated to head")
