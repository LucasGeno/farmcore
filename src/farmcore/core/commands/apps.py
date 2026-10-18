"""`farmcore apps`: print the apps in load order with version, state and dependencies."""

from __future__ import annotations

import typer

from farmcore.core import registry
from farmcore.db import make_engine, make_session_factory
from farmcore.settings import get_settings

APP_PACKAGES: tuple[str, ...] = ("farmcore.apps",)


def register(cli: typer.Typer) -> None:
    @cli.command()
    def apps() -> None:
        """List apps in load order: position, name, version, state, depends."""
        settings = get_settings()
        ordered = registry.discover(packages=APP_PACKAGES)
        engine = make_engine(settings)
        try:
            with make_session_factory(engine)() as session:
                states = registry.read_states(session, ordered)
        finally:
            engine.dispose()
        for i, m in enumerate(ordered, start=1):
            depends = ", ".join(m.depends) or "-"
            typer.echo(f"{i:>2}  {m.name:<12} {m.version:<8} {states[m.name]:<12} {depends}")
