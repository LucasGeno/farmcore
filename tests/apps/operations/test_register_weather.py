"""Saved spray weather on the owner register: JSON list, one CSV column, spray cards.

Evidence is written straight onto `LogSource.raw_payload` in the v1 shape (no capture code
path), so these tests prove only the read side: whitelisting, per-parcel filtering, all sources
in a stable order, words for unavailable and unsupported records, no mutation on GET, and the
default projection left as it was. Synthetic parcels, stations and values only.
"""

from __future__ import annotations

import copy
import csv
import io
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape

import pytest
from sqlalchemy import select

from farmcore.apps.operations import register, spray_weather
from farmcore.apps.operations import seed as ops_seed
from farmcore.core import clock, logs
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import LogSource

# 10 Apr 2024 06:00 UTC = 08:00 Paris (CEST); the machine file 20 min later; clock 2 days on.
T_VOICE = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
T_MACHINE = T_VOICE + timedelta(minutes=20)
NOW = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)
TRANSCRIPT = "synthetic transcript sprayed the vines"
FAKE_PATH = "synthetic-provider-path-91c"
VOICE = logs.SourceInput("voice", "tg:u13:1", {"transcript": TRANSCRIPT}, 0.9, logs.RANK_VOICE)
MACHINE = logs.SourceInput(
    "isoxml", "isoxml:u13:TSK1", {"text": TRANSCRIPT}, 1.0, logs.RANK_MACHINE
)
PROJECTED = {
    "source",
    "source_label",
    "v",
    "policy",
    "status",
    "reason",
    "source_time",
    "parcel_ids",
    "slot",
    "station",
    "observed_at",
    "rain_interval",
    "values",
    "units",
    "captured_at",
    "data_clock",
}


def _repo(session, farm) -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")


def _write(session, demo, products, source, *, parcels, ts):
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p.id for p in parcels],
        ts_start=ts,
        ts_end=None,
        quantities=[
            logs.QuantityInput(
                "rate", Decimal("2"), "kg/ha", product_id=products["copper-contact"].id
            )
        ],
        source=source,
        actor="owner",
        occurred_at=ts,
    )
    return log


def _evidence(ts, parcels, *, values=(14.5, 9.0, 0.0), reason=None):
    """A v1 record as `spray_weather` writes it, plus keys the projection must drop."""
    slot = ts.replace(minute=0)
    record = {
        "v": spray_weather.VERSION,
        "policy": spray_weather.POLICY,
        "status": spray_weather.UNAVAILABLE,
        "reason": reason,
        "source_time": ts.isoformat(),
        "parcel_ids": [p.id for p in parcels],
        "slot": slot.isoformat(),
        "station": None,
        "observed_at": None,
        "rain_interval": None,
        "values": dict.fromkeys(spray_weather.FACTS),
        "units": dict(spray_weather.UNITS),
        "captured_at": NOW.isoformat(),
        "data_clock": NOW.isoformat(),
        "provider_path": FAKE_PATH,
    }
    if reason is not None:
        return record
    facts = dict(zip(spray_weather.FACTS, values, strict=True))
    known = [v for v in facts.values() if v is not None]
    return {
        **record,
        "status": spray_weather.CAPTURED if len(known) == 3 else spray_weather.PARTIAL,
        "station": {"id": 7, "name": "Synthetic station", "path": FAKE_PATH},
        "observed_at": slot.isoformat(),
        # as capture writes it: the rain hour only when rain was read
        "rain_interval": [(slot - timedelta(hours=1)).isoformat(), slot.isoformat()]
        if facts["rr1_mm"] is not None
        else None,
        "values": facts,
    }


def _save(session, source: LogSource, evidence) -> None:
    source.raw_payload = {**(source.raw_payload or {}), spray_weather.NAMESPACE: evidence}
    session.flush()


def _sources(session, log) -> list[LogSource]:
    return list(
        session.scalars(select(LogSource).where(LogSource.log_id == log.id).order_by(LogSource.id))
    )


def _at(session, settings, t=NOW):
    clock.set_replay(session, t, settings)
    session.commit()


def _merged(session, demo):
    """Voice on P1 + P2, then a machine file on P1 only merged into the same log; each source
    keeps its own snapshot (the machine's is partial: no rain)."""
    products = ops_seed.ensure_products(session)
    p1, p2 = demo.parcels
    log = _write(session, demo, products, VOICE, parcels=[p1, p2], ts=T_VOICE)
    merged = _write(session, demo, products, MACHINE, parcels=[p1], ts=T_MACHINE)
    assert merged.id == log.id
    voice, machine = _sources(session, log)
    assert (voice.source_type, machine.source_type) == ("voice", "isoxml")
    _save(session, voice, _evidence(T_VOICE, [p1, p2]))
    _save(session, machine, _evidence(T_MACHINE, [p1], values=(15.0, 12.0, None)))
    return log


def _csv(client) -> tuple[list[str], list[dict[str, str]]]:
    text = client.get("/operations/register.csv").text
    header = next(csv.reader(io.StringIO(text)))
    return header, list(csv.DictReader(io.StringIO(text)))


def _card(html: str, log_id: int) -> str:
    """The words of one spray card (up to the next card, or the EU table after the last)."""
    [card] = [c for c in html.split('data-spray="')[1:] if c.startswith(f'{log_id}"')]
    card = card.split("<details", 1)[0]
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", card)).split())


def test_owner_json_rows_carry_every_applicable_snapshot_whitelisted_in_source_order(
    client, session, settings, demo
):
    log = _merged(session, demo)
    _at(session, settings)

    resp = client.get("/operations/register.json")

    assert resp.status_code == 200
    rows = resp.json()
    assert all(
        list(r) == [*register.CSV_FIELDS, register.SAVED_WEATHER, register.PARCEL_ALIASES]
        for r in rows
    )
    by_alias = {r["parcel_alias"]: r["saved_weather"] for r in rows if r["log_id"] == log.id}
    # The machine file covered P1 only: its snapshot never spreads onto P2 through the merge.
    assert [e["source_label"] for e in by_alias["P1"]] == ["Voice", "Machine file"]
    assert [e["source_label"] for e in by_alias["P2"]] == ["Voice"]
    voice, machine = by_alias["P1"]
    assert all(set(e) == PROJECTED for e in (voice, machine))
    assert voice["source_time"] == T_VOICE.isoformat()  # its own time, not the merged log's
    assert machine["source_time"] == T_MACHINE.isoformat()
    assert machine["status"] == "partial" and machine["values"]["rr1_mm"] is None
    # whitelisted identity: the stored `path` never leaves; no provider ref was saved here
    assert voice["station"] == {"id": 7, "name": "Synthetic station", "provider": None}
    assert voice["units"] == {"t_c": "°C", "ff_kmh": "km/h", "rr1_mm": "mm"}
    for leak in (TRANSCRIPT, FAKE_PATH, "tg:u13:1", "isoxml:u13:TSK1", '"text"', "external_key"):
        assert leak not in resp.text
    assert client.get("/operations/register.json").json() == rows  # stable order


def test_owner_csv_appends_one_saved_weather_column_labelled_per_source(
    client, session, settings, demo
):
    log = _merged(session, demo)
    _at(session, settings)

    header, rows = _csv(client)

    assert header[: len(register.CSV_FIELDS)] == list(register.CSV_FIELDS)
    assert header == [*register.CSV_FIELDS, "saved_weather"]
    cells = {r["parcel_alias"]: r["saved_weather"] for r in rows if r["log_id"] == str(log.id)}
    assert cells["P1"] == (
        "Voice · treated 10 Apr 08:00: Synthetic station reading 10 Apr 08:00, saved with the "
        "record: 14.5 °C, wind 9 km/h, rain 0 mm 07:00–08:00 | Machine file · treated "
        "10 Apr 08:20: Synthetic station reading 10 Apr 08:00, saved with the record: 15 °C, "
        "wind 12 km/h, rain (hour ending) missing"
    )
    assert "Voice" in cells["P2"] and "Machine file" not in cells["P2"]
    assert FAKE_PATH not in cells["P1"] and TRANSCRIPT not in cells["P1"]


def test_owner_csv_writes_the_saved_weather_header_for_an_empty_register(
    client, session, settings, demo
):
    _at(session, settings)

    text = client.get("/operations/register.csv").text

    assert text.splitlines() == [",".join([*register.CSV_FIELDS, "saved_weather"])]


def test_old_records_have_a_blank_cell_and_no_saved_weather_on_the_card(
    client, session, settings, demo
):
    products = ops_seed.ensure_products(session)
    log = _write(session, demo, products, VOICE, parcels=demo.parcels, ts=T_VOICE)
    _at(session, settings)

    rows = client.get("/operations/register.json").json()
    _, csv_rows = _csv(client)
    page = client.get("/operations/register").text

    assert {tuple(r["saved_weather"]) for r in rows} == {()}
    assert {r["saved_weather"] for r in csv_rows} == {""}
    assert "No saved weather" in _card(page, log.id)


def test_unavailable_and_unsupported_records_render_their_words(client, session, settings, demo):
    products = ops_seed.ensure_products(session)
    p1, p2 = demo.parcels
    missing = _write(session, demo, products, VOICE, parcels=[p1], ts=T_VOICE)
    later = logs.SourceInput("voice", "tg:u13:2", {}, 0.9, logs.RANK_VOICE)
    odd = _write(session, demo, products, later, parcels=[p2], ts=T_VOICE + timedelta(days=1))
    [m_source], [o_source] = _sources(session, missing), _sources(session, odd)
    _save(session, m_source, _evidence(T_VOICE, [p1], reason=spray_weather.NO_STATION))
    _save(session, o_source, {"v": 99, "anything": FAKE_PATH})
    _at(session, settings)

    rows = {r["log_id"]: r["saved_weather"] for r in client.get("/operations/register.json").json()}
    _, csv_rows = _csv(client)
    cells = {r["log_id"]: r["saved_weather"] for r in csv_rows}
    page = client.get("/operations/register").text

    assert rows[missing.id][0]["status"] == "unavailable"
    assert rows[missing.id][0]["reason"] == "no_station"
    assert rows[odd.id] == [
        {"source": "voice", "source_label": "Voice", "v": 99, "status": "unsupported"}
    ]
    assert (
        cells[str(missing.id)]
        == "Voice · treated 10 Apr 08:00: no saved weather (no weather station)"
    )
    assert "unsupported" in cells[str(odd.id)]
    assert "no weather station" in _card(page, missing.id)
    assert "unsupported" in _card(page, odd.id)
    assert FAKE_PATH not in page


def test_card_lists_each_source_snapshot_once_and_the_table_has_the_column(
    client, session, settings, demo
):
    log = _merged(session, demo)
    _at(session, settings)

    page = client.get("/operations/register").text

    card = _card(page, log.id)
    # each line names its source's own parcels and treatment time, not the card's
    assert card.count("Voice · P1, P2 · treated 10 Apr 08:00: Synthetic station reading") == 1
    assert card.count("Machine file · P1 · treated 10 Apr 08:20: Synthetic station reading") == 1
    assert "Machine file · P1, P2" not in card
    assert "No saved weather" not in card
    [head] = re.findall(r"<table[^>]*data-register.*?<thead>(.*?)</thead>", page, re.S)
    assert re.findall(r"<th[^>]*>(.*?)</th>", head)[-2:] == ["checks", "saved_weather"]


def test_reading_the_register_never_changes_a_saved_payload(client, session, settings, demo):
    log = _merged(session, demo)
    _at(session, settings)
    before = [copy.deepcopy(s.raw_payload) for s in _sources(session, log)]

    for path in ("/operations/register", "/operations/register.csv", "/operations/register.json"):
        assert client.get(path).status_code == 200

    session.expire_all()
    assert [s.raw_payload for s in _sources(session, log)] == before


def test_default_projection_and_csv_are_unchanged_with_evidence_present(session, settings, demo):
    _merged(session, demo)
    repo = _repo(session, demo.farm)

    plain = register.register_rows(repo, NOW)
    checked = register.register_rows(repo, NOW, checks=True)

    assert all(list(r) == list(register.FIELDS) for r in plain)
    assert all(list(r) == list(register.CSV_FIELDS) for r in checked)
    assert next(csv.reader(io.StringIO(register.to_csv(checked)))) == list(register.CSV_FIELDS)
    assert register.to_csv([]).splitlines() == [",".join(register.CSV_FIELDS)]
    assert (*register.FIELDS, "checks") == register.CSV_FIELDS


def test_a_merged_card_attributes_each_machine_snapshot_to_its_own_parcel(
    client, session, settings, demo
):
    # QA: voice P1 + P2, machine P1 13.5 °C / 0 mm, machine P2 16.25 °C / rain missing, one card
    products = ops_seed.ensure_products(session)
    p1, p2 = demo.parcels
    log = _write(session, demo, products, VOICE, parcels=[p1, p2], ts=T_VOICE)
    second = logs.SourceInput("isoxml", "isoxml:u13:TSK2", {}, 1.0, logs.RANK_MACHINE)
    t_p2 = T_MACHINE + timedelta(minutes=30)
    assert _write(session, demo, products, MACHINE, parcels=[p1], ts=T_MACHINE).id == log.id
    assert _write(session, demo, products, second, parcels=[p2], ts=t_p2).id == log.id
    voice, machine_p1, machine_p2 = _sources(session, log)
    _save(session, voice, _evidence(T_VOICE, [p1, p2]))
    _save(session, machine_p1, _evidence(T_MACHINE, [p1], values=(13.5, 4.0, 0.0)))
    _save(session, machine_p2, _evidence(t_p2, [p2], values=(16.25, 6.0, None)))
    _at(session, settings)

    card = _card(client.get("/operations/register").text, log.id)

    assert "Machine file · P1 · treated 10 Apr 08:20: Synthetic station reading" in card
    assert "Machine file · P2 · treated 10 Apr 08:50: Synthetic station reading" in card
    p1_line = card.split("Machine file · P1 · treated", 1)[1].split("Machine file", 1)[0]
    assert "13.5 °C" in p1_line and "rain 0 mm" in p1_line and "16.25" not in p1_line
    p2_line = card.split("Machine file · P2 · treated", 1)[1]
    assert "16.25 °C" in p2_line and "rain (hour ending) missing" in p2_line


def _valid_record(**changes):
    p_id = changes.pop("_parcel")
    record = _evidence(T_VOICE, [], values=(14.5, 9.0, 0.0))
    record["parcel_ids"] = [p_id]
    record.pop("provider_path", None)
    record.update(changes)
    return record


MALFORMED = {  # planning QA D3 probes: each stored as real JSON on the source
    "namespace_none": None,
    "v_true": {"v": True},
    "v_float": {"v": 1.0},
    "v_string": {"v": "1"},
    "status_unknown": {"status": "weird"},
    "status_missing": {"status": None},
    "values_list": {"values": [1, 2, 3]},
    "values_text": {"values": "13.5"},
    "t_c_text": {"values": {"t_c": "13.5", "ff_kmh": 9.0, "rr1_mm": 0.0}},
    "t_c_bool": {"values": {"t_c": True, "ff_kmh": 9.0, "rr1_mm": 0.0}},
    "values_missing_when_captured": {"values": None},
    "parcel_ids_int": {"parcel_ids": 12},
    "parcel_ids_text": {"parcel_ids": "7"},
    "parcel_ids_text_list": {"parcel_ids": ["7"]},
    "parcel_ids_missing": {"parcel_ids": None},
    "source_time_bad": {"source_time": "not-a-date"},
    "source_time_number": {"source_time": 12},
    "observed_at_bad": {"observed_at": "not-a-date"},
    "rain_interval_bad": {"rain_interval": "x"},
    "captured_with_a_missing_fact": {"values": {"t_c": 14.5, "ff_kmh": None, "rr1_mm": 0.0}},
    "unavailable_unknown_reason": {"status": "unavailable", "reason": "because"},
    # a captured / partial record must carry its reading time and no unavailable reason
    "captured_with_a_reason": {"reason": "no_station"},
    "partial_without_observed_at": {
        "status": "partial",
        "values": {"t_c": 14.5, "ff_kmh": 9.0, "rr1_mm": None},
        "rain_interval": None,
        "observed_at": None,
    },
    # second review: unhashable reason, overflowing integer, nested identity values
    "unavailable_reason_object": {"status": "unavailable", "reason": {}},
    "unavailable_reason_list": {"status": "unavailable", "reason": []},
    "t_c_overflowing_integer": {"values": {"t_c": 10**400, "ff_kmh": 9.0, "rr1_mm": 0.0}},
    "station_id_object": {"station": {"id": {"transcript": TRANSCRIPT}, "name": "Stn"}},
    "station_name_object": {"station": {"id": 7, "name": {"transcript": TRANSCRIPT}}},
    "station_not_a_dict": {"station": "Stn /home/x"},
    "provider_name_object": {
        "station": {"id": 7, "name": "Stn", "provider": {"name": {"transcript": TRANSCRIPT}}}
    },
    "provider_not_a_dict": {"station": {"id": 7, "name": "Stn", "provider": "/home/p.csv"}},
}


@pytest.mark.parametrize("case", list(MALFORMED))
def test_a_malformed_saved_record_reads_as_unsupported_on_every_view(
    client, session, settings, demo, case
):
    products = ops_seed.ensure_products(session)
    p1, _ = demo.parcels
    log = _write(session, demo, products, VOICE, parcels=[p1], ts=T_VOICE)
    [source] = _sources(session, log)
    change = MALFORMED[case]
    if change is None:
        stored = None
    else:
        stored = _valid_record(_parcel=p1.id)
        for key, value in change.items():
            if value is None and key != "status":
                stored.pop(key)
            else:
                stored[key] = value
        if case == "status_missing":
            stored.pop("status")
    _save(session, source, stored)
    _at(session, settings)
    before = copy.deepcopy(_sources(session, log)[0].raw_payload)

    rows = client.get("/operations/register.json")
    text = client.get("/operations/register.csv")
    page = client.get("/operations/register")

    assert (rows.status_code, text.status_code, page.status_code) == (200, 200, 200)
    [entry] = [e for r in rows.json() if r["log_id"] == log.id for e in r["saved_weather"]]
    assert set(entry) == {"source", "source_label", "v", "status"}  # no trusted values
    assert entry["status"] == "unsupported" and entry["v"] in (None, 1)
    [cell] = [r["saved_weather"] for r in csv.DictReader(io.StringIO(text.text))]
    assert cell.startswith("Voice: saved weather in an unsupported format")
    assert "°C" not in cell
    for body in (rows.text, text.text, page.text):
        assert TRANSCRIPT not in body and "/home/" not in body  # no raw nested value echoed
    card = _card(page.text, log.id)
    assert "saved weather in an unsupported format" in card and "No saved weather" not in card
    assert _sources(session, log)[0].raw_payload == before  # never rewritten or backfilled


def test_a_valid_record_still_reads_as_saved_weather(client, session, settings, demo):
    products = ops_seed.ensure_products(session)
    p1, _ = demo.parcels
    log = _write(session, demo, products, VOICE, parcels=[p1], ts=T_VOICE)
    [source] = _sources(session, log)
    _save(session, source, _valid_record(_parcel=p1.id))
    _at(session, settings)

    [entry] = [
        e for r in client.get("/operations/register.json").json() for e in r["saved_weather"]
    ]

    assert entry["status"] == "captured" and entry["values"]["t_c"] == 14.5
