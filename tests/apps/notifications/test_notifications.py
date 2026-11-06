"""The bell reads stored farm facts through its public GET fragment; no transport/model calls."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice
from farmcore.core.seed import ensure_default_farm
from tests.apps.advisor import scenes
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.advisor.scenes import local
from tests.apps.share.conftest import make_parcel

manifests, seeded = scenes.manifests, scenes.seeded
NOW = local("2024-04-10 06:00")


def test_bell_shows_only_current_risky_parcels_with_safe_wait_wording(client, session, settings):
    farm = ensure_default_farm(session, settings)
    complete_station(session, farm.id, NOW)
    for n, decision in enumerate(("spray", "wait", "protected", "none"), 1):
        parcel = make_parcel(session, farm, n)
        session.add(
            Advice(
                farm_id=farm.id,
                parcel_id=parcel.id,
                evaluated_at=NOW,
                decision=decision,
                props={"headline": f"Stored {decision} reason"},
            )
        )
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=NOW + timedelta(days=1),
            decision="spray",
        )
    )
    set_replay(session, NOW, settings)
    session.commit()
    response = client.get(f"/notifications?farm={farm.id}")
    assert response.status_code == 200
    assert response.text.count('data-severity="red"') == 2
    assert response.text.count('data-severity-icon="red"') == 2  # never colour alone
    assert "At risk — wait" in response.text
    assert "Stored spray reason" in response.text
    ids = session.scalars(select(Advice.parcel_id).where(Advice.decision == "wait")).all()
    assert f"/advisor/parcels/{ids[0]}?farm={farm.id}" in response.text
    assert "t.me/" not in response.text


def test_bell_names_limited_parcels_while_the_farm_is_available(client, session, settings):
    farm = ensure_default_farm(session, settings)
    complete_station(session, farm.id, NOW)
    for n, (decision, status) in enumerate((("none", "limited"), ("protected", "evaluable")), 1):
        parcel = make_parcel(session, farm, n)
        session.add(
            Advice(
                farm_id=farm.id,
                parcel_id=parcel.id,
                evaluated_at=NOW,
                decision=decision,
                props={"assessment_status": status, "headline": f"Stored {status}"},
            )
        )
    set_replay(session, NOW, settings)
    session.commit()
    text = client.get(f"/notifications?farm={farm.id}").text
    assert "Assessment limited" in text
    assert "Infection risk unknown for P1 " in text
    assert "P2 " not in text.split("Infection risk unknown for", 1)[1].split(":", 1)[0]


def test_station_health_is_replay_bounded_and_archive_is_information(client, session, settings):
    from farmcore.core.models.canonical import Asset, Observation

    farm = ensure_default_farm(session, settings)
    station = Asset(farm_id=farm.id, kind="station", name="Synthetic station", props={})
    session.add(station)
    session.flush()
    session.add_all(
        [
            Observation(asset_id=station.id, ts=NOW - timedelta(hours=3), variable="t_c", value=15),
            Observation(asset_id=station.id, ts=NOW + timedelta(hours=1), variable="t_c", value=16),
        ]
    )
    set_replay(session, NOW, settings)
    session.commit()
    response = client.get("/notifications")
    assert "Station reading is stale" in response.text
    assert f"/stations/{station.id}?farm={farm.id}" in response.text
    station.props = {"error": "file missing"}
    session.commit()
    assert "Import failed: file missing" in client.get("/notifications").text
    station.props = {"archive_only": True, "archive_page": f"/demo_es/archive/{station.id}"}
    farm.props = {"advisor_unavailable_reason": "Archive only; timestamp basis unverified"}
    session.commit()
    response = client.get("/notifications")
    assert "Archive only; timestamp basis unverified" in response.text
    assert "Station reading is stale" not in response.text
    assert "No forecast" not in response.text
    assert 'data-severity="red"' not in response.text
    assert 'data-severity="amber"' not in response.text


def test_forecast_avoid_reuses_ranking_and_respects_busy_and_available_time(
    client, session, settings
):
    from farmcore.apps.advisor.service import config_for
    from farmcore.apps.forecast import sources, store
    from farmcore.apps.forecast.ranking import rank_days
    from tests.apps.forecast.conftest import FIXTURE

    farm = ensure_default_farm(session, settings)
    now = local("2024-04-09 06:00")
    set_replay(session, now, settings)
    runs = sources.FileSource(FIXTURE).runs()
    store.import_runs(session, farm.id, runs)
    session.commit()
    selected = max((r for r in runs if r.available <= now), key=lambda r: r.available)
    from farmcore.apps.advisor.engine import HourlyObs

    values = {}
    for ts, variable, value in selected.values:
        if variable != "fx_kmh":
            values.setdefault(ts, {})[variable] = value
    graded = rank_days(
        [HourlyObs(ts=ts, **v) for ts, v in sorted(values.items())],
        now,
        settings.display_tz,
        config_for(farm, now, settings.display_tz),
    )
    avoid = [d for d in graded if d.grade == "avoid"]
    assert avoid
    response = client.get("/notifications")
    for day in avoid:
        assert day.reason in response.text
    assert f"/?farm={farm.id}#spray-window-{farm.id}-today" in response.text
    assert "hail" not in response.text.lower() and "storm" not in response.text.lower()
    client.cookies.set(
        f"farmcore_forecast_busy_{farm.id}", ",".join(d.day.isoformat() for d in avoid)
    )
    assert "Spray window: avoid" not in client.get("/notifications").text
    set_replay(session, local("2024-04-07 06:00"), settings)
    session.commit()
    assert "No forecast available" in client.get("/notifications").text


def test_read_cookie_is_bounded_farm_local_and_does_not_hide_current_risk(
    client, session, settings
):
    import re

    farm = ensure_default_farm(session, settings)
    complete_station(session, farm.id, NOW)
    parcel = make_parcel(session, farm, 1)
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="spray",
        props={"headline": "Synthetic stored risk"},
    )
    session.add(row)
    set_replay(session, NOW, settings)
    session.commit()
    first = client.get("/notifications").text
    key = re.search(r'data-notification-key="([0-9a-f]+)" data-severity="red"', first)[1]
    cookie = f"farmcore_notifications_read_{farm.id}"
    assert 'data-unread-count="' in first
    client.cookies.set(cookie, key)
    read = client.get("/notifications").text
    assert 'data-unread="false"' in read
    assert "Synthetic stored risk" in read
    # A routine reevaluation is the same item, not a new unread alert.
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="spray",
            props=row.props,
        )
    )
    session.commit()
    assert (
        f'data-notification-key="{key}" data-severity="red" data-unread="false"'
        in client.get("/notifications").text
    )
    for malformed in ("x" * 4000, "not-a-fingerprint", ",".join([key] * 101)):
        client.cookies.set(cookie, malformed)
        response = client.get("/notifications")
        assert response.status_code == 200
        assert (
            f'data-notification-key="{key}" data-severity="red" data-unread="true"' in response.text
        )
    client.cookies.set(cookie, key)
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="wait",
            props={"headline": "Synthetic closed window"},
        )
    )
    session.commit()
    assert 'data-severity="red" data-unread="true"' in client.get("/notifications").text


def test_unknown_advice_is_information_and_upcoming_window_is_not_infection_prediction(
    client, session, settings
):
    farm = ensure_default_farm(session, settings)
    complete_station(session, farm.id, NOW)
    parcel = make_parcel(session, farm, 1)
    set_replay(session, NOW, settings)
    session.commit()
    assert "No advice yet" in client.get("/notifications").text
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="wait",
        next_window_start=NOW + timedelta(hours=6),
        next_window_end=NOW + timedelta(hours=72),
        reasons=[{"rule": "spray_window", "threshold": {"window_horizon_h": 72}}],
        props={"headline": "Stored risk; window closed", "flags": ["replay_forecast"]},
    )
    session.add(row)
    session.commit()
    response = client.get("/notifications")
    assert response.text.count('data-severity="red"') == 1
    assert "Upcoming spray opportunity: 10 Apr 12:00" in response.text
    assert "observed weather" in response.text
    assert "until 13 Apr" not in response.text
    assert "No advice yet" not in response.text


def test_tour_get_and_app_off_keep_farm_boundaries(client, session, settings):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import AccessLog, Farm
    from farmcore.core.models.kernel import AppModule

    farm = ensure_default_farm(session, settings)
    other = Farm(
        name="Synthetic Spanish archive", props={"advisor_unavailable_reason": "ES archive only"}
    )
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    parcel = make_parcel(session, farm, 1)
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="spray",
            props={"headline": "French risk only"},
        )
    )
    set_replay(session, NOW, settings)
    session.commit()
    client.cookies.set("farmcore_tour", "1")
    assert client.get(f"/notifications?farm={farm.id}").status_code == 200
    assert client.post("/notifications").status_code == 403
    es = client.get(f"/notifications?farm={other.id}")
    assert "ES archive only" in es.text and "French risk only" not in es.text
    assert f"farmcore_notifications_read_{other.id}" in es.text
    assert 'data-severity="red"' not in es.text
    for name in ("advisor", "forecast", "stations"):
        session.get(AppModule, name).state = "uninstalled"
    session.commit()
    before = session.scalars(select(AccessLog.id)).all()
    off = client.get(f"/notifications?farm={farm.id}")
    assert "French risk only" not in off.text and 'data-unread-count="0"' in off.text
    session.expire_all()
    after = session.scalars(select(AccessLog.resource).where(AccessLog.id.not_in(before))).all()
    assert "advice" not in after and "observation" not in after and "forecast_run" not in after
    session.get(AppModule, "notifications").state = "uninstalled"
    session.commit()
    assert client.get("/notifications").text == ""


@pytest.mark.spanish_data
@pytest.mark.data
def test_real_demo_reset_step_live_loop_and_spanish_archive_have_expected_bell_items(
    client, session, settings, session_factory, manifests, seeded
):
    from farmcore.core import jobs
    from farmcore.core.models.canonical import Farm
    from tests.apps.advisor.scenes import confirm_spray
    from tests.core.test_demo_control import _stage

    seeded("B")
    farm = ensure_default_farm(session, settings)
    es = session.scalar(select(Farm).where(Farm.id != farm.id))
    assert es is not None
    assert _stage(client, farm.id).status_code == 303
    response = client.get(f"/notifications?farm={farm.id}")
    assert 'data-severity="red"' not in response.text
    assert "No advice yet" not in response.text
    assert all(r.ok for r in jobs.step_to(session_factory, manifests, NOW, settings))
    assert client.get("/notifications").text.count('data-severity="red"') == 10
    assert _stage(client, farm.id, preset="live-loop", action="preset").status_code == 303
    response = client.get("/notifications")
    assert response.text.count('data-severity="red"') == 3
    assert all(name in response.text for name in ("P5 Les Granges", "P6 Peyrot", "P7 La Fontaine"))
    confirm_spray(session, settings, "P5", local("2024-04-10 05:30"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok
    assert client.get("/notifications").text.count('data-severity="red"') == 2
    response = client.get(f"/notifications?farm={es.id}")
    assert "InfoRiego" in response.text
    assert (
        'data-severity="red"' not in response.text and 'data-severity="amber"' not in response.text
    )


def test_missing_forecast_variables_are_a_coverage_issue_not_weather_avoid(
    client, session, settings
):
    from dataclasses import replace

    from farmcore.apps.forecast import sources, store
    from tests.apps.forecast.conftest import FIXTURE

    farm = ensure_default_farm(session, settings)
    run = sources.FileSource(FIXTURE).runs()[0]
    # A run reaching the horizon but with all rain cells absent: ranking's old reason is Rain 0 mm.
    run = replace(run, values=[v for v in run.values if v[1] != "rr1_mm"])
    store.import_runs(session, farm.id, [run])
    set_replay(session, local("2024-04-08 06:00"), settings)
    session.commit()
    text = client.get("/notifications").text
    assert "Forecast coverage incomplete" in text
    assert "Spray window: avoid" not in text
    assert "Rain 0.0 mm" not in text


def test_new_issue_of_unchanged_forecast_does_not_resurrect_read_items(client, session, settings):
    import re
    from dataclasses import replace

    from farmcore.apps.forecast import sources, store
    from tests.apps.forecast.conftest import FIXTURE

    farm = ensure_default_farm(session, settings)
    run = sources.FileSource(FIXTURE).runs()[0]
    store.import_runs(session, farm.id, [run])
    set_replay(session, local("2024-04-09 06:00"), settings)
    session.commit()
    first = client.get("/notifications").text
    keys = re.findall(r'data-notification-key="([0-9a-f]+)"', first)
    client.cookies.set(f"farmcore_notifications_read_{farm.id}", ",".join(keys))
    store.import_runs(
        session, farm.id, [replace(run, issued_at=run.issued_at + timedelta(hours=1))]
    )
    session.commit()
    second = client.get("/notifications").text
    assert re.findall(r'data-notification-key="([0-9a-f]+)"', second) == keys
    assert 'data-unread-count="0"' in second


def test_bell_reports_an_open_check_and_the_advisor_answer(committed_session_factory, settings):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from farmcore.apps.review import recommendations
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.clock import set_replay
    from farmcore.core.models.canonical import Farm
    from farmcore.main import create_app
    from tests.apps.share.conftest import make_parcel
    from tests.core.test_member_sessions import _account, _login

    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    _, farm_id = _account(committed_session_factory, login="owner")
    _account(committed_session_factory, login="expert", role="advisor", farm_id=farm_id)
    with committed_session_factory.begin() as session:
        farm = session.get(Farm, farm_id)
        parcel = make_parcel(session, farm, 1)
        row = Advice(
            farm_id=farm_id,
            parcel_id=parcel.id,
            evaluated_at=now,
            decision="wait",
            reasons=[],
            products=[],
            props={"headline": "Stored wait", "assessment_status": "evaluable"},
        )
        session.add(row)
        session.flush()
        set_replay(session, now)
        repo = ScopedRepository(
            session, farm_id, ensure_owner_grant(session, farm), "owner", "test"
        )
        asked = recommendations.request_check(
            repo, now, row, parcel, note="Please check", requested_by="owner"
        )
        request_id = asked.request.id
        advice_id = row.id
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        bell = owner.get("/notifications", params={"farm": farm_id})
        assert bell.status_code == 200
        assert "Advisor check requested" in bell.text
        assert "P1" in bell.text
        assert "No answer yet" in bell.text

        answered = advisor.post(
            f"/review/requests/{request_id}/response",
            params={"farm": farm_id},
            json={"opinion": "agree", "note": "Looks right"},
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": advisor.cookies.get("farmcore_csrf"),
            },
        )
        assert answered.status_code == 201
        bell = owner.get("/notifications", params={"farm": farm_id})
        assert "Advisor check answered" in bell.text
        assert "expert" in bell.text
        assert "Agrees with this saved check" in bell.text
        assert "Please check" not in bell.text  # the note stays on the review page/card
    assert advice_id


def _review_farm(factory):
    from datetime import UTC, datetime

    from farmcore.apps.review import recommendations
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.clock import set_replay
    from farmcore.core.models.canonical import Farm
    from tests.apps.share.conftest import make_parcel
    from tests.core.test_member_sessions import _account

    now = datetime(2024, 4, 10, 4, tzinfo=UTC)
    _, farm_id = _account(factory, login="owner")
    _account(factory, login="expert", role="advisor", farm_id=farm_id)
    with factory.begin() as session:
        farm = session.get(Farm, farm_id)
        parcel = make_parcel(session, farm, 1)
        row = Advice(
            farm_id=farm_id,
            parcel_id=parcel.id,
            evaluated_at=now,
            decision="none",
            reasons=[],
            products=[],
            props={"headline": "Stored check", "assessment_status": "evaluable"},
        )
        session.add(row)
        session.flush()
        set_replay(session, now)
        repo = ScopedRepository(
            session, farm_id, ensure_owner_grant(session, farm), "owner", "test"
        )
        asked = recommendations.request_check(
            repo, now, row, parcel, note="Please check", requested_by="owner"
        )
        recommendations.respond(
            repo,
            now,
            row,
            parcel,
            asked.request.id,
            opinion="agree",
            note="Private advisor note",
            reviewer="expert",
        )
    return farm_id, now


def test_the_bell_hides_review_metadata_from_an_anonymous_demo_visitor(
    committed_session_factory, settings
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    farm_id, _ = _review_farm(committed_session_factory)
    demo = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm_id]})
    with TestClient(
        create_app(demo, session_factory=committed_session_factory), base_url="https://testserver"
    ) as visitor:
        bell = visitor.get("/notifications", params={"farm": farm_id})
        assert bell.status_code == 200  # an enabled demo farm is readable anonymously
        assert "Advisor check" not in bell.text
        assert "expert" not in bell.text
        assert "Agrees with this saved check" not in bell.text
        assert "Private advisor note" not in bell.text


def test_the_bell_hides_review_metadata_from_a_member_of_another_farm(
    committed_session_factory, settings
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app
    from tests.core.test_member_sessions import _account, _login

    farm_id, _ = _review_farm(committed_session_factory)
    _account(committed_session_factory, login="other-owner")
    demo = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm_id]})
    with TestClient(
        create_app(demo, session_factory=committed_session_factory), base_url="https://testserver"
    ) as other:
        assert _login(other, login="other-owner").status_code == 303
        bell = other.get("/notifications", params={"farm": farm_id})
        assert bell.status_code == 403
        assert "Private advisor note" not in bell.text
        assert "Agrees with this saved check" not in bell.text


def test_review_items_need_an_explicit_member_capability(committed_session_factory, settings):
    from farmcore.apps.notifications.service import items
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.models.canonical import Farm

    farm_id, now = _review_farm(committed_session_factory)
    with committed_session_factory() as session:
        farm = session.get(Farm, farm_id)
        repo = ScopedRepository(
            session, farm_id, ensure_owner_grant(session, farm), "owner", "test"
        )
        tz = settings.display_tz
        without = items(repo, now, tz, ["review"])
        with_capability = items(repo, now, tz, ["review"], review_reader=True)
    assert not any("Advisor check" in i.title for i in without)
    assert any("Advisor check answered" in i.title for i in with_capability)
