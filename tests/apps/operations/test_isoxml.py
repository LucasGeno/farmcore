"""ISOXML TaskData: the synthetic demo file, `parse`, and `import_taskdata` (merge with voice).

Expected values are literals, not recomputed from the module.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from farmcore.apps.operations import isoxml, isoxml_demo, register
from farmcore.core import clock
from farmcore.core.access import ScopeDenied, ScopedRepository
from farmcore.core.models.canonical import (
    DataGrant,
    Event,
    Log,
    LogSource,
    Parcel,
    Product,
    Quantity,
)
from tests.apps.operations import isoxml_farm
from tests.apps.operations.isoxml_farm import CLOCK, SQUARE, TZ, Farm, local, voice_spray


def test_parse_demo_file_returns_two_completed_tasks_at_machine_times():
    tasks = isoxml.parse(isoxml_demo.demo_taskdata())

    assert [(t.partfield, t.ts_start, t.ts_end) for t in tasks] == [
        (
            "Les Granges",
            datetime(2024, 4, 10, 5, 12, 5, tzinfo=UTC),
            datetime(2024, 4, 10, 5, 58, 40, tzinfo=UTC),
        ),
        (
            "Peyrot",
            datetime(2024, 4, 10, 6, 3, 10, tzinfo=UTC),
            datetime(2024, 4, 10, 6, 31, 55, tzinfo=UTC),
        ),
    ]
    for task in tasks:
        assert [(p.name, p.value, p.unit) for p in task.products] == [
            ("Bouillie bordelaise 20 WG", Decimal("1.94"), "kg/ha"),
            ("Soufre mouillable 80 WG", Decimal("2.06"), "kg/ha"),
        ]


def test_demo_file_is_iso11783_taskdata_with_two_partfields_products_and_completed_tasks():
    root = ET.fromstring(isoxml_demo.demo_taskdata())

    assert root.tag == "ISO11783_TaskData"
    assert root.get("VersionMajor") == "4"
    assert root.get("DataTransferOrigin") == "1"
    assert sorted(p.get("C") for p in root.findall("PFD")) == ["Les Granges", "Peyrot"]
    assert sorted(p.get("D") for p in root.findall("PFD")) == ["22100", "40700"]
    assert [p.get("B") for p in root.findall("PDT")] == [
        "Bouillie bordelaise 20 WG",
        "Soufre mouillable 80 WG",
    ]
    assert [t.get("G") for t in root.findall("TSK")] == ["4", "4"]


def _zipped(xml: bytes, member: str = "TASKDATA/TASKDATA.XML") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, xml)
    return buf.getvalue()


def test_parse_accepts_the_same_file_zipped():
    xml = isoxml_demo.demo_taskdata()

    assert isoxml.parse(_zipped(xml)) == isoxml.parse(xml)


MINIMAL = """<?xml version="1.0" encoding="UTF-8"?>
<ISO11783_TaskData VersionMajor="4" VersionMinor="3" DataTransferOrigin="1">
  <PFD A="PFD1" C="Les Granges" D="40700"/>
  <PDT A="PDT1" B="Some product"/>
  {tasks}
</ISO11783_TaskData>"""


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"<ISO11783_TaskData><TSK", "not valid XML"),
        (b"PK\x03\x04 not really a zip", "not a readable zip"),
        (b"<farm/>", "not an ISO 11783 TaskData file"),
        (MINIMAL.format(tasks="").encode(), "no task"),
        (
            MINIMAL.format(tasks='<TSK A="TSK9" E="PFD1" G="4"/>').encode(),
            "TSK9 has no start time",
        ),
        (_zipped(b"<x/>", "OTHER.XML"), "no TASKDATA.XML"),
    ],
)
def test_parse_rejects_unreadable_files_with_a_plain_message(data, message):
    with pytest.raises(isoxml.IsoxmlError, match=message):
        isoxml.parse(data)


def test_parse_converts_volume_rates_and_lists_ignored_ddis_and_binary_logs():
    task = """<TSK A="TSK1" E="PFD1" G="4">
      <TZN A="1"><PDV A="0001" B="15000" C="PDT1"/><PDV A="0010" B="5" C="PDT1"/></TZN>
      <TIM A="2024-04-10T07:00:00" D="4"/>
      <TLG A="TLG00001"/>
    </TSK>
    <TSK A="TSK2" E="PFD1" G="1"><TIM A="2024-04-11T07:00:00Z"/></TSK>"""

    [record] = isoxml.parse(MINIMAL.format(tasks=task).encode())

    assert record.ts_start == datetime(2024, 4, 10, 5, 0, tzinfo=UTC)  # no offset: local time
    assert record.ts_end is None
    assert [(p.name, p.value, p.unit) for p in record.products] == [
        ("Some product", Decimal("150"), "l/ha")
    ]
    assert any("0010" in n for n in record.notes)
    assert any("TLG00001" in n for n in record.notes)


# --- import_taskdata: the demo farm's P5 Les Granges and P6 Peyrot (synthetic test parcels) ---

farm = isoxml_farm.farm  # fixture


def run_import(farm: Farm, data: bytes | None = None, clock: str = CLOCK) -> isoxml.ImportResult:
    data = isoxml_demo.demo_taskdata() if data is None else data
    return isoxml.import_taskdata(farm.repo, data, now=local(clock), actor="test")


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_import_merges_both_tasks_into_the_voice_log_with_machine_time_and_dose(session, farm):
    voice = voice_spray(session, farm)

    result = run_import(farm)

    [log] = session.scalars(select(Log).where(Log.type == "spray")).all()
    assert log.id == voice.id
    assert {p.alias for p in log.parcels} == {"P5", "P6"}
    assert sorted(s.source_type for s in log.sources) == ["isoxml", "isoxml", "voice"]
    assert log.ts_start == local("2024-04-10 07:12:05")
    assert sorted((q.product_id, q.value, q.unit) for q in log.quantities) == sorted(
        [
            (farm.products["copper"].id, Decimal("1.94"), "kg/ha"),
            (farm.products["sulphur"].id, Decimal("2.06"), "kg/ha"),
        ]
    )
    merged = session.scalars(
        select(Event).where(Event.kind == "log.merged").order_by(Event.id)
    ).all()
    assert [(e.res_id, e.payload["source_types"], e.payload["replaced"]) for e in merged] == [
        (farm.parcels["P5"].id, ["isoxml"], True),
        (farm.parcels["P6"].id, ["isoxml"], True),
        (farm.parcels["P5"].id, ["isoxml"], False),
        (farm.parcels["P6"].id, ["isoxml"], False),
    ]
    assert [(t.task_id, t.log_id, t.merged, t.replaced) for t in result.tasks] == [
        ("TSK1", voice.id, True, True),
        ("TSK2", voice.id, True, False),
    ]
    assert result.tasks[-1].badges == ("voice", "isoxml")


def test_without_a_voice_log_each_task_becomes_its_own_machine_log(session, farm):
    result = run_import(farm)

    spray_logs = session.scalars(select(Log).where(Log.type == "spray").order_by(Log.id)).all()
    assert [{p.alias for p in log.parcels} for log in spray_logs] == [{"P5"}, {"P6"}]
    assert [log.ts_start for log in spray_logs] == [
        local("2024-04-10 07:12:05"),
        local("2024-04-10 08:03:10"),
    ]
    assert [(t.merged, t.replaced, t.badges) for t in result.tasks] == [
        (False, False, ("isoxml",)),
        (False, False, ("isoxml",)),
    ]
    kinds = session.scalars(select(Event.kind).order_by(Event.id)).all()
    assert kinds == ["log.created", "log.created"]


TABLES = (Log, LogSource, Quantity, Event, Product)


def test_reimporting_the_same_bytes_changes_nothing(session, farm):
    voice_spray(session, farm)
    data = isoxml_demo.demo_taskdata()
    run_import(farm, data)
    before = {m.__name__: count(session, m) for m in TABLES}

    again = run_import(farm, data)

    assert {m.__name__: count(session, m) for m in TABLES} == before
    assert [(t.merged, t.replaced, t.unchanged) for t in again.tasks] == [
        (True, False, True),
        (True, False, True),
    ]


def test_a_zip_whose_taskdata_expands_past_the_cap_is_refused(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("TASKDATA/TASKDATA.XML", isoxml_demo.demo_taskdata() + b" " * 5_000)
    monkeypatch.setattr(isoxml, "MAX_XML_BYTES", 4_000)

    with pytest.raises(isoxml.IsoxmlError, match="larger than"):
        isoxml.parse(buf.getvalue())


@pytest.mark.parametrize(
    ("clock", "data", "message"),
    [
        ("2024-04-10 08:00", None, "later than the replay clock — step the clock past 08:03"),
        (
            CLOCK,
            isoxml_demo.demo_taskdata().replace(b'C="Peyrot"', b'C="Le Clos"'),
            "unknown partfield.*Le Clos.*P5 Les Granges, P6 Peyrot",
        ),
    ],
)
def test_a_late_task_or_unknown_partfield_writes_nothing(session, farm, clock, data, message):
    voice_spray(session, farm)
    before = {m.__name__: count(session, m) for m in TABLES}

    with pytest.raises(isoxml.IsoxmlError, match=message):
        run_import(farm, data, clock)

    assert {m.__name__: count(session, m) for m in TABLES} == before


def test_an_unknown_product_is_a_labelled_quantity_without_product_id(session, farm):
    data = isoxml_demo.demo_taskdata().replace(
        b'B="Soufre mouillable 80 WG"', b'B="Mystery tank mix"'
    )
    products = count(session, Product)

    run_import(farm, data)

    rows = session.scalars(select(Quantity).order_by(Quantity.id)).all()
    assert [(q.product_id is None, q.label) for q in rows] == [
        (False, "BOUILLIE BORDELAISE RSR DISPERSS NC"),
        (True, "Mystery tank mix"),
    ] * 2
    assert count(session, Product) == products


def test_a_product_matches_the_shortlist_by_its_active_substance_word(session, farm):
    data = isoxml_demo.demo_taskdata().replace(b'B="Bouillie bordelaise 20 WG"', b'B="CUIVRE 50"')

    run_import(farm, data)

    ids = {q.product_id for q in session.scalars(select(Quantity))}
    assert ids == {farm.products["copper"].id, farm.products["sulphur"].id}


def test_a_logs_write_grant_with_parcel_names_can_import(session, farm):
    voice_spray(session, farm)
    grant = DataGrant(
        farm_id=farm.repo.farm_id,
        grantee="sprayer",
        scopes=["logs:write", "fields:read"],
        purpose="test",
    )
    session.add(grant)
    session.flush()
    writer = ScopedRepository(session, farm.repo.farm_id, grant, "sprayer", "test")

    result = isoxml.import_taskdata(
        writer, isoxml_demo.demo_taskdata(), now=local(CLOCK), actor="sprayer"
    )

    assert [(t.merged, t.replaced) for t in result.tasks] == [(True, True), (True, False)]


def test_import_needs_the_logs_write_scope(session, farm):
    grant = DataGrant(
        farm_id=farm.repo.farm_id, grantee="reader", scopes=["logs:read"], purpose="test"
    )
    session.add(grant)
    session.flush()
    reader = ScopedRepository(session, farm.repo.farm_id, grant, "reader", "test")

    with pytest.raises(ScopeDenied):
        isoxml.import_taskdata(
            reader, isoxml_demo.demo_taskdata(), now=local(CLOCK), actor="reader"
        )
    assert count(session, Log) == 0


@pytest.mark.parametrize("unknown_only", [False, True])
def test_unknown_imported_product_survives_register_and_http_exports(
    session,
    farm,
    client,
    settings,
    unknown_only,
):
    data = isoxml_demo.demo_taskdata().replace(
        b'B="Soufre mouillable 80 WG"', b'B="Mystery tank mix"'
    )
    if unknown_only:
        root = ET.fromstring(data)
        for task in root.findall("TSK"):
            zone = task.find("TZN")
            zone.remove(zone.findall("PDV")[0])
        data = ET.tostring(root)
    run_import(farm, data)
    rows = register.register_rows(farm.repo, local(CLOCK), checks=True)
    mystery = [r for r in rows if r["product_name"] == "Mystery tank mix"]
    assert [(r["parcel_alias"], Decimal(r["dose"]), r["dose_unit"]) for r in mystery] == [
        ("P5", Decimal("2.06"), "kg/ha"),
        ("P6", Decimal("2.06"), "kg/ha"),
    ]
    assert all(r["active_substance"] == r["authorisation_no"] == "" for r in mystery)
    clock.set_replay(session, local(CLOCK), settings)
    session.commit()
    json_response = client.get("/operations/register.json")
    csv_response = client.get("/operations/register.csv")
    assert json_response.status_code == csv_response.status_code == 200
    json_rows = json_response.json()
    csv_rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(json_rows) == len(csv_rows) == (2 if unknown_only else 4)
    fields = ("product_name", "dose", "dose_unit", "parcel_alias")
    assert [tuple(r[f] for f in fields) for r in json_rows] == [
        tuple(r[f] for f in fields) for r in csv_rows
    ]


def test_import_refuses_two_parcels_sharing_a_name_after_folding(farm, session):
    """A machine record must never be guessed onto one of two same-named parcels."""
    session.add(
        Parcel(
            farm_id=farm.repo.farm_id,
            name="Les Granges",
            alias="P7",
            area_ha=Decimal("1.0"),
            geom=SQUARE,
            props={},
        )
    )
    session.flush()

    with pytest.raises(isoxml.IsoxmlError, match="ambiguous partfield"):
        isoxml.import_taskdata(
            farm.repo, isoxml_demo.demo_taskdata(), now=local(CLOCK), actor="test", tz=TZ
        )


def test_import_refuses_aliases_that_collide_after_case_folding(farm, session):
    """`parcel` is unique on (farm_id, alias) case-sensitively; folding can still collide."""
    session.add(
        Parcel(
            farm_id=farm.repo.farm_id,
            name="Other field",
            alias="p5",
            area_ha=Decimal("1.0"),
            geom=SQUARE,
            props={},
        )
    )
    session.flush()
    xml = MINIMAL.format(
        tasks='<TSK A="TSK1" E="PFD1" G="4">'
        '<TIM A="2024-04-10T07:00:00"/>'
        '<PDV A="0006" B="20000" C="PDT1"/>'
        "</TSK>"
    ).replace('C="Les Granges"', 'C="P5"')

    with pytest.raises(isoxml.IsoxmlError, match="ambiguous partfield"):
        isoxml.import_taskdata(farm.repo, xml.encode(), now=local(CLOCK), actor="test", tz=TZ)


def test_import_refuses_a_name_that_matches_another_parcels_alias(farm, session):
    """Name and alias are separate namespaces; a cross-namespace hit must not be guessed."""
    session.add(
        Parcel(
            farm_id=farm.repo.farm_id,
            name="P5",
            alias="P9",
            area_ha=Decimal("1.0"),
            geom=SQUARE,
            props={},
        )
    )
    session.flush()
    xml = MINIMAL.format(
        tasks='<TSK A="TSK1" E="PFD1" G="4">'
        '<TIM A="2024-04-10T07:00:00"/>'
        '<PDV A="0006" B="20000" C="PDT1"/>'
        "</TSK>"
    ).replace('C="Les Granges"', 'C="P5"')

    with pytest.raises(isoxml.IsoxmlError, match="ambiguous partfield"):
        isoxml.import_taskdata(farm.repo, xml.encode(), now=local(CLOCK), actor="test", tz=TZ)
