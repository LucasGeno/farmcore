"""`farmcore apps [--install NAME] [--uninstall NAME]`: switch optional apps on or off, then
print the apps in load order with version, state and dependencies.

Like `POST /apps/{name}/install|uninstall`, a switch only changes `app_module.state` and takes
effect at the next start. Required apps stay on, and no change may leave an installed app
without an installed dependency.
"""

from __future__ import annotations

import typer

from farmcore.core import registry
from farmcore.core.models.kernel import AppModule
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

APP_PACKAGES: tuple[str, ...] = ("farmcore.apps",)


def _refusal(ordered, states: dict[str, str]) -> str | None:
    for m in ordered:
        if states[m.name] == registry.UNINSTALLED and (m.required or m.name == registry.CORE.name):
            return f"{m.name} is always on."
    for m in ordered:
        if states[m.name] != registry.INSTALLED:
            continue
        for dep in m.depends:
            if states.get(dep, registry.INSTALLED) == registry.UNINSTALLED:
                return f"{m.name} needs {dep}: switch {m.name} off first, or {dep} on."
    return None


def register(cli: typer.Typer) -> None:
    @cli.command()
    def apps(
        install: list[str] = typer.Option([], "--install", help="Switch this app on."),
        uninstall: list[str] = typer.Option([], "--uninstall", help="Switch this app off."),
    ) -> None:
        """List apps in load order: position, name, version, state, depends."""
        settings = get_settings()
        ordered = registry.discover(packages=APP_PACKAGES)
        names = {m.name for m in ordered}
        for name in [*install, *uninstall]:
            if name not in names:
                raise typer.BadParameter(f"no app named {name!r}")
        engine = make_engine(settings)
        try:
            with session_scope(make_session_factory(engine)) as session:
                states = registry.read_states(session, ordered)
                wanted = {
                    **states,
                    **dict.fromkeys(install, registry.INSTALLED),
                    **dict.fromkeys(uninstall, registry.UNINSTALLED),
                }
                refusal = _refusal(ordered, wanted)
                if refusal:
                    typer.echo(refusal, err=True)
                    raise typer.Exit(1)
                for m in ordered:
                    if wanted[m.name] == states[m.name]:
                        continue
                    row = session.get(AppModule, m.name) or AppModule(
                        name=m.name, version=m.version
                    )
                    row.state = wanted[m.name]
                    session.add(row)
                states = wanted
        finally:
            engine.dispose()
        for i, m in enumerate(ordered, start=1):
            depends = ", ".join(m.depends) or "-"
            typer.echo(f"{i:>2}  {m.name:<12} {m.version:<8} {states[m.name]:<12} {depends}")
        if install or uninstall:
            typer.echo("Changes apply when the app and bot restart.")
