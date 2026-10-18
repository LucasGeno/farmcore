"""Saved spray weather through the public ISOXML import: exact slot, immutability, Weather
off, missing data and rollback. Synthetic station and readings only.

The demo file's tasks: TSK1 P5 Les Granges 10 Apr 05:12 UTC (slot 05:00), TSK2 P6 Peyrot 06:03
UTC (slot 06:00); the voice spray they merge into is P5 + P6 at 04:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from farmcore.apps.operations import isoxml, isoxml_demo, spray_weather
from farmcore.composition import compose_spray_weather_reader
from farmcore.core.models.canonical import Asset, Log, LogSource, Observation
from farmcore.core.models.kernel import AppModule
from farmcore.core.registry import discover
from tests.apps.operations import isoxml_farm
from tests.apps.operations.isoxml_farm import CLOCK, local, voice_spray

farm = isoxml_farm.farm  # fixture
NS = spray_weather.NAMESPACE
READER = compose_spray_weather_reader(discover())


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 4, 10, hour, minute, tzinfo=UTC)


def add_station(session, farm_id: int, rows: dict[datetime, dict[str, float | None]]) -> Asset:
    station = Asset(farm_id=farm_id, kind="station", name="QA synthetic station")
    session.add(station)
    session.flush()
    for ts, values in rows.items():
        for variable, value in values.items():
            if value is not None:
                session.add(Observation(asset_id=station.id, ts=ts, variable=variable, value=value))
    session.flush()
    return station


def hours(*hs: int, **override: float | None) -> dict[datetime, dict[str, float | None]]:
    """Readings at each hour: t_c = hour, wind = hour + 1, rain 0.2 (overridable)."""
    base = {"t_c": None, "ff_kmh": None, "rr1_mm": 0.2, "rh_pct": 80.0}
    return {utc(h): {**base, "t_c": float(h), "ff_kmh": float(h + 1), **override} for h in hs}


def run(farm, reader=READER, now: str = CLOCK):
    return isoxml.import_taskdata(
        farm.repo,
        isoxml_demo.demo_taskdata(),
        now=local(now),
        actor="test",
        weather_reader=reader,
    )


def machine_sources(session) -> dict[str, LogSource]:
    rows = session.scalars(
        select(LogSource)
        .where(LogSource.source_type == "isoxml")
        .execution_options(populate_existing=True)
    )
    return {s.external_key.rsplit(":", 1)[1]: s for s in rows}  # "<digest>:<task id>"


def saved(session) -> dict[str, dict]:
    return {k: s.raw_payload[NS] for k, s in machine_sources(session).items()}


def set_weather(session, state: str) -> None:
    session.merge(AppModule(name="stations", state=state, version="0"))
    session.flush()


# --- selection ----------------------------------------------------------------------------


PARIS = spray_weather.ZoneInfo("Europe/Paris")


@pytest.mark.parametrize(
    ("at", "slot"),
    [
        (utc(5, 12), utc(5)),
        (utc(5, 0), utc(5)),
        (utc(5, 59), utc(5)),
        # 03:30 CEST on the DST night is 01:30 UTC: the UTC slot, not a local hour
        (datetime(2024, 3, 31, 3, 30, tzinfo=PARIS), datetime(2024, 3, 31, 1, tzinfo=UTC)),
    ],
)
def test_the_slot_is_the_utc_hour_at_or_before_the_source_time(at, slot):
    assert spray_weather.slot_for(at) == slot


# --- capture through the import -----------------------------------------------------------


def test_a_new_machine_receipt_saves_its_own_exact_slot_reading(session, farm):
    add_station(session, farm.repo.farm_id, hours(4, 5, 6, 7))

    run(farm)

    p5, p6 = saved(session)["TSK1"], saved(session)["TSK2"]
    assert p5["v"] == 1 and p5["policy"] == "exact_previous_hour_slot_v1"
    assert p5["status"] == "captured" and p5["reason"] is None
    assert p5["source_time"] == utc(5, 12).replace(second=5).isoformat()
    assert p5["slot"] == p5["observed_at"] == utc(5).isoformat()
    assert p5["values"] == {"t_c": 5.0, "ff_kmh": 6.0, "rr1_mm": 0.2}
    assert p5["units"] == {"t_c": "°C", "ff_kmh": "km/h", "rr1_mm": "mm"}
    assert p5["rain_interval"] == [utc(4).isoformat(), utc(5).isoformat()]
    assert p5["parcel_ids"] == [farm.parcels["P5"].id]
    assert p5["station"]["name"] == "QA synthetic station"
    assert p5["data_clock"] == local(CLOCK).isoformat()
    assert p6["values"]["t_c"] == 6.0 and p6["parcel_ids"] == [farm.parcels["P6"].id]
    # the original receipt fields stay as they were
    assert machine_sources(session)["TSK1"].raw_payload["partfield"]


def test_no_carry_forward_measured_zeros_and_partial_readings(session, farm):
    rows = hours(4)  # an older hour only for P5: not used
    rows[utc(6)] = {"t_c": 0.0, "ff_kmh": 0.0, "rr1_mm": 0.0}
    add_station(session, farm.repo.farm_id, rows)

    run(farm)

    p5, p6 = saved(session)["TSK1"], saved(session)["TSK2"]
    assert p5["status"] == "unavailable" and p5["reason"] == spray_weather.NO_SLOT
    assert p5["values"] == {"t_c": None, "ff_kmh": None, "rr1_mm": None}
    assert p6["status"] == "captured" and p6["values"] == {"t_c": 0.0, "ff_kmh": 0.0, "rr1_mm": 0.0}


def test_a_missing_rain_reading_is_partial_not_dry(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6, rr1_mm=None))

    run(farm)

    p5 = saved(session)["TSK1"]
    assert p5["status"] == "partial"
    assert p5["values"]["rr1_mm"] is None and p5["rain_interval"] is None
    assert p5["values"]["t_c"] == 5.0


def test_no_station_and_no_reader_are_distinct_unavailable_records(session, farm):
    run(farm)  # no station asset
    assert {s["reason"] for s in saved(session).values()} == {spray_weather.NO_STATION}


def test_without_the_weather_software_the_record_says_weather_unavailable(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))

    run(farm, reader=None)

    p5 = saved(session)["TSK1"]
    assert p5["status"] == "unavailable" and p5["reason"] == spray_weather.NO_CAPABILITY
    assert p5["station"] is None and p5["slot"] == utc(5).isoformat()


def test_weather_switched_off_never_reads_the_station(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))
    set_weather(session, "uninstalled")
    calls = []

    run(farm, reader=lambda repo, slot: calls.append(slot) or READER(repo, slot))

    assert calls == []
    assert {s["reason"] for s in saved(session).values()} == {spray_weather.NO_CAPABILITY}


def test_weather_switched_back_on_captures_the_next_new_receipt(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))
    set_weather(session, "uninstalled")
    set_weather(session, "installed")

    run(farm)

    assert {s["status"] for s in saved(session).values()} == {"captured"}


def test_saved_facts_never_change_after_reimport_new_readings_or_a_later_clock(session, farm):
    station = add_station(session, farm.repo.farm_id, hours(5, 6))
    run(farm)
    before = {k: dict(s.raw_payload) for k, s in machine_sources(session).items()}

    session.execute(
        update(Observation).where(Observation.asset_id == station.id).values(value=99.0)
    )
    session.flush()
    run(farm, now="2024-04-12 10:00")  # the same bytes again, two days later

    after = {k: dict(s.raw_payload) for k, s in machine_sources(session).items()}
    assert after == before


def test_an_earlier_unavailable_record_is_not_refilled_when_data_arrives(session, farm):
    run(farm)  # no station yet: unavailable
    before = saved(session)
    add_station(session, farm.repo.farm_id, hours(5, 6))

    run(farm)

    assert saved(session) == before


def test_merging_into_the_voice_spray_keeps_each_receipts_own_time_and_parcels(session, farm):
    add_station(session, farm.repo.farm_id, hours(4, 5, 6))
    voice = voice_spray(session, farm)
    session.flush()

    run(farm)

    log = session.get(Log, voice.id, populate_existing=True)
    assert log.ts_start == utc(5, 12).replace(second=5)  # the machine time won
    [voice_src] = [s for s in log.sources if s.source_type == "voice"]
    assert NS not in voice_src.raw_payload  # a seed-time record: no backfill
    p5 = saved(session)["TSK1"]
    assert p5["parcel_ids"] == [farm.parcels["P5"].id]  # never the log's P5 + P6 union
    assert p5["source_time"].startswith("2024-04-10T05:12")


def test_a_held_receipt_retry_writes_nothing(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))
    run(farm)
    for src in machine_sources(session).values():  # simulate receipts held before saved weather
        payload = dict(src.raw_payload)
        payload.pop(NS)
        src.raw_payload = payload
    session.flush()

    run(farm)

    assert all(NS not in s.raw_payload for s in machine_sources(session).values())


def test_a_failing_station_read_rolls_the_import_back(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))

    def broken(repo, slot):
        raise OperationalError("SELECT observation", {}, Exception("connection lost"))

    nested = session.begin_nested()
    with pytest.raises(OperationalError):
        run(farm, reader=broken)
    nested.rollback()

    assert machine_sources(session) == {}


def test_capture_reads_only_the_importing_farms_station(session, settings, farm):
    from farmcore.core.models.canonical import Farm

    other = Farm(name="Other synthetic farm", props={})
    session.add(other)
    session.flush()
    add_station(session, other.id, hours(5, 6))

    run(farm)

    assert {s["reason"] for s in saved(session).values()} == {spray_weather.NO_STATION}


def test_a_reading_after_the_data_clock_is_not_used(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))
    evidence = spray_weather._evidence(
        farm.repo,
        spray_weather.Receipt(0, utc(6, 30), (farm.parcels["P5"].id,)),
        READER,
        utc(5, 59),
        datetime.now(UTC),
    )
    assert evidence["reason"] == spray_weather.NO_SLOT


def test_an_unknown_source_time_is_saved_as_unavailable(session, farm):
    evidence = spray_weather._evidence(
        farm.repo,
        spray_weather.Receipt(0, None, (farm.parcels["P5"].id,)),
        READER,
        utc(9),
        datetime.now(UTC) - timedelta(minutes=1),
    )
    assert evidence["status"] == "unavailable" and evidence["reason"] == spray_weather.NO_TIME


def test_without_the_stations_software_no_reader_is_composed():
    software = [m for m in discover() if m.name != "stations"]

    assert compose_spray_weather_reader(software) is None
    assert READER is not None  # discovered: the stations exact-slot reader


def test_the_saved_station_carries_its_public_provider_reference_never_the_file(
    session, farm, data_dir
):
    from farmcore.apps.stations import seed as stations_seed

    stations_seed.seed(session, scene=None, data_dir=data_dir)
    station = session.scalars(select(Asset).where(Asset.kind == "station")).one()
    for ts, values in hours(5, 6).items():
        for variable, value in values.items():
            session.add(Observation(asset_id=station.id, ts=ts, variable=variable, value=value))
    session.flush()

    run(farm)

    p5 = saved(session)["TSK1"]
    assert p5["station"]["name"] == "St-Émilion 33394002"
    provider = p5["station"]["provider"]
    assert (provider["source"], provider["key"]) == ("meteofrance", "33394002")
    assert provider["name"].startswith("Météo-France") and provider["licence"] == "Licence Ouverte"
    assert "file" not in str(p5) and ".csv" not in str(p5)
    projected = spray_weather.project("isoxml", machine_sources(session)["TSK1"].raw_payload)
    assert projected["station"]["provider"] == provider


def test_a_station_without_an_external_id_has_no_provider_reference(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6))

    run(farm)

    assert saved(session)["TSK1"]["station"]["provider"] is None


def test_an_hour_with_only_other_variables_is_no_reading(session, farm):
    add_station(session, farm.repo.farm_id, hours(5, 6, rr1_mm=None, t_c=None, ff_kmh=None))

    run(farm)  # the hour exists (humidity only): none of the three facts

    p5 = saved(session)["TSK1"]
    assert p5["status"] == "unavailable" and p5["reason"] == spray_weather.NO_SLOT


def test_capture_at_the_module_seam_is_a_no_op_when_nothing_is_new(session, farm):
    from sqlalchemy import func

    from farmcore.core.models.canonical import AccessLog

    add_station(session, farm.repo.farm_id, hours(5, 6))
    run(farm)
    src = machine_sources(session)["TSK1"]
    before = dict(src.raw_payload)
    audits = session.scalar(select(func.count()).select_from(AccessLog))
    receipt = spray_weather.Receipt(src.id, utc(9), (farm.parcels["P5"].id,))
    kwargs = {"reader": READER, "data_now": utc(10), "captured_at": utc(10)}

    spray_weather.capture(farm.repo, [], **kwargs)
    assert session.scalar(select(func.count()).select_from(AccessLog)) == audits  # no read
    spray_weather.capture(farm.repo, [receipt], **kwargs)  # already captured: kept as it was

    assert machine_sources(session)["TSK1"].raw_payload == before


def test_an_unknown_treatment_time_reads_as_time_unknown_on_the_card():
    record = {"v": 1, "status": "unavailable", "reason": spray_weather.NO_TIME}
    record |= {"parcel_ids": [1], "values": dict.fromkeys(spray_weather.FACTS)}
    entry = spray_weather.project("voice", {NS: record})

    line = spray_weather.describe(entry, "Europe/Paris", "P1")

    assert line == "Voice · P1 · treated time unknown: no saved weather (treatment time unknown)"
