"""Operations HTTP: register CSV/JSON, parcel timeline page + HTMX partial, index; access_log."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.apps.operations import register
from farmcore.apps.operations import seed as ops_seed
from farmcore.apps.operations.manifest import MANIFEST
from farmcore.core import clock, logs
from farmcore.core.models.canonical import AccessLog
from farmcore.core.timeline import post_event

# Synthetic times (UTC): 10 Apr 2024 08:00 local voice spray, clock on 12 Apr.
T_VOICE = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
CLOCK_12_APR = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)
CLOCK_20_MAY = datetime(2024, 5, 20, 6, 0, tzinfo=UTC)
VOICE = logs.SourceInput("voice", "tg:1", {"transcript": "synthetic"}, 0.9, logs.RANK_VOICE)
MACHINE = logs.SourceInput("isoxml", "TSK1", {"task": "TSK1"}, 1.0, logs.RANK_MACHINE)

OWNER = [*register.CSV_FIELDS, register.SAVED_WEATHER]  # owner exports: saved weather last


def _voice_spray(session, demo, products, ts=T_VOICE):
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p.id for p in demo.parcels],
        ts_start=ts,
        ts_end=None,
        quantities=[
            logs.QuantityInput("rate", Decimal("2"), "kg/ha", product_id=products[k].id)
            for k in ("copper-contact", "sulphur-wettable")
        ],
        source=VOICE,
        actor="owner",
        occurred_at=ts,
    )
    return log


def _at(session, settings, t):
    clock.set_replay(session, t, settings)
    session.commit()


def _scopes(session) -> set[tuple[str, str]]:
    session.expire_all()
    return {(a.resource, a.scope) for a in session.scalars(select(AccessLog))}


def test_manifest_declares_the_operations_app():
    assert MANIFEST.name == "operations"
    assert MANIFEST.depends == ("core", "fields")
    assert set(MANIFEST.scopes) == {"logs:read", "logs:write", "register:read", "fields:read"}
    assert MANIFEST.nav == ("Diary", "/operations/")
    assert MANIFEST.seed == "farmcore.apps.operations.seed:seed"


def test_register_csv_lists_confirmed_sprays_up_to_the_clock_and_logs_access(
    client, session, settings, demo
):
    products = ops_seed.ensure_products(session)
    _voice_spray(session, demo, products)
    _at(session, settings, datetime(2024, 4, 9, 6, 0, tzinfo=UTC))

    before = client.get("/operations/register.csv")
    assert before.status_code == 200
    assert before.headers["content-type"].startswith("text/csv")
    assert before.headers["content-disposition"].startswith("attachment")
    assert before.text.splitlines() == [",".join(OWNER)]

    _at(session, settings, CLOCK_12_APR)
    response = client.get("/operations/register.csv")

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 4
    assert list(rows[0]) == OWNER
    assert rows[0]["checks"].startswith("OK: Copper 0.4 of 4 kg Cu/ha this year; ")
    assert {(r["parcel_alias"], r["active_substance"]) for r in rows} == {
        ("P1", "copper"),
        ("P1", "sulphur"),
        ("P2", "copper"),
        ("P2", "sulphur"),
    }
    assert {r["authorisation_placeholder"] for r in rows} == {""}  # false is blank
    assert rows[0]["crop_name"] == "Vigne" and rows[0]["crop_eppo"] == "VITVI"
    assert ("log", "register:read") in _scopes(session)


def test_register_json_has_every_field_and_real_e_phy_numbers(client, session, settings, demo):
    products = ops_seed.ensure_products(session)
    _voice_spray(session, demo, products)
    _at(session, settings, CLOCK_12_APR)

    response = client.get("/operations/register.json")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 4
    assert all(list(row) == [*OWNER, register.PARCEL_ALIASES] for row in body)  # JSON: + ids
    assert body[0]["checks"][0] == {
        "key": "copper",
        "status": "ok",
        "label": "Copper 0.4 of 4 kg Cu/ha this year",
        "detail": body[0]["checks"][0]["detail"],
    }
    assert "2018/1981" in body[0]["checks"][0]["detail"]
    assert body[0]["authorisation_no"] == "9800474"
    assert body[0]["authorisation_placeholder"] is False
    assert (body[0]["date"], body[0]["start_time"]) == ("2024-04-10", "08:00")


def test_timeline_partial_is_newest_first_with_a_badge_per_source(
    client, session, settings, demo, tmp_path
):
    p1 = demo.parcels[0]
    products = ops_seed.ensure_products(session)
    _voice_spray(session, demo, products)  # 10 Apr: voice
    ops_seed.seed(session, scene="B", data_dir=tmp_path)  # 9 May: seed
    post_event(
        session,
        farm_id=demo.farm.id,
        res_model="parcel",
        res_id=p1.id,
        kind="advice.evaluated",
        body="advice: spray",
        payload={"decision": "spray"},
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=datetime(2024, 5, 15, 4, 0, tzinfo=UTC),
    )
    # A machine record merging into the 9 May seed spray (ISOXML, deferred 19): 🚜.
    logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p1.id],
        ts_start=datetime(2024, 5, 9, 7, 0, tzinfo=UTC),
        ts_end=None,
        quantities=[],
        source=MACHINE,
        actor="import",
        occurred_at=datetime(2024, 5, 16, 6, 0, tzinfo=UTC),
    )
    # After the clock: hidden.
    post_event(
        session,
        farm_id=demo.farm.id,
        res_model="parcel",
        res_id=p1.id,
        kind="advice.evaluated",
        body="advice: wait",
        payload={},
        actor="job:advisor.evaluate",
        source_app="advisor",
        occurred_at=datetime(2024, 6, 1, 4, 0, tzinfo=UTC),
    )
    _at(session, settings, CLOCK_20_MAY)

    response = client.get(f"/operations/parcels/{p1.id}/timeline")

    assert response.status_code == 200
    assert "<html" not in response.text  # a partial, not a page
    sources = re.findall(r'data-source="([a-z]+)"', response.text)
    assert sources == ["isoxml", "advisor", "seed", "voice"]
    for badge in ("🚜", "🤖", "🌱", "🎤"):
        assert badge in response.text
    assert "advice: wait" not in response.text
    assert ("event", "logs:read") in _scopes(session)


def test_empty_timeline_says_so(client, session, settings, demo):
    _at(session, settings, CLOCK_12_APR)

    response = client.get(f"/operations/parcels/{demo.parcels[0].id}/timeline")

    assert response.status_code == 200
    assert "Nothing recorded" in response.text


def test_parcel_page_loads_its_timeline_and_unknown_parcel_is_404(client, session, settings, demo):
    _at(session, settings, CLOCK_12_APR)
    p1 = demo.parcels[0]

    page = client.get(f"/operations/parcels/{p1.id}")
    missing = client.get("/operations/parcels/999999")

    assert page.status_code == 200
    assert "Test parcel 1" in page.text
    assert f'hx-get="/operations/parcels/{p1.id}/timeline"' in page.text
    assert missing.status_code == 404


def test_index_lists_recent_logs_with_sources_and_export_links(client, session, settings, demo):
    products = ops_seed.ensure_products(session)
    _voice_spray(session, demo, products)
    _voice_spray(session, demo, products, ts=datetime(2024, 4, 30, 6, 0, tzinfo=UTC))  # future
    _at(session, settings, CLOCK_12_APR)

    response = client.get("/operations/")

    assert response.status_code == 200
    assert 'href="/operations/register.csv"' in response.text
    assert 'href="/operations/register.json"' in response.text
    assert "10 Apr 2024 08:00" in response.text
    assert "30 Apr 2024" not in response.text  # later than the clock
    assert "🎤" in response.text
    assert f'href="/operations/parcels/{demo.parcels[0].id}"' in response.text
    assert ("log", "logs:read") in _scopes(session)


def test_index_hides_pending_drafts_because_they_are_not_records(client, session, settings, demo):
    products = ops_seed.ensure_products(session)
    _voice_spray(session, demo, products)
    logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[demo.parcels[0].id],
        ts_start=datetime(2024, 4, 11, 15, 0, tzinfo=UTC),
        ts_end=None,
        quantities=[],
        source=logs.SourceInput("voice", "tg:2", {"transcript": "synthetic"}, 0.9, 30),
        actor="owner",
        occurred_at=datetime(2024, 4, 11, 15, 0, tzinfo=UTC),
        status="pending",
    )
    _at(session, settings, CLOCK_12_APR)

    text = client.get("/operations/").text

    assert "10 Apr 2024 08:00" in text  # the done log
    assert "11 Apr 2024" not in text  # the unconfirmed draft


def test_timeline_of_an_unknown_parcel_is_404(client, session, settings, demo):
    _at(session, settings, CLOCK_12_APR)

    assert client.get("/operations/parcels/999999/timeline").status_code == 404


def test_hourly_advice_does_not_push_sprays_off_the_timeline(client, session, settings, demo):
    from farmcore.apps.operations import routes

    p1 = demo.parcels[0]
    products = ops_seed.ensure_products(session)
    _voice_spray(session, demo, products)  # 10 Apr
    for hour in range(routes.TIMELINE_LIMIT + 10):  # hourly advice after the spray
        post_event(
            session,
            farm_id=demo.farm.id,
            res_model="parcel",
            res_id=p1.id,
            kind="advice.evaluated",
            body=f"advice {hour}",
            payload={},
            actor="job:advisor.evaluate",
            source_app="advisor",
            occurred_at=T_VOICE + timedelta(hours=hour + 1),
        )
    _at(session, settings, CLOCK_20_MAY)

    response = client.get(f"/operations/parcels/{p1.id}/timeline")

    sources = re.findall(r'data-source="([a-z]+)"', response.text)
    assert "voice" in sources
    assert sources.count("advisor") == routes.ADVICE_LIMIT
    assert sources[-1] == "voice"  # still newest first


def test_index_lists_parcels_by_alias_number_and_names_the_bbch_assumption(
    client, session, settings, demo, add_parcel
):
    add_parcel(10)
    _at(session, settings, CLOCK_12_APR)

    text = client.get("/operations/").text

    assert text.index("Test parcel 2") < text.index("Test parcel 10")
    assert "Growth stage" in text and "Bordeaux" in text


def test_diary_links_preserve_explicit_farm_against_conflicting_cookie(
    client, session, settings, demo
):
    from html import unescape
    from urllib.parse import parse_qs, urlsplit

    from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
    from farmcore.core.models.canonical import Farm

    other = Farm(name="Other diary farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    products = ops_seed.ensure_products(session)
    _voice_spray(session, demo, products)
    _at(session, settings, CLOCK_12_APR)
    client.cookies.set(FARM_COOKIE, str(other.id))

    page = client.get("/operations/", params={"farm": demo.farm.id})
    assert page.status_code == 200
    targets = [
        unescape(url)
        for url in re.findall(r'href="([^"]+)"', page.text)
        if url.startswith(("/operations/register", "/operations/parcels/"))
    ]
    assert len(targets) >= 7  # Three register links, two timelines, two recent-record links.
    for target in targets:
        assert parse_qs(urlsplit(target).query)["farm"] == [str(demo.farm.id)]
        response = client.get(target)
        assert response.status_code == 200
        if urlsplit(target).path.endswith(".csv"):
            assert "FR-TEST-1" in response.text
        if "/parcels/" in target:
            assert "Test parcel" in response.text
    assert client.cookies[FARM_COOKIE] == str(other.id)


@pytest.mark.parametrize("attribute", ["action", "hx-post"])
def test_diary_import_form_keeps_explicit_farm_despite_cookie(client, session, settings, attribute):
    from html import unescape

    from farmcore.apps.operations import isoxml_demo
    from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
    from farmcore.core.models.canonical import Farm, Log
    from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm

    first = make_farm(session, settings)
    second = Farm(name="Other import farm")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    _at(session, settings, local(CLOCK))
    client.cookies.set(FARM_COOKIE, str(second.id))
    body = client.get("/operations/", params={"farm": first.repo.farm_id}).text
    [target] = re.findall(rf'{attribute}="([^"]*operations/isoxml[^"]*)"', body)
    response = client.post(
        unescape(target),
        files={"file": ("TASKDATA.XML", isoxml_demo.demo_taskdata(), "application/xml")},
        headers={"HX-Request": "true"} if attribute == "hx-post" else {},
    )
    assert response.status_code == 200
    imported = session.scalars(select(Log).where(Log.type == "spray")).all()
    assert len(imported) == 2
    assert {log.farm_id for log in imported} == {first.repo.farm_id}
    assert client.cookies[FARM_COOKIE] == str(second.id)
