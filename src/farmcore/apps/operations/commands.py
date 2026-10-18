"""`farmcore operations isoxml-demo --out <dir>` and `farmcore operations import-isoxml <path>`.

`isoxml-demo` writes the synthetic sprayer file to `<dir>/TASKDATA/TASKDATA.XML` (never commit
it). `import-isoxml` imports a TaskData file (XML or zip) on the default farm at the clock's now
and prints one line per task; exit 1 with the plain message on error.
"""

from __future__ import annotations

from pathlib import Path

import typer

from farmcore.apps.operations import isoxml, isoxml_demo
from farmcore.composition import compose_spray_weather_reader
from farmcore.core import registry
from farmcore.core.access import FarmNotSeeded, ScopedRepository, default_farm, ensure_owner_grant
from farmcore.core.clock import get_clock
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

ACTOR = "cli:operations.import-isoxml"


def task_line(t: isoxml.TaskImport) -> str:
    if t.unchanged:  # first: a re-import of a task that made its own log is not merged either
        what = "merged, already imported" if t.merged else "new log, already imported"
    elif not t.merged:
        what = "new log"
    elif t.replaced:
        what = "merged, time and dose replaced"
    else:
        what = "merged, not replaced"
    return f"{t.task_id} {t.partfield}: log {t.log_id} {what}"


def register(cli: typer.Typer) -> None:
    group = typer.Typer(no_args_is_help=True, help="Field operations: ISOXML TaskData.")
    cli.add_typer(group, name="operations")

    @group.command("isoxml-demo")
    def isoxml_demo_(
        out: Path = typer.Option(..., "--out", help="Directory; writes TASKDATA/TASKDATA.XML."),
    ) -> None:
        """Write the synthetic demo TaskData file (10 Apr 2024 pass on P5 and P6)."""
        settings = get_settings()
        path = out / "TASKDATA" / isoxml.XML_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(isoxml_demo.demo_taskdata(tz=settings.display_tz))
        typer.echo(str(path))

    @group.command("import-isoxml")
    def import_isoxml(
        path: Path = typer.Argument(..., help="TASKDATA.XML or a zip holding it."),
    ) -> None:
        """Import a TaskData file on the default farm at the clock's now."""
        try:
            data = path.read_bytes()
        except OSError as exc:
            typer.echo(f"import-isoxml failed: cannot read {path}: {exc.strerror}")
            raise typer.Exit(code=1) from None
        settings = get_settings()
        engine = make_engine(settings)
        try:
            with session_scope(make_session_factory(engine)) as session:
                farm = default_farm(session, settings)
                grant = ensure_owner_grant(session, farm)
                repo = ScopedRepository(session, farm.id, grant, ACTOR, "cli")
                result = isoxml.import_taskdata(
                    repo,
                    data,
                    now=get_clock(session, settings).now(),
                    actor=ACTOR,
                    tz=settings.display_tz,
                    # The discovered apps: Weather's live state is read at capture.
                    weather_reader=compose_spray_weather_reader(registry.discover()),
                )
        except (isoxml.IsoxmlError, FarmNotSeeded) as exc:
            typer.echo(f"import-isoxml failed: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            engine.dispose()
        for t in result.tasks:
            typer.echo(task_line(t))
