"""Parcel season history (N7): sprays, detected infection-risk episodes, protection periods and
advice changes, read from stored rows only, never after the replay clock's now."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from farmcore.core import seed as core_seed
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm, Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
pytestmark = pytest.mark.data


@pytest.fixture
def live_loop(session, settings, seeded, step):
    from farmcore.apps.advisor.demo import apply_live_loop

    seeded("B")
    step("2024-04-08 06:00")
    farm = core_seed.ensure_default_farm(session, settings)
    apply_live_loop(session, farm, local("2024-04-10 06:00"))
    session.commit()
    step("2024-04-10 06:00")
    return farm


def _parcel(session, farm, alias):
    return session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == alias))


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _section(html: str, name: str) -> str:
    return _text(html.split(f'data-history="{name}"', 1)[1].split("</section>", 1)[0])


def test_protected_parcel_lists_its_spray_cover_episode_and_no_future_spray(
    client, session, live_loop
):
    p1 = _parcel(session, live_loop, "P1")

    html = client.get(f"/advisor/parcels/{p1.id}/history").text

    sprays = _section(html, "sprays")
    assert "9 Apr" in sprays and "BOUILLIE BORDELAISE" in sprays and "2 kg/ha" in sprays
    assert "9 May" not in sprays  # the scene B spray is after now
    cover = _section(html, "protection")
    assert "9 Apr" in cover and "17 Apr" in cover
    risk = _section(html, "episodes")
    assert "10.3 mm" in risk and "covered" in risk.lower()
    assert "History season 2024" in _text(html)


def test_uncovered_parcel_shows_the_uncovered_episode_and_the_advice_change(
    client, session, live_loop
):
    p5 = _parcel(session, live_loop, "P5")

    html = client.get(f"/advisor/parcels/{p5.id}/history").text

    risk = _section(html, "episodes")
    assert "10.3 mm" in risk and "uncovered" in risk.lower()
    assert "No sprays" in _section(html, "sprays")
    changes = _section(html, "changes")
    assert "Spray" in changes and "10 Apr" in changes


def test_history_shows_nothing_after_the_replay_clock(client, session, settings, live_loop):
    p1 = _parcel(session, live_loop, "P1")
    set_replay(session, local("2024-04-09 06:00"), settings)
    session.commit()

    html = client.get(f"/advisor/parcels/{p1.id}/history").text

    assert "No sprays" in _section(html, "sprays")  # the 9 Apr 08:00 spray is later
    assert "17 Apr" not in _text(html)
    assert "10 Apr" not in _text(html)


def test_season_selector_is_an_ordinary_reading_control(client, session, live_loop):
    from datetime import datetime

    from farmcore.core.models.canonical import Log

    p1 = _parcel(session, live_loop, "P1")
    session.add(
        Log(
            farm_id=live_loop.id,
            type="spray",
            status="done",
            name="Test spray last season",
            ts_start=datetime.fromisoformat("2023-06-01T08:00:00+02:00"),
            parcels=[p1],
        )
    )
    session.commit()
    url = f"/advisor/parcels/{p1.id}/history"

    plain = client.get(url, params={"season": 2023}).text
    assert "History season 2023" in _text(plain)
    assert 'name="season"' in plain

    client.cookies.set("expert", "1")
    expert = client.get(url, params={"season": 2023}).text
    assert 'name="season"' in expert
    assert "History season 2023" in _text(expert)
    assert "9 Apr" not in _section(expert, "sprays")
    assert "1 Jun" in _section(expert, "sprays")
    assert client.get(url, params={"season": 2099}).status_code == 200
    assert "History season 2024" in _text(
        client.get(url, params={"season": 2099}).text
    )  # no future


@pytest.mark.spanish_data
def test_history_is_farm_scoped_and_the_card_loads_it(client, session, live_loop):
    p1 = _parcel(session, live_loop, "P1")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))

    assert client.get(f"/advisor/parcels/{p1.id}/history?farm={es.id}").status_code == 404
    card = client.get(f"/advisor/parcels/{p1.id}").text
    assert 'id="history-heading"' in card and 'data-history-season="2024"' in card
