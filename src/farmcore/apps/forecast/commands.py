"""`farmcore forecast fetch --from --to [--out]` saves Open-Meteo single runs to files (the only
network step); `farmcore forecast import [--dir | --file]` imports runs into the demo farm
(idempotent). No request or job fetches a forecast. Prints counts and times only.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer

from farmcore.apps.forecast import sources, store
from farmcore.apps.stations import seed as stations_seed
from farmcore.core.access import FarmNotSeeded, default_farm
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings

ISSUED = "%Y-%m-%d %H:%M UTC"


def register(cli: typer.Typer) -> None:
    group = typer.Typer(no_args_is_help=True, help="Weather forecasts for the spray window.")
    cli.add_typer(group, name="forecast")

    @group.command("fetch")
    def fetch(
        first: datetime = typer.Option(..., "--from", formats=["%Y-%m-%d"], help="First run day."),
        last: datetime = typer.Option(..., "--to", formats=["%Y-%m-%d"], help="Last run day."),
        out: Path | None = typer.Option(
            None, "--out", help="Folder for the run files (default DATA_DIR/open-meteo/ecmwf_ifs)."
        ),
    ) -> None:
        """Save Open-Meteo ECMWF IFS HRES single runs (00Z and 12Z) to files: the only network
        step. `forecast import --dir` (and the seed) then read them offline."""
        folder = out or get_settings().data_dir / sources.RUNS_DIR
        try:
            saved, skipped = sources.fetch_single_runs(
                first.date(), last.date(), folder, stations_seed.LAT, stations_seed.LON
            )
        except (OSError, ValueError) as exc:
            typer.echo(f"forecast fetch failed: {exc}")
            raise typer.Exit(code=1) from None
        typer.echo(f"{saved} runs saved, {skipped} already saved in {folder}")

    @group.command("import")
    def import_(
        file: Path | None = typer.Option(
            None, "--file", help="Forecast runs in one file (Open-Meteo hourly shape)."
        ),
        folder: Path | None = typer.Option(
            None, "--dir", help="A folder of saved single runs (`forecast fetch`)."
        ),
    ) -> None:
        """Import forecast runs, each with its run and availability time (idempotent)."""
        settings = get_settings()
        engine = make_engine(settings)
        try:
            if file is not None:
                runs = sources.FileSource(file).runs()
            else:
                runs = sources.SingleRunsDir(folder or settings.data_dir / sources.RUNS_DIR).runs()
            with session_scope(make_session_factory(engine)) as session:
                farm = default_farm(session, settings)
                counts = store.import_runs(session, farm.id, runs)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, FarmNotSeeded) as exc:
            typer.echo(f"forecast import failed: {exc}")
            raise typer.Exit(code=1) from None
        finally:
            engine.dispose()
        held = len(runs) - counts["runs"]
        line = f"{counts['runs']} runs imported ({counts['values']} values)"
        if held:
            line += f", {held} already held"
        if runs:
            issued = sorted(r.issued_at for r in runs)
            line += f"; issued {issued[0]:{ISSUED}} .. {issued[-1]:{ISSUED}}"
        typer.echo(line)
