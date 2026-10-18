"""P-D: exported machine intervals belong to the task's parcel, not every merged parcel."""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from farmcore.apps.operations import isoxml, isoxml_demo
from farmcore.apps.share import service
from farmcore.core import clock
from farmcore.core.models.canonical import LogSource
from tests.apps.operations import isoxml_farm
from tests.apps.operations.isoxml_farm import CLOCK, local, voice_spray

farm = isoxml_farm.farm
TIMES = {"P5": "07:12", "P6": "08:03"}
INTERVALS = {
    "P5": ("2024-04-10T05:12:05Z", "2024-04-10T05:58:40Z"),
    "P6": ("2024-04-10T06:03:10Z", "2024-04-10T06:31:55Z"),
}


@pytest.fixture
def merged(session, settings, farm):
    log = voice_spray(session, farm)
    isoxml.import_taskdata(farm.repo, isoxml_demo.demo_taskdata(), now=local(CLOCK), actor="test")
    clock.set_replay(session, local(CLOCK), settings)
    session.commit()
    return log


@pytest.fixture
def export_urls(session, farm, merged, request):
    if request.param == "owner":
        return "/operations/register.json", "/share/ngsi-ld.json"
    grant = service.create_grant(
        farm.repo,
        grantee="Interval test adviser",
        preset="adviser_logs",
        purpose="parcel interval test",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()
    return f"/share/as/{grant.id}/register.json", f"/share/as/{grant.id}/ngsi-ld.json"


@pytest.mark.parametrize("export_urls", ["owner", "grantee"], indirect=True)
def test_merged_task_register_json_uses_each_parcels_machine_time(client, merged, export_urls):
    response = client.get(export_urls[0])
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 4
    assert {(r["parcel_alias"], r["start_time"]) for r in rows} == set(TIMES.items())
    assert {r["log_id"] for r in rows} == {merged.id}


def test_merged_task_register_csv_uses_each_parcels_machine_time(client, merged):
    response = client.get("/operations/register.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 4
    assert {(r["parcel_alias"], r["start_time"]) for r in rows} == set(TIMES.items())


@pytest.mark.parametrize("export_urls", ["owner", "grantee"], indirect=True)
def test_merged_task_ngsi_uses_each_parcels_machine_interval(client, farm, merged, export_urls):
    response = client.get(export_urls[1])
    assert response.status_code == 200
    entities = [e for e in response.json() if e["type"] == "AgriParcelOperation"]
    expected = {
        f"urn:ngsi-ld:AgriParcel:farmcore-{farm.parcels[alias].id}": interval
        for alias, interval in INTERVALS.items()
    }
    assert len(entities) == 4
    for entity in entities:
        assert (entity["startedAt"], entity["endedAt"]) == expected[entity["hasAgriParcel"]]
        assert entity["plannedStartAt"] == entity["startedAt"]
        assert entity["plannedEndAt"] == entity["endedAt"]
    # Merge storage/ranking and doses stay log-level; only the export interval changes.
    assert merged.ts_start == local("2024-04-10 07:12:05")
    assert merged.ts_end == local("2024-04-10 07:58:40")
    assert sorted(str(q.value) for q in merged.quantities) == ["1.94", "2.06"]


def test_legacy_receipts_keep_log_interval_even_with_saved_weather_ids(
    client, session, farm, merged
):
    before = {}
    for source in merged.sources:
        if source.source_type == "isoxml":
            payload = dict(source.raw_payload)
            payload.pop("parcel_ids")
            assert payload["farmcore.spray_weather"]["parcel_ids"]
            source.raw_payload = payload
            before[source.id] = payload
    session.commit()
    result = isoxml.import_taskdata(
        farm.repo, isoxml_demo.demo_taskdata(), now=local(CLOCK), actor="test"
    )
    session.commit()
    assert all(task.unchanged for task in result.tasks)
    assert {
        session.get(LogSource, key).id: session.get(LogSource, key).raw_payload for key in before
    } == before
    rows = client.get("/operations/register.json").json()
    assert {r["start_time"] for r in rows} == {"07:12"}
    entities = client.get("/share/ngsi-ld.json").json()
    assert {
        (e["startedAt"], e["endedAt"]) for e in entities if e["type"] == "AgriParcelOperation"
    } == {INTERVALS["P5"]}


def test_source_rank_is_per_parcel_and_equal_ranks_keep_the_first_receipt(
    client, session, farm, merged
):
    for key, alias, rank, start, end in (
        ("first", "P6", 40, "09:00", "09:15"),
        ("tie", "P6", 40, "09:20", "09:30"),
        ("other-parcel", "P5", 50, "09:40", "09:50"),
        ("lower", "P6", 35, "09:55", "09:59"),
    ):
        session.add(
            LogSource(
                log_id=merged.id,
                source_type="file",
                external_key=key,
                precision_rank=rank,
                raw_payload={
                    "parcel_ids": [farm.parcels[alias].id],
                    "ts_start": local(f"2024-04-10 {start}").isoformat(),
                    "ts_end": local(f"2024-04-10 {end}").isoformat(),
                },
            )
        )
        session.flush()
    session.commit()
    rows = client.get("/operations/register.json").json()
    assert {(r["parcel_alias"], r["start_time"]) for r in rows} == {
        ("P5", "09:40"),
        ("P6", "09:00"),
    }
    entities = client.get("/share/ngsi-ld.json").json()
    p6 = f"urn:ngsi-ld:AgriParcel:farmcore-{farm.parcels['P6'].id}"
    assert {
        (e["startedAt"], e["endedAt"])
        for e in entities
        if e["type"] == "AgriParcelOperation" and e["hasAgriParcel"] == p6
    } == {("2024-04-10T07:00:00Z", "2024-04-10T07:15:00Z")}


@pytest.mark.parametrize("time_known", [True, False])
def test_parcel_local_date_and_open_end_do_not_borrow_from_the_log(
    client, session, settings, farm, merged, time_known
):
    source = next(
        s
        for s in merged.sources
        if s.source_type == "isoxml" and s.raw_payload["partfield"] == "Peyrot"
    )
    source.raw_payload = {
        **source.raw_payload,
        "ts_start": "2024-04-10T22:03:10Z",
        "ts_end": None,
    }
    merged.props = {**merged.props, "time_known": time_known}
    clock.set_replay(session, local("2024-04-11 10:00"), settings)
    session.commit()
    rows = client.get("/operations/register.json").json()
    assert {(r["date"], r["start_time"]) for r in rows if r["parcel_alias"] == "P6"} == {
        ("2024-04-11", "00:03" if time_known else "")
    }
    p6 = f"urn:ngsi-ld:AgriParcel:farmcore-{farm.parcels['P6'].id}"
    entities = [
        e
        for e in client.get("/share/ngsi-ld.json").json()
        if e["type"] == "AgriParcelOperation" and e["hasAgriParcel"] == p6
    ]
    assert len(entities) == 2
    for entity in entities:
        assert entity["startedAt"] == "2024-04-10T22:03:10Z"
        assert entity["plannedEndAt"] == entity["startedAt"]
        assert "endedAt" not in entity


@pytest.mark.parametrize("bad_end", ["", False, 0, "invalid", "2024-04-10T06:00:00Z"])
def test_malformed_higher_rank_interval_does_not_override_valid_machine_evidence(
    client, session, farm, merged, bad_end
):
    session.add(
        LogSource(
            log_id=merged.id,
            source_type="file",
            external_key="malformed",
            precision_rank=99,
            raw_payload={
                "parcel_ids": [farm.parcels["P6"].id],
                "ts_start": "2024-04-10T07:00:00Z",
                "ts_end": bad_end,
            },
        )
    )
    session.commit()
    rows = client.get("/operations/register.json").json()
    assert {(r["parcel_alias"], r["start_time"]) for r in rows} == set(TIMES.items())
