"""Home variants retain replay facts and independently polling fragment identities."""

import html
import re

import pytest
from sqlalchemy import select

from farmcore.core.models.canonical import Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.test_card_plain import by_id

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


@pytest.mark.data
@pytest.mark.parametrize("explicit_farm", [False, True])
def test_attention_is_empty_before_risk_but_keeps_polling(
    client, session, seeded, step, explicit_farm
):
    seeded()
    step("2024-04-08 06:00")
    farm_id = session.scalar(select(Parcel.farm_id).where(Parcel.alias == "P1"))
    params = {"view": "attention"}
    if explicit_farm:
        params["farm"] = str(farm_id)
    body = client.get("/advisor/today", params=params).text
    assert "data-home-empty" in body
    assert 'id="today"' in body and "data-poll-feed" in body
    poll = by_id(body, "today")["attrs"]
    assert poll["hx-get"] == f"/advisor/today?view=attention&farm={farm_id}"
    assert poll["hx-trigger"] == "every 12s [!document.hidden]"
    assert poll["hx-target"] == "this" and poll["hx-swap"] == "outerHTML"
    assert 'id="today-hero"' not in body
    calm = html.unescape(client.get("/advisor/today?view=calm").text)
    assert 'id="today-calm"' in calm
    assert "data-home-empty" not in calm
    assert "Nothing to spray today" in calm


@pytest.mark.data
def test_attention_names_only_the_live_loop_risk_and_keeps_farm(client, session, seeded, step):
    from sqlalchemy import select

    from farmcore.apps.advisor.demo import apply_live_loop
    from farmcore.core.models.canonical import Farm

    seeded()
    farm = session.scalar(select(Farm).where(Farm.name == "Domaine des Trois Coteaux"))
    if farm is None:
        farm = session.scalar(select(Farm).order_by(Farm.id))
    apply_live_loop(session, farm, scenes.local("2024-04-10 06:00"))
    session.commit()
    step("2024-04-10 06:00")
    body = html.unescape(
        client.get("/advisor/today", params={"view": "attention", "farm": farm.id}).text
    )
    assert "data-home-empty" not in body
    for name in ("Les Granges", "Peyrot", "La Fontaine"):
        assert name in body
    assert "7 parcels protected" in body
    assert f"view=attention&farm={farm.id}" in body
    assert "data-home-empty" in client.get("/advisor/today?view=calm").text


@pytest.mark.data
def test_unknown_advice_is_not_a_calm_all_clear(client, seeded):
    seeded()
    body = client.get("/advisor/today?view=calm").text
    assert "Nothing to spray today" not in body
    assert "No advice" in body


@pytest.mark.data
def test_home_map_shell_loads_at_phone_width_with_farm_link(client, session, seeded):
    from sqlalchemy import select

    from farmcore.core.models.canonical import Farm

    seeded()
    farm_id = session.scalar(select(Farm.id).order_by(Farm.id))
    body = html.unescape(client.get(f"/fields/today?desktop=1&farm={farm_id}").text)
    assert "matchMedia" not in body
    assert 'class="hidden lg:block"' not in body
    assert 'hx-trigger="load"' in body
    assert f'href="/fields/?farm={farm_id}"' in body
    assert "Open map" in body


@pytest.mark.data
def test_compact_observation_shows_now_not_replay_future(client, seeded, step):
    seeded()
    step("2024-04-08 12:00")
    body = html.unescape(client.get("/stations/today?compact=1").text)
    assert "21 °C" in body and "Dry" in body
    assert "Observed" in body
    assert "Rain from" not in body and "next 24 h" not in body
    assert "data-rain-bar" not in body


def test_home_heading_and_collapsed_source_slot(client):
    assert "/data-sources/today" not in client.get("/today/").text  # Expert off (N4)
    client.cookies.set("expert", "1")
    body = client.get("/today/").text
    headings = re.findall(r"<h1\b([^>]*)>(.*?)</h1>", body, re.S)
    assert len(headings) == 1
    attrs, label = headings[0]
    assert label.strip() == "Home"
    assert 'id="home-heading"' in attrs
    assert "data-return-heading" in attrs
    assert 'tabindex="-1"' in attrs
    classes = re.search(r'class="([^"]+)"', attrs)[1].split()
    assert {"sr-only", "focus:not-sr-only", "focus:text-2xl", "focus:font-semibold"} <= set(classes)
    assert {"focus:outline", "focus:outline-2", "focus:outline-brand-700"} <= set(classes)
    source = body.index('hx-get="/data-sources/today"')
    assert "<details" in body[:source]
    assert "<summary" in body[:source] and "Data sources" in body[:source]
    assert "open" not in body[:source].rsplit("<details", 1)[1].split(">")[0]


@pytest.mark.spanish_data
@pytest.mark.data
def test_spanish_home_withholds_advice_and_keeps_archive_separate(client, session, seeded):
    from sqlalchemy import select

    from farmcore.apps.demo_es.archive import NOTICE
    from farmcore.core.models.canonical import Farm

    seeded(scene="B")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    assert es is not None
    attention = client.get("/advisor/today", params={"view": "attention", "farm": es.id}).text
    calm = client.get("/advisor/today", params={"view": "calm", "farm": es.id}).text
    assert "data-home-empty" in attention and NOTICE not in attention
    assert NOTICE in calm and "Crop protection unavailable" in calm
    assert "Nothing to spray today" not in calm and "Les Granges" not in calm
    weather = client.get("/stations/today", params={"compact": "1", "farm": es.id}).text
    assert "Station archive" in weather and "Météo-France" not in weather
    assert "Weather now" not in weather


@pytest.mark.data
def test_tour_attention_has_no_logging_link(settings, session_factory, seeded, step):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    seeded()
    step("2024-04-10 06:00")
    configured = settings.model_copy(update={"telegram_bot_username": "@synthetic_home_bot"})
    with TestClient(create_app(configured, session_factory=session_factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert "https://t.me/synthetic_home_bot" in client.get("/advisor/today?view=attention").text
        client.cookies.set("farmcore_tour", "1")
        for path in ("/today/", "/advisor/today?view=attention", "/advisor/today?view=calm"):
            body = client.get(path).text
            assert "https://t.me/" not in body and "Log a spray" not in body


def test_home_does_not_show_widgets_from_disabled_apps(settings, session_factory):
    from fastapi.testclient import TestClient

    from farmcore.core.models.kernel import AppModule
    from farmcore.main import create_app

    app = create_app(settings, session_factory=session_factory)
    with session_factory() as session:
        for name in ("advisor", "stations", "forecast"):
            session.get(AppModule, name).state = "uninstalled"
        session.commit()
    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        body = client.get("/today/").text
    for path in ("/advisor/today", "/stations/today", "/forecast/"):
        assert f'hx-get="{path}' not in body
    assert 'hx-get="/activity/today"' in body
    assert 'hx-get="/fields/today' in body  # required app cannot be switched off


@pytest.mark.data
def test_compact_now_is_one_linked_row(client, seeded, step):
    seeded()
    step("2024-04-08 12:00")
    body = client.get("/stations/today?compact=1").text
    assert body.count("<a ") == 1 and "data-now-row" in body
    assert "mt-2 flex flex-wrap items-center gap-x-4" not in body


@pytest.mark.data
def test_home_forecast_footnotes_sit_in_a_details_with_a_source_line(client, seeded, step):
    seeded()
    step("2024-04-08 12:00")
    body = client.get("/forecast/home-weather").text
    assert body.count("<details") == 1
    summary = body.split("<summary", 1)[1].split("</summary>", 1)[0]
    assert "ECMWF" in summary and "CC BY 4.0" in summary and "run " in summary
    assert "Gusts are information only" in body.split("</summary>", 1)[1]
    assert "Gusts are information only" not in body.split("<details", 1)[0] + summary
