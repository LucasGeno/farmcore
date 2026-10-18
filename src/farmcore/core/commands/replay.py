"""`farmcore replay --to "2024-04-10 06:00"` (local time) or `farmcore replay --live`."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import typer
from sqlalchemy.orm import Session, sessionmaker

from farmcore.core import jobs, registry
from farmcore.core.clock import get_clock, parse_local, set_live
from farmcore.core.manifest import Manifest
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings
from farmcore.web import LOCAL_FORMAT


def _manifests(factory: sessionmaker[Session], packages: tuple[str, ...]) -> list[Manifest]:
    """Installed apps in load order, as `registry.load()` puts on `app.state.manifests`, so
    `farmcore replay` runs exactly the jobs the navbar step runs."""
    ordered = registry.discover(packages=packages)
    with factory() as session:
        states = registry.read_states(session, ordered)
    return registry.active_manifests(ordered, states)


def register(cli: typer.Typer) -> None:
    @cli.command()
    def replay(
        to: str | None = typer.Option(
            None, "--to", help='Step the replay clock to local time "YYYY-MM-DD HH:MM".'
        ),
        live: bool = typer.Option(False, "--live", help="Switch the shared clock to live mode."),
        app_package: list[str] = typer.Option(
            ["farmcore.apps"], "--app-package", hidden=True, help="App packages to discover."
        ),
    ) -> None:
        """Step the replay clock (running due jobs in manifest order), or switch to live."""
        if (to is None) == (not live):
            raise typer.BadParameter("pass exactly one of --to or --live")
        settings = get_settings()
        engine = make_engine(settings)
        try:
            factory = make_session_factory(engine)
            if live:
                with session_scope(factory) as session:
                    set_live(session, settings)
                typer.echo("clock mode: live (restart web and bot to start the scheduler)")
                return
            try:
                t = parse_local(to, settings.display_tz)
            except ValueError as exc:
                raise typer.BadParameter(str(exc), param_hint="--to") from exc
            runs = jobs.step_to(factory, _manifests(factory, tuple(app_package)), t, settings)
            with factory() as session:
                now = get_clock(session, settings).now()
        finally:
            engine.dispose()

        local = now.astimezone(ZoneInfo(settings.display_tz)).strftime(LOCAL_FORMAT)
        typer.echo(f"replay now {local} {settings.display_tz} ({now.isoformat()})")
        for run in runs:
            status = "ok" if run.ok else "FAILED"
            line = f"{status:<6} {run.name}  {run.ms} ms"
            typer.echo(f"{line}  {run.error}" if run.error else line)
        failed = sum(not r.ok for r in runs)
        typer.echo(f"{len(runs)} job(s) ran, {failed} failed")
        if failed:
            raise typer.Exit(code=1)
