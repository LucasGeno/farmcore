"""The advisor map panel sections over HTTP, plus the typed nearest-deadline rule.

- `GET /advisor/map/farm`: parcels per decision, "N of M parcels evaluated" whenever N < M,
  parcels awaiting advice, the nearest finite spray deadline (typed `ParcelRow.window_end`,
  never `Group.when`).
- `GET /advisor/map/parcels/{id}`: decision chip, cover, rain since spray (never 0 for unknown),
  weather gaps, card link; "Awaiting advice" apart from an evaluated "No spray needed".

Synthetic advice rows on invented parcels (the throwaway test database), and Scene B for the
real spray / protected / replay path (`data`).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from farmcore.apps.advisor.manifest import MANIFEST
from farmcore.apps.advisor.map_sections import nearest_spray_deadline
from farmcore.core import clock, jobs
from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import Advice, Farm
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor import scenes
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.advisor.scenes import confirm_spray, local
from tests.apps.advisor.test_presenter import row, spray
from tests.apps.operations.conftest import make_parcel

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

NOW = datetime(2024, 4, 10, 4, tzinfo=UTC)  # 06:00 Paris
WINDOW = {"rule": "spray_window", "threshold": {"window_horizon_h": 72}, "inputs": {}}


def text(resp) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", resp.text)).split())


def hrefs(resp) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]+)"', resp.text)]


def advise(session, farm, parcel, decision, **kw) -> Advice:
    a = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=kw.pop("at", NOW),
        decision=decision,
        reasons=kw.pop("reasons", [WINDOW]),
        next_window_start=kw.pop("start", None),
        next_window_end=kw.pop("end", None),
        protected_until=kw.pop("until", None),
        products=kw.pop("products", []),
        props={"headline": "", "flags": [], **kw.pop("props", {})},
    )
    assert not kw
    session.add(a)
    session.flush()
    return a


def at(session, settings, t):
    clock.set_replay(session, t, settings)
    session.commit()


@pytest.fixture
def farm(session, settings):
    f = ensure_default_farm(session, settings)
    complete_station(session, f.id, NOW + timedelta(hours=2))
    parcels = [make_parcel(session, f, n, area="1", props={}) for n in (1, 2, 3)]
    at(session, settings, NOW)
    return f, parcels


# --- the typed deadline ---------------------------------------------------------------------


def test_deadline_is_the_earliest_finite_spray_window_end() -> None:
    now = local("2024-04-10 06:00")
    rows = [
        spray(1, end=local("2024-04-11 11:00")),
        spray(2, end=local("2024-04-10 11:00")),
        spray(3, end=local("2024-04-10 11:00"), products=("sulphur",)),
        spray(4, end=local("2024-04-10 08:00"), open_ended=True),  # forecast edge: no deadline
        row(5, "wait", start=local("2024-04-10 07:00"), end=local("2024-04-10 09:00")),
    ]
    assert nearest_spray_deadline(rows, now) == (local("2024-04-10 11:00"), ("P2", "P3"))


def test_no_finite_deadline_is_no_deadline() -> None:
    now = local("2024-04-10 06:00")
    rows = [
        spray(1, end=local("2024-04-13 06:00"), open_ended=True),
        row(2, "wait", start=local("2024-04-11 06:00")),
        spray(3, end=local("2024-04-10 05:00")),  # already past
    ]
    assert nearest_spray_deadline(rows, now) is None


# --- HTTP ------------------------------------------------------------------------------------


def test_manifest_declares_the_sections() -> None:
    assert MANIFEST.farm_sections == ("/advisor/map/farm",)
    assert MANIFEST.parcel_sections == ("/advisor/map/parcels/{id}",)


def test_empty_farm(client, session, settings) -> None:
    empty = ensure_default_farm(session, settings)
    complete_station(session, empty.id, NOW)
    at(session, settings, NOW)
    resp = client.get("/advisor/map/farm")
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith("<section") and "<html" not in resp.text
    assert "No parcels on this farm yet" in text(resp)


def test_no_advice_is_zero_of_n_evaluated(client, farm) -> None:
    f, parcels = farm
    body = text(client.get("/advisor/map/farm"))
    assert "0 of 3 parcels evaluated" in body
    assert "Awaiting advice 3 parcels" in body
    assert "Nearest spray deadline" not in body
    one = text(client.get(f"/advisor/map/parcels/{parcels[0].id}"))
    assert "Awaiting advice" in one and "No spray needed" not in one
    assert "Not evaluated yet." in one


def test_unevaluated_is_never_calm(client, session, farm) -> None:
    f, (p1, p2, p3) = farm
    advise(session, f, p1, "none")
    advise(session, f, p2, "none")
    body = text(client.get("/advisor/map/farm"))
    assert "2 of 3 parcels evaluated" in body
    assert "No spray needed 2 parcels" in body
    assert "Awaiting advice 1 parcel" in body
    assert "Nothing to spray" not in body
    calm = text(client.get(f"/advisor/map/parcels/{p1.id}"))
    assert "No spray needed" in calm and "Awaiting advice" not in calm
    assert "Awaiting advice" in text(client.get(f"/advisor/map/parcels/{p3.id}"))


def test_finite_deadline_open_ended_and_wait(client, session, farm) -> None:
    f, (p1, p2, p3) = farm
    copper = [{"name": "copper"}]
    advise(session, f, p1, "spray", start=NOW, end=NOW + timedelta(hours=72), products=copper)
    advise(session, f, p2, "wait", start=NOW + timedelta(hours=30), end=NOW + timedelta(hours=34))
    advise(session, f, p3, "none")
    body = text(client.get("/advisor/map/farm"))
    # Open-ended window and a wait: no deadline line.
    assert "Spray 1 parcel" in body and "Wait 1 parcel" in body
    assert "Nearest spray deadline" not in body
    assert "spray window open now" in text(client.get(f"/advisor/map/parcels/{p1.id}"))

    advise(
        session,
        f,
        p3,
        "spray",  # the newer row at the same hour wins
        start=NOW,
        end=NOW + timedelta(hours=5),
        products=[{"name": "sulphur"}],
    )
    body = text(client.get("/advisor/map/farm"))
    assert "Nearest spray deadline: Wed 10 Apr · 11:00 (P3)" in body
    assert "Spray 2 parcels" in body
    # Each parcel names its own products, never a union of the farm's.
    one = text(client.get(f"/advisor/map/parcels/{p1.id}"))
    three = text(client.get(f"/advisor/map/parcels/{p3.id}"))
    assert "Products copper" in one and "sulphur" not in one
    assert "Products sulphur" in three and "copper" not in three
    assert "until Wed 10 Apr · 11:00" in three


def test_protected_cover_and_rain(client, session, farm) -> None:
    f, (p1, p2, p3) = farm
    until = NOW + timedelta(days=8)
    advise(session, f, p1, "protected", until=until, props={"mm_since_spray": 3.2})
    advise(
        session,
        f,
        p2,
        "spray",
        start=NOW,
        end=NOW + timedelta(hours=72),
        props={"mm_since_spray": 44.0, "flags": ["washed_off"]},
    )
    one = text(client.get(f"/advisor/map/parcels/{p1.id}"))
    assert "Protected" in one and "Covered until Thu 18 Apr · 06:00" in one
    assert "Rain since spray 3.2 mm" in one
    two = text(client.get(f"/advisor/map/parcels/{p2.id}"))
    assert "Washed off by rain" in two and "Rain since spray 44.0 mm" in two


def test_partial_weather_is_a_stated_gap_never_zero(client, session, farm) -> None:
    f, (p1, p2, p3) = farm
    gap = {"rule": "protection_downy", "inputs": {"rain_data_gap": True, "mm_since_spray": 0.0}}
    advise(
        session,
        f,
        p1,
        "protected",
        until=NOW + timedelta(days=2),
        reasons=[WINDOW, gap],
        props={"mm_since_spray": 0.0, "flags": ["data_gap"]},
    )
    advise(session, f, p2, "none", props={"mm_since_spray": None})
    some = {"rule": "protection_downy", "inputs": {"rain_data_gap": True}}
    advise(session, f, p3, "none", reasons=[some], props={"mm_since_spray": 2.5})
    one = text(client.get(f"/advisor/map/parcels/{p1.id}"))
    assert "Rain since spray no rain reading (station hours missing)" in one
    assert "0.0 mm" not in one
    assert "Weather gap: some station hours are missing" in one
    two = text(client.get(f"/advisor/map/parcels/{p2.id}"))
    assert "Rain since spray no rain reading" in two and "0.0 mm" not in two
    three = text(client.get(f"/advisor/map/parcels/{p3.id}"))
    assert "at least 2.5 mm (station hours missing)" in three


def test_backward_replay_hides_later_advice(client, session, settings, farm) -> None:
    f, (p1, p2, p3) = farm
    advise(session, f, p1, "none", at=NOW + timedelta(hours=1))
    assert "0 of 3 parcels evaluated" in text(client.get("/advisor/map/farm"))
    assert "Awaiting advice" in text(client.get(f"/advisor/map/parcels/{p1.id}"))
    at(session, settings, NOW + timedelta(hours=2))
    assert "1 of 3 parcels evaluated" in text(client.get("/advisor/map/farm"))


@pytest.fixture
def farms(client, session, settings, farm):
    first, parcels = farm
    second = Farm(name="Map QA B")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    complete_station(session, second.id, datetime(2024, 5, 10, 4, tzinfo=UTC))
    p202 = make_parcel(session, second, 202, area="2", props={})
    advise(session, second, p202, "protected", until=NOW + timedelta(days=3))
    session.commit()
    client.cookies.set(FARM_COOKIE, str(first.id), domain="testserver.local")
    return first, second, parcels, p202


def test_alien_parcel_is_404(client, farms) -> None:
    first, second, parcels, p202 = farms
    assert client.get(f"/advisor/map/parcels/{p202.id}").status_code == 404
    alien = client.get(f"/advisor/map/parcels/{parcels[0].id}", params={"farm": second.id})
    assert alien.status_code == 404


def test_query_farm_wins_over_the_cookie_and_qualifies_links(client, farms) -> None:
    first, second, parcels, p202 = farms
    farm_b = client.get("/advisor/map/farm", params={"farm": second.id})
    body = text(farm_b)
    assert "Protected 1 parcel" in body and "evaluated" not in body
    assert hrefs(farm_b) == [f"/advisor/?farm={second.id}"]
    parcel = client.get(f"/advisor/map/parcels/{p202.id}", params={"farm": second.id})
    assert parcel.status_code == 200 and "Protected" in text(parcel)
    _assert_contextual_card_link(parcel, p202.id, second.id)
    # The cookie farm A alone: its own three parcels, none evaluated.
    assert "0 of 3 parcels evaluated" in text(client.get("/advisor/map/farm"))


def _assert_contextual_card_link(response, parcel_id, farm_id):
    link, about = hrefs(response)
    assert about == f"/advisor/about?farm={farm_id}"
    target = urlsplit(link)
    assert target.path == f"/advisor/parcels/{parcel_id}"
    query = parse_qs(target.query)
    assert query.keys() == {"farm", "return_to"}
    assert query["farm"] == [str(farm_id)]
    [origin] = query["return_to"]
    assert urlsplit(origin).path == "/fields/"
    assert parse_qs(urlsplit(origin).query) == {"parcel": [str(parcel_id)], "farm": [str(farm_id)]}


# --- Scene B (real engine) --------------------------------------------------------------------


@pytest.mark.data
def test_scene_b_spray_then_protected_and_backward_replay(
    client, session, session_factory, settings, manifests, seeded, step
) -> None:
    seeded()
    body = text(client.get("/advisor/map/farm"))
    assert "0 of 10 parcels evaluated" in body

    step("2024-04-10 06:00")
    for alias in ("P5", "P6"):
        confirm_spray(session, settings, alias, local("2024-04-10 06:00"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok
    body = text(client.get("/advisor/map/farm"))
    assert "Spray 8 parcels" in body and "Protected 2 parcels" in body
    assert "evaluated" not in body  # full coverage
    # The window runs to the forecast edge: no deadline line, never a made-up one.
    assert "Nearest spray deadline" not in body

    from sqlalchemy import select

    from farmcore.core.models.canonical import Parcel

    p5 = session.scalar(select(Parcel).where(Parcel.alias == "P5"))
    one = text(client.get(f"/advisor/map/parcels/{p5.id}"))
    assert "Protected" in one and "Covered until Thu 18 Apr" in one
    ops = text(client.get(f"/operations/map/parcels/{p5.id}"))
    assert "Last spray Wed 10 Apr 2024" in ops and "0.4 kg Cu/ha" in ops

    at(session, settings, local("2024-04-08 06:00"))
    assert "0 of 10 parcels evaluated" in text(client.get("/advisor/map/farm"))
    assert "No confirmed spray yet" in text(client.get(f"/operations/map/parcels/{p5.id}"))


# --- a stored window the clock has passed (display only) ---------------------------------------


def _day(t: datetime) -> str:
    from zoneinfo import ZoneInfo

    return t.astimezone(ZoneInfo("Europe/Paris")).strftime("%a %-d %b · %H:%M")


@pytest.mark.parametrize("offset_h", [-1, 0, 1])
def test_a_finite_window_past_the_clock_reads_ended(client, session, farm, offset_h) -> None:
    f, (p1, _, _) = farm
    end = NOW + timedelta(hours=offset_h)
    evaluated = NOW - timedelta(hours=48)  # end < evaluated + 72 h: a finite deadline
    advise(session, f, p1, "spray", at=evaluated, start=evaluated, end=end)
    body = text(client.get(f"/advisor/map/parcels/{p1.id}"))
    if offset_h <= 0:
        assert f"Window ended {_day(end)}" in body
        assert "until" not in body and "open now" not in body and "from" not in body
    else:
        assert f"Window until {_day(end)}" in body and "ended" not in body
    farm_body = text(client.get("/advisor/map/farm"))
    assert ("Nearest spray deadline" in farm_body) is (offset_h > 0)


@pytest.mark.parametrize("offset_h", [-1, 0, 1])
def test_an_open_ended_window_past_the_clock_is_no_longer_known(
    client, session, farm, offset_h
) -> None:
    f, (p1, _, _) = farm
    end = NOW + timedelta(hours=offset_h)
    evaluated = end - timedelta(hours=72)  # the window runs to the stored 72 h forecast edge
    advise(session, f, p1, "spray", at=evaluated, start=evaluated, end=end)
    body = text(client.get(f"/advisor/map/parcels/{p1.id}"))
    if offset_h <= 0:
        assert f"Window no longer known — advice is from {_day(evaluated)}" in body
        assert "open now" not in body and "until" not in body
    else:
        assert "Window spray window open now" in body and "no longer known" not in body
    assert "Nearest spray deadline" not in text(client.get("/advisor/map/farm"))


def test_expired_stored_forecast_window_never_claims_open_now(client, session, settings) -> None:
    """The independent review's fixture: farm B selected by query over cookie farm A (both have
    a P1); advice of 6 May with a window to its 72 h forecast edge (9 May); clock 10 May."""
    first = ensure_default_farm(session, settings)
    second = Farm(name="Map QA B")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    complete_station(session, second.id, datetime(2024, 5, 10, 4, tzinfo=UTC))
    make_parcel(session, first, 1, area="1", props={})
    from farmcore.core import ext_ids
    from farmcore.core.models.canonical import Parcel

    p1, _ = ext_ids.upsert(
        session,
        source="test-parcels",
        key="FR-TEST-B-1",
        model=Parcel,
        values={
            "farm_id": second.id,
            "name": "B parcel 1",
            "alias": "P1",
            "iacs_id": "FR-TEST-B-1",
            "area_ha": 1,
            "geom": "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,"
            "-0.16 44.90,-0.16 44.89)))",
            "props": {},
        },
    )
    evaluated = datetime(2024, 5, 6, 4, tzinfo=UTC)
    advise(
        session,
        second,
        p1,
        "spray",
        at=evaluated,
        start=evaluated,
        end=datetime(2024, 5, 9, 4, tzinfo=UTC),
    )
    at(session, settings, datetime(2024, 5, 10, 4, tzinfo=UTC))
    client.cookies.set(FARM_COOKIE, str(first.id), domain="testserver.local")
    resp = client.get(f"/advisor/map/parcels/{p1.id}", params={"farm": second.id})
    assert resp.status_code == 200
    body = text(resp)
    assert "open now" not in body
    assert "Window no longer known — advice is from Mon 6 May · 06:00" in body
    _assert_contextual_card_link(resp, p1.id, second.id)
