"""`stations_weather`: `rr1_mm` is the rain of the hour *ending* at `ts`, so a local day's rain
is the hours in (00:00, next 00:00]; temperature, wind and humidity stay [00:00, next 00:00).
Synthetic station rows."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from farmcore.apps.stations import tools
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.seed import ensure_default_farm
from tests.apps.chat.assistant.toolkit import ctx_at

PARIS = ZoneInfo("Europe/Paris")


def test_rain_at_midnight_belongs_to_the_day_before(session, settings) -> None:
    farm = ensure_default_farm(session, settings)
    station = Asset(farm_id=farm.id, kind="station", name="synthetic station")
    session.add(station)
    session.flush()
    day = datetime(2024, 4, 11, tzinfo=PARIS)
    rows = [
        (day, "rr1_mm", 8.0),  # 23:00–00:00 on 10 Apr
        (day, "t_c", 30.0),  # the 00:00 reading of 11 Apr
        (day + timedelta(hours=12), "t_c", 12.0),
        (day + timedelta(days=1), "rr1_mm", 5.0),  # 23:00–00:00 on 11 Apr
        (day + timedelta(days=1), "t_c", -3.0),  # the 00:00 reading of 12 Apr
    ]
    for ts, variable, value in rows:
        session.add(Observation(asset_id=station.id, ts=ts, variable=variable, value=value))
    session.commit()

    facts = tools.weather(ctx_at(session, settings, "2024-04-10 06:00"), day="2024-04-11").facts

    assert facts["rain_mm"] == 5.0
    assert (facts["t_min"], facts["t_max"]) == (12.0, 30.0)
