"""`farmcore apps [--install NAME] [--uninstall NAME]`: switch optional apps on or off, then
print the apps in load order with version, state and dependencies.

Like `POST /apps/{name}/install|uninstall`, a switch only changes `app_module.state`, takes
effect at the next start and follows the same rule (`registry.switch_refusal`). Several
switches apply together or not at all: switches off first (dependants before what they need),
then switches on (dependencies first).
"""

from __future__ import annotations

import typer

from farmcore.core import registry
from farmcore.core.models.kernel import AppModule
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

APP_PACKAGES: tuple[str, ...] = ("farmcore.apps",)


def register(cli: typer.Typer) -> None:
    @cli.command()
    def apps(
        install: list[str] = typer.Option([], "--install", help="Switch this app on."),
        uninstall: list[str] = typer.Option([], "--uninstall", help="Switch this app off."),
    ) -> None:
        """List apps in load order: position, name, version, state, depends."""
        settings = get_settings()
        ordered = registry.discover(packages=APP_PACKAGES)
        by_name = {m.name: m for m in ordered}
        for name in [*install, *uninstall]:
            if name not in by_name:
                raise typer.BadParameter(f"no app named {name!r}")
        both = sorted(set(install) & set(uninstall))
        if both:
            raise typer.BadParameter(f"{', '.join(both)}: pass --install or --uninstall, not both")
        switches = [(m, registry.UNINSTALLED) for m in reversed(ordered) if m.name in uninstall]
        switches += [(m, registry.INSTALLED) for m in ordered if m.name in install]
        engine = make_engine(settings)
        try:
            with session_scope(make_session_factory(engine)) as session:
                states = registry.read_states(session, ordered)
                for manifest, state in switches:
                    refusal = registry.switch_refusal(ordered, states, manifest, state)
                    if refusal is not None:
                        typer.echo(refusal.detail, err=True)
                        raise typer.Exit(1)
                    states = {**states, manifest.name: state}
                    row = session.get(AppModule, manifest.name) or AppModule(
                        name=manifest.name, version=manifest.version
                    )
                    row.state = state
                    session.add(row)
        finally:
            engine.dispose()
        for i, m in enumerate(ordered, start=1):
            depends = ", ".join(m.depends) or "-"
            typer.echo(f"{i:>2}  {m.name:<12} {m.version:<8} {states[m.name]:<12} {depends}")
        if switches:
            typer.echo("Changes apply when the app and bot restart.")
