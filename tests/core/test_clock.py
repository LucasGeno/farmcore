"""Clock: live/replay, shared `clock_state`, local-time parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from farmcore.core import jobs
from farmcore.core.clock import LiveClock, get_clock, parse_local, set_replay
from farmcore.core.models.kernel import ClockState
from farmcore.db import make_engine


def test_parse_local_converts_paris_wall_time_to_utc() -> None:
    assert parse_local("2024-04-10 06:00", "Europe/Paris") == datetime(2024, 4, 10, 4, tzinfo=UTC)
    # The `datetime-local` input form (T separator) and winter time (UTC+1).
    assert parse_local("2024-01-15T06:00", "Europe/Paris") == datetime(2024, 1, 15, 5, tzinfo=UTC)


def test_parse_local_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_local("tomorrow", "Europe/Paris")


def test_get_clock_creates_the_row_from_settings_replay_start(session, settings) -> None:
    assert session.get(ClockState, 1) is None

    clock = get_clock(session, settings)

    # replay_start 2024-04-01 06:00 Paris (CEST) == 04:00 UTC
    assert clock.now() == datetime(2024, 4, 1, 4, 0, tzinfo=UTC)
    assert session.get(ClockState, 1).mode == "replay"


def test_get_clock_in_live_mode_is_wall_time(session, settings) -> None:
    clock = get_clock(session, settings.model_copy(update={"clock_mode": "live"}))
    assert isinstance(clock, LiveClock)
    assert abs(clock.now() - datetime.now(UTC)) < timedelta(seconds=5)


def test_set_replay_requires_an_aware_datetime(session, settings) -> None:
    with pytest.raises(ValueError):
        set_replay(session, datetime(2024, 4, 10, 6, 0), settings)


def test_a_second_process_sees_the_stepped_time(committed_session_factory, settings) -> None:
    t = datetime(2024, 5, 15, 4, 0, tzinfo=UTC)
    jobs.step_to(committed_session_factory, [], t, settings)

    # A separate engine stands in for the bot process.
    other = make_engine(settings)
    try:
        with sessionmaker(bind=other)() as bot_session:
            assert get_clock(bot_session, settings).now() == t
    finally:
        other.dispose()


# --- HTTP: /clock partial and /clock/step -------------------------------------------------


def _app_client(settings, session_factory, **overrides):
    from fastapi.testclient import TestClient

    from farmcore.core.app_manifest import MANIFEST as CORE
    from farmcore.core.manifest import JobSpec, Manifest
    from farmcore.main import create_app

    manifests = [
        CORE,
        Manifest(
            name="a",
            version="0.1.0",
            jobs=(JobSpec("a.job", "tests.core.fake_jobs:record", timedelta(hours=1)),),
        ),
        Manifest(
            name="b",
            version="0.1.0",
            jobs=(JobSpec("b.job", "tests.core.fake_jobs:fail", timedelta(hours=1)),),
        ),
    ]
    app = create_app(
        settings.model_copy(update=overrides), manifests=manifests, session_factory=session_factory
    )
    return TestClient(
        app,
        headers={}
        if "admin_token" in overrides
        else {"X-Admin-Token": settings.admin_token.get_secret_value()},
    )


def test_get_clock_partial_shows_mode_time_input_and_presets(client) -> None:
    resp = client.get("/clock")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="replay-control"' in html
    assert "replay" in html
    assert "01 Apr 2024 06:00" in html  # replay_start, local
    assert 'type="datetime-local"' in html
    assert "2024-04-10T06:00" in html and "10 Apr 06:00" in html
    assert "2024-05-15T06:00" in html and "15 May 06:00" in html
    assert 'hx-post="/clock/step"' in html
    # The partial must not reload itself (the navbar slot does that once).
    assert 'hx-trigger="load"' not in html
    # Form and presets share one sync point: a double-click drops the second step.
    assert html.count('hx-sync="#replay-control:drop"') == 1 + 2
    # A refused step (403 once admin_token is set) is shown, not silently dropped.
    assert "hx-on::response-error" in html and 'id="replay-error"' in html


def test_post_clock_step_updates_clock_state_and_refreshes(client, session) -> None:
    resp = client.post("/clock/step", data={"to": "2024-04-10T06:00"})

    assert resp.status_code == 200
    assert resp.headers["HX-Refresh"] == "true"
    assert "10 Apr 2024 06:00" in resp.text
    session.expire_all()
    row = session.get(ClockState, 1)
    assert (row.mode, row.replay_now) == ("replay", datetime(2024, 4, 10, 4, 0, tzinfo=UTC))


def test_clock_partial_summarises_the_last_steps_job_runs(settings, session_factory) -> None:
    from tests.core import fake_jobs

    fake_jobs.CALLS.clear()
    with _app_client(settings, session_factory) as c:
        stepped = c.post("/clock/step", data={"to": "2024-04-10T06:00"})
        shown = c.get("/clock")
    for html in (stepped.text, shown.text):
        assert "a.job" in html and "b.job" in html
        assert "RuntimeError" in html
        assert "fake job failure" not in html  # messages may quote data rows


def test_post_clock_step_rejects_a_bad_time(client) -> None:
    assert client.post("/clock/step", data={"to": "soon"}).status_code == 422


def test_post_clock_step_requires_the_admin_token_when_set(settings, session_factory) -> None:
    form = {"to": "2024-04-10T06:00"}
    token = SecretStr("tok-test")
    with _app_client(settings, session_factory, admin_token=token) as c:
        assert c.post("/clock/step", data=form).status_code == 403
        bad = c.post("/clock/step", data=form, headers={"X-Admin-Token": "nope"})
        assert bad.status_code == 403
        ok = c.post("/clock/step", data=form, headers={"X-Admin-Token": "tok-test"})
        assert ok.status_code == 200
        c.cookies.set("admin_token", "tok-test")
        assert c.post("/clock/step", data=form).status_code == 403
        c.headers["Origin"] = "http://testserver"
        assert c.post("/clock/step", data=form).status_code == 200


def test_navbar_slot_loads_the_clock_partial(client) -> None:
    from starlette.requests import Request

    from farmcore import web

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "app": client.app}
    html = web.render(Request(scope), "core/base.html").body.decode()
    assert html.count('id="replay-control"') == 1
    assert 'hx-get="/clock"' in html and 'hx-trigger="load"' in html
