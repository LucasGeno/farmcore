"""`farmcore backtest --season YYYY`: advisor passes vs the calendar programme and the first
downy alert vs the BSV first-symptom window, for one of the France-only backtest seasons
(2020–2023, Météo-France 33394002). Pure engine on a committed extract: no database, no network.
A backtest, not validation.
"""

from __future__ import annotations

import typer

from farmcore.apps.kpi import backtest


def register(cli: typer.Typer) -> None:
    @cli.command("backtest")
    def run(
        season: int = typer.Option(
            ..., "--season", help=f"Season year: {', '.join(map(str, backtest.YEARS))}."
        ),
    ) -> None:
        """Backtest one season (France only): passes vs calendar, first downy alert vs BSV."""
        try:
            r = backtest.run_season(season)
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=2) from None
        typer.echo(f"{r.year} {backtest.LABEL} · {backtest.SOURCE_LINE}")
        typer.echo(f"span {r.start} .. {r.end} (default season dates: assumed)")
        typer.echo(
            f"advisor {r.model_passes} passes / {r.model_treatments} treatments · "
            f"calendar programme {r.calendar_passes} passes / {r.calendar_treatments} treatments"
        )
        alert = f"{r.first_alert:%d %b %Y}" if r.first_alert else "none"
        symptoms = f"{r.predicted_symptoms:%d %b %Y}" if r.predicted_symptoms else "not reached"
        typer.echo(f"first downy alert {alert} · predicted first symptoms {symptoms}")
        precision = f" [{r.bsv.precision}]" if r.bsv.precision else ""
        typer.echo(
            f"BSV first symptoms{precision}: {backtest.display(r.bsv.quote)} ({r.bsv.source})"
        )
        typer.echo(backtest.BSV_PROVENANCE)
        typer.echo(f"{backtest.CONSISTENCY_LABEL}: {r.verdict}")
        typer.echo(backtest.ASSUMPTION_NOTE)
        for note in r.gap_notes:
            typer.echo(f"Data gap: {note}")
