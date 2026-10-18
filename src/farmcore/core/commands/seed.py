"""`farmcore seed [--scene B]`: run every installed app's seed in load order, one transaction."""

from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy.orm import Session

from farmcore.core import registry
from farmcore.core.manifest import Manifest, import_string
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

APP_PACKAGES: tuple[str, ...] = ("farmcore.apps",)


def run_seeds(
    session: Session, manifests: list[Manifest], *, scene: str | None, data_dir: Path
) -> list[tuple[str, dict[str, int]]]:
    """Call each loaded app's seed (load order; uninstalled apps skipped); per-app counts."""
    ordered = registry.resolve_order(manifests)
    active = registry.active_manifests(ordered, registry.read_states(session, ordered))
    results = []
    for manifest in active:
        if manifest.seed is None:
            continue
        counts = import_string(manifest.seed)(session, scene=scene, data_dir=data_dir)
        results.append((manifest.name, dict(counts or {})))
    return results


def _format(counts: dict[str, int]) -> str:
    return " ".join(f"{k}={v}" for k, v in counts.items()) or "nothing to seed"


def register(cli: typer.Typer) -> None:
    @cli.command()
    def seed(
        scene: str | None = typer.Option(None, help="Demo scene to seed, e.g. B."),
    ) -> None:
        """Seed every installed app in load order, in one transaction."""
        settings = get_settings()
        engine = make_engine(settings)
        try:
            with session_scope(make_session_factory(engine)) as session:
                results = run_seeds(
                    session,
                    registry.discover(packages=APP_PACKAGES),
                    scene=scene,
                    data_dir=settings.data_dir,
                )
        finally:
            engine.dispose()
        for name, counts in results:
            typer.echo(f"{name}: {_format(counts)}")
