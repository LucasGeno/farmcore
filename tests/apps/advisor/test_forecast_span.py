"""The replay forecast covers the whole spray-window horizon (review item 9).

The window's last horizon hour reads rain up to `forecast_span(config)` after now (73 h with the
defaults). Through the real jobs (stations ingest, then advisor evaluate) on a synthetic station
file: an uncovered infection event behind now, then a fully dry, calm horizon. The window must run
to the horizon, its last hour open.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from farmcore.apps.advisor.engine import AdvisorConfig
from farmcore.apps.advisor.engine.config import REGION_DEFAULTS, SEASONS
from farmcore.apps.advisor.engine.window import forecast_span
from farmcore.apps.advisor.job import evaluate
from farmcore.apps.stations import jobs as station_jobs
from farmcore.apps.stations.jobs import ingest_meteofrance
from farmcore.apps.stations.seed import seed as stations_seed
from farmcore.core import seed as core_seed
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Advice, Parcel
from tests.apps.stations.synth import write_csv

# Replay now 10 Apr 2024 06:00 Europe/Paris = 04:00 UTC, after the 2024 FRI12 season gate (5 Apr).
NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
START = datetime(2024, 4, 1, 0, 0, tzinfo=UTC)
RAIN_FROM = datetime(2024, 4, 8, 0, 0, tzinfo=UTC)  # 12 h at 1 mm/h, 14 °C: a 3-10 event
RAIN_HOURS = 12
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"


def _rows() -> list[tuple[datetime, str, str, str, str]]:
    rows = []
    for i in range(24 * 16):  # 1 Apr .. 16 Apr 23:00 UTC
        ts = START + timedelta(hours=i)
        wet = RAIN_FROM <= ts < RAIN_FROM + timedelta(hours=RAIN_HOURS)
        # (ts, rr1_mm, t_c, rh_pct, ff_kmh): rain then dry and calm (0 mm, 5 km/h, 15 °C).
        rows.append((ts, "1.0", "14.0", "95", "5.0") if wet else (ts, "0.0", "15.0", "60", "5.0"))
    return rows


def test_a_dry_calm_72_h_horizon_has_its_last_hour_open(session, settings, monkeypatch, tmp_path):
    job_settings = settings.model_copy(update={"data_dir": tmp_path})
    monkeypatch.setattr(core_seed, "get_settings", lambda: job_settings)
    farm = core_seed.ensure_default_farm(session, job_settings)
    stations_seed(session, scene=None, data_dir=tmp_path)
    session.add(
        Parcel(farm_id=farm.id, name="Test parcel", alias="P1", area_ha=Decimal("1"), geom=SQUARE)
    )
    session.flush()
    write_csv(tmp_path, _rows())
    ctx = JobContext(session=session, now=NOW, settings=job_settings)

    ingest_meteofrance(ctx)
    evaluate(ctx)

    advice = session.scalar(select(Advice).where(Advice.evaluated_at == NOW))
    assert advice.decision == "spray"
    assert advice.next_window_start == NOW
    assert advice.next_window_end == NOW + timedelta(hours=72)  # the horizon, not 71 h


def test_the_station_ingest_covers_every_region_years_forecast_span():
    keys = set(SEASONS) | {(r, y) for r in REGION_DEFAULTS for y in range(2015, 2026)}
    for region, year in sorted(keys):
        span = forecast_span(AdvisorConfig.for_region_year(region, year))
        assert span <= station_jobs.INGEST_HORIZON, (region, year)
