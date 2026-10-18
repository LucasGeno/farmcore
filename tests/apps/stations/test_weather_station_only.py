"""Hard rule: `stations_weather` reads only the station asset (`Asset.kind == "station"`,
Météo-France 33394002), never a `regional` asset (regional imports put their rows on the
demo farm under that kind). The regional asset here is synthetic, with values that differ."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from farmcore.apps.stations import tools
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor import scenes
from tests.apps.chat.assistant.toolkit import ctx_at

pytestmark = pytest.mark.data

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

NOW = "2024-04-10 06:00"
PARIS = ZoneInfo("Europe/Paris")


def add_regional(session, settings) -> Asset:
    """A synthetic regional asset on the demo farm, 11 Apr every hour: 9 mm rain, 40 °C,
    120 km/h, 5 % humidity (nothing like the station)."""
    farm = ensure_default_farm(session, settings)
    regional = Asset(farm_id=farm.id, kind="regional", name="synthetic regional cell")
    session.add(regional)
    session.flush()
    start = datetime(2024, 4, 11, tzinfo=PARIS)
    for h in range(24):
        ts = start + timedelta(hours=h)
        for variable, value in (("rr1_mm", 9.0), ("t_c", 40.0), ("ff_kmh", 120.0), ("rh_pct", 5)):
            session.add(Observation(asset_id=regional.id, ts=ts, variable=variable, value=value))
    session.commit()
    return regional


class SpyRepository(ScopedRepository):
    """Records every `list` statement as SQL with its values inlined."""

    def __init__(self, repo: ScopedRepository) -> None:
        super().__init__(repo.session, repo.farm_id, repo.grant, repo.actor, repo.purpose)
        self.sql: list[str] = []

    def _record(self, stmt) -> None:
        self.sql.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))

    def list(self, model, *where, scope, order_by=None, limit=None):
        self._record(self._scoped(model).where(*where))
        return super().list(model, *where, scope=scope, order_by=order_by, limit=limit)


def test_a_regional_asset_on_the_demo_farm_leaves_the_weather_unchanged(
    session, settings, seeded, step
):
    seeded("B")
    step(NOW)
    before = tools.weather(ctx_at(session, settings, NOW), day="tomorrow").facts

    add_regional(session, settings)
    after = tools.weather(ctx_at(session, settings, NOW), day="tomorrow").facts

    assert after == before
    assert after["rain_mm"] < 9 * 24


def _assert_reads_station_only(session, settings) -> None:
    regional = add_regional(session, settings)
    station = session.scalar(
        select(Asset).where(
            Asset.kind == "station", Asset.farm_id == ensure_default_farm(session, settings).id
        )
    )
    ctx = ctx_at(session, settings, NOW)
    spy = SpyRepository(ctx.repo)

    tools.weather(type(ctx)(repo=spy, now=ctx.now, tz=ctx.tz, lang=ctx.lang), day="tomorrow")

    asset_reads = [s for s in spy.sql if "FROM asset" in s]
    observation_reads = [s for s in spy.sql if "FROM observation" in s]
    assert asset_reads and observation_reads
    assert all("asset.kind = 'station'" in s for s in asset_reads)
    assert all(f"observation.asset_id = {station.id}" in s for s in observation_reads)
    # Only an asset id column naming the regional asset counts (`farm_id = <same number>` is
    # the farm, not the asset).
    regional_id = re.compile(rf"\b(?:asset\.id|asset_id)\s*(?:=|IN \()\s*{regional.id}\b")
    assert not any(regional_id.search(s) or "regional" in s for s in spy.sql)


def test_every_read_is_on_the_station_asset_only(session, settings, seeded, step):
    seeded("B")
    step(NOW)
    _assert_reads_station_only(session, settings)


def test_a_regional_asset_id_equal_to_the_farm_id_is_not_a_regional_read(
    session, settings, seeded, step
):
    """Ids come from shared sequences, so after other tests the regional asset can get the farm's
    id; `farm_id = <n>` in a station read is then not a regional read. Forced here."""
    session.execute(text("SELECT setval(pg_get_serial_sequence('farm', 'id'), 1000000)"))
    seeded("B")
    step(NOW)
    farm_id = ensure_default_farm(session, settings).id
    session.execute(
        text("SELECT setval(pg_get_serial_sequence('asset', 'id'), :n)"), {"n": farm_id - 1}
    )
    _assert_reads_station_only(session, settings)
