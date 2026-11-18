"""`farmcore worker`: run durable live tasks (the live forecast fetch) until stopped."""

from __future__ import annotations

import logging

import typer

from farmcore.core import registry, tasks
from farmcore.db import make_engine, make_session_factory
from farmcore.settings import get_settings


def register(cli: typer.Typer) -> None:
    @cli.command()
    def worker(
        concurrency: int = typer.Option(
            4, "--concurrency", min=1, help="Tasks run at once (more than the farms slow at once)."
        ),
        tick: float = typer.Option(tasks.TICK_SECONDS, "--tick", hidden=True, min=0.05),
        stalled: float = typer.Option(tasks.STALLED_SECONDS, "--stalled", hidden=True, min=0.1),
        heartbeat: float = typer.Option(
            tasks.HEARTBEAT_SECONDS, "--heartbeat", hidden=True, min=0.05
        ),
        app_package: list[str] = typer.Option(
            ["farmcore.apps"], "--app-package", hidden=True, help="App packages to discover."
        ),
    ) -> None:
        """Run durable live tasks: plan due work every tick, retry the tasks of dead workers.
        Live mode only; while the clock replays nothing is planned and queued tasks do nothing."""
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        settings = get_settings()
        engine = make_engine(settings)
        try:
            factory = make_session_factory(engine)
            ordered = registry.discover(packages=tuple(app_package))
            with factory() as session:
                states = registry.read_states(session, ordered)
            manifests = registry.active_manifests(ordered, states)
            tasks.run_worker(
                settings,
                factory,
                manifests,
                concurrency=concurrency,
                tick=tick,
                stalled=stalled,
                heartbeat=heartbeat,
            )
        finally:
            engine.dispose()
