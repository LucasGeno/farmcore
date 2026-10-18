"""A machine-file import keeps the file's device designators (`DVC B`) on its log
source (`raw_payload["file_devices"]`), and the Activity column "Machine file device(s)" shows
them. A task names no device, so the column lists the file's devices, never "the sprayer that
sprayed"; records without them (voice, older imports, a file without DVC) show "—"."""

from __future__ import annotations

import html
import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.apps.operations import isoxml_demo
from farmcore.core import clock, logs
from farmcore.core.models.canonical import LogSource
from tests.apps.activity.test_columns import _cells, _rows
from tests.apps.operations import isoxml_farm
from tests.apps.operations.isoxml_farm import local
from tests.apps.operations.test_isoxml_routes import _ready, _upload

farm = isoxml_farm.farm  # fixture


ISOXML = LogSource.source_type == "isoxml"


def _device_cells(client) -> list[tuple[str, str]]:
    client.get("/activity/columns", params=[("show", "source"), ("show", "device")])
    rows = _rows(client.get("/activity/").text)
    return [(_cells(r)["source"], _cells(r)["device"]) for r in rows]


def test_import_stores_the_files_devices_and_the_column_shows_them(
    client, session, settings, farm
) -> None:
    _ready(session, settings, farm, voice=False)
    assert _upload(client).status_code == 200
    payloads = list(session.scalars(select(LogSource.raw_payload).where(ISOXML)))
    assert payloads and all(p["file_devices"] == [isoxml_demo.DEVICE] for p in payloads)
    rows = _device_cells(client)
    assert rows and all(device == isoxml_demo.DEVICE for _source, device in rows)


def test_a_voice_record_has_no_device(client, session, settings, farm) -> None:
    _ready(session, settings, farm, voice=True)
    assert _device_cells(client) == [("Voice note", "—")]


def test_a_file_without_dvc_stores_none_and_shows_a_dash(client, session, settings, farm) -> None:
    _ready(session, settings, farm, voice=False)
    xml = isoxml_demo.demo_taskdata().decode()
    start = xml.index("<DVC")
    end = xml.index(">", xml.index("</DVC>") if "</DVC>" in xml else start) + 1
    assert _upload(client, (xml[:start] + xml[end:]).encode()).status_code == 200
    payloads = list(session.scalars(select(LogSource.raw_payload).where(ISOXML)))
    assert payloads and all(p["file_devices"] == [] for p in payloads)
    assert all(device == "—" for _source, device in _device_cells(client))


@pytest.mark.parametrize("paged", [False, True])
def test_current_machine_summary_is_only_on_the_newest_log_row(
    client, session, settings, farm, monkeypatch, paged
):
    from farmcore.apps.activity import routes

    at = local("2024-04-10 06:00")
    copper = farm.products["copper"]
    pending = logs.create_pending(
        session,
        farm_id=farm.repo.farm_id,
        type="spray",
        parcel_ids=[p.id for p in farm.parcels.values()],
        ts_start=at,
        ts_end=None,
        quantities=[logs.QuantityInput("rate", Decimal("2"), "kg/ha", copper.id, copper.name)],
        source=logs.SourceInput("voice", "rehearsal:voice", {}, 0.9, logs.RANK_VOICE),
        actor="chat:123456789",
        occurred_at=at,
    )
    logs.confirm(session, pending.id, "chat:123456789", at)
    clock.set_replay(session, at, settings)
    session.commit()
    client.get(
        "/activity/columns",
        params=[("show", key) for key in ("time", "product", "source", "device")],
    )
    (voice,) = _rows(client.get("/activity/").text)
    assert _cells(voice)["product"] == "copper 2 kg/ha"
    assert _cells(voice)["device"] == "—"

    clock.set_replay(session, local("2024-04-10 10:00"), settings)
    session.commit()
    assert _upload(client).status_code == 200
    if paged:
        monkeypatch.setattr(routes, "PAGE", 1)
    page = client.get("/activity/").text
    newest = _cells(_rows(page)[0])
    assert "10:00" in newest["time"]
    assert newest["product"] == "copper 1.94 kg/ha + sulphur 2.06 kg/ha"
    assert newest["device"] == isoxml_demo.DEVICE
    if paged:
        link = re.search(r'href="([^"]+)"[^>]*>Show older', page)
        assert link
        page = client.get(html.unescape(link[1])).text
    (confirmed,) = [r for r in _rows(page) if 'data-kind="log.confirmed"' in r]
    old = _cells(confirmed)
    assert "06:00" in old["time"]
    assert old["source"] == "Voice note"
    assert old["product"] == "—"
    assert old["device"] == "—"
