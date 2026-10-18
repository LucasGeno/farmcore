"""Admin demo controls over HTTP; reset scope and transaction boundaries are observable."""

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event

from farmcore.core.clock import clock_row
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.share.conftest import make_parcel

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def demo_client(settings, session_factory):
    configured = settings.model_copy(update={"admin_token": SecretStr("demo-test")})
    app = create_app(configured, session_factory=session_factory)
    with session_factory() as session:
        from farmcore.apps.operations.seed import ensure_products

        ensure_products(session)
        farm = ensure_default_farm(session, configured)
        make_parcel(session, farm, 1)
        clock_row(session, configured)
        session.commit()
    with TestClient(app) as client:
        client.cookies.set("admin_token", "demo-test")
        yield client


def test_demo_page_is_read_only_and_admin_only(demo_client, connection):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        page = demo_client.get("/demo/")
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert page.status_code == 200
    assert "Demo controls" in page.text and "Demo start" in page.text
    assert 'href="/demo/"' in page.text
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", s, re.I) for s in statements)
    demo_client.cookies.clear()
    assert demo_client.get("/demo/").status_code == 403
    assert 'href="/demo/"' not in demo_client.get("/apps").text
    demo_client.cookies.set("admin_token", "demo-test")
    demo_client.cookies.set("farmcore_tour", "1")
    assert demo_client.get("/demo/").status_code == 403
    assert demo_client.post("/demo/apply").status_code == 403
    assert 'href="/demo/"' not in demo_client.get("/apps").text


def _generation(client):
    page = client.get("/demo/")
    assert page.status_code == 200
    return int(re.search(r'name="generation" value="(\d+)"', page.text)[1])


def _stage(client, farm_id, *, preset="demo-start", action="reset", **extra):
    form = {
        "action": action,
        "preset": preset,
        "farm_id": farm_id,
        "generation": _generation(client),
        **extra,
    }
    preview = client.post("/demo/preview", data=form, headers=ORIGIN)
    assert preview.status_code == 200
    assert "Confirm" in preview.text
    return client.post(
        "/demo/apply", data={**form, "confirm": "yes"}, headers=ORIGIN, follow_redirects=False
    )


def test_confirmed_reset_is_scoped_preserves_binding_and_reseeds_idempotently(
    demo_client, session, settings
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from farmcore.core.access import default_farm, ensure_owner_grant
    from farmcore.core.clock import get_clock
    from farmcore.core.models.canonical import AccessLog, Advice, Event, ExtId, Farm, Log, LogSource

    farm = default_farm(session, settings)
    farm.props = {"chat": {"telegram_chat_ids": [12345], "push_cursor": 42}, "keep": "yes"}
    other = Farm(name="Other farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    make_parcel(session, other, 1)
    old = Log(
        farm_id=farm.id,
        type="spray",
        status="pending",
        name="Synthetic old operation",
        ts_start=datetime(2024, 4, 1, tzinfo=UTC),
        props={},
    )
    kept = Log(
        farm_id=other.id,
        type="spray",
        status="done",
        name="Other farm operation",
        ts_start=datetime(2024, 4, 1, tzinfo=UTC),
        props={},
    )
    session.add_all([old, kept])
    session.flush()
    old_id, kept_id, other_id, farm_id = old.id, kept.id, other.id, farm.id
    session.add(ExtId(source="test-reset", key="old", model="log", res_id=old_id))
    session.add_all(
        [
            AccessLog(
                farm_id=farm.id,
                actor="test",
                resource="farm",
                scope="fields:read",
                purpose="test",
                ts=farm.created_at + timedelta(seconds=1),
            ),
            AccessLog(
                farm_id=farm.id,
                actor="before-seed",
                resource="farm",
                scope="fields:read",
                purpose="test",
                ts=farm.created_at - timedelta(seconds=1),
            ),
        ]
    )
    session.commit()
    before_clock = get_clock(session, settings).now()
    preview = demo_client.post(
        "/demo/preview",
        headers=ORIGIN,
        data={"action": "reset", "preset": "demo-start", "farm_id": farm_id, "generation": 0},
    )
    assert preview.status_code == 200
    session.expire_all()
    assert session.get(Log, old_id) is not None
    assert get_clock(session, settings).now() == before_clock
    for _ in range(2):
        response = _stage(demo_client, farm_id)
        assert response.status_code == 303
        session.expire_all()
        assert session.get(Log, old_id) is None
        assert session.get(Log, kept_id).name == "Other farm operation"
        assert session.get(Farm, farm_id).props["chat"] == {
            "telegram_chat_ids": [12345],
            "push_cursor": 42,
        }
        assert session.get(Farm, farm_id).props["keep"] == "yes"
        assert (
            session.scalar(select(func.count()).select_from(Log).where(Log.farm_id == farm_id)) == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ExtId).where(ExtId.source == "test-reset")
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Advice).where(Advice.farm_id == other_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(AccessLog).where(AccessLog.farm_id == other_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(AccessLog).where(AccessLog.actor == "before-seed")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count()).select_from(AccessLog).where(AccessLog.actor == "test")
            )
            == 0
        )
        assert get_clock(session, settings).now() == datetime(2024, 4, 8, 4, tzinfo=UTC)
        staged = session.scalar(select(Log).where(Log.farm_id == farm_id))
        assert "Demo data" in staged.notes
        assert staged.name.startswith("Demo data")
        assert (
            session.scalar(
                select(func.count()).select_from(LogSource).where(LogSource.source_type == "seed")
            )
            == 1
        )
    assert (
        session.scalar(select(func.count()).select_from(Event).where(Event.kind == "demo.reset"))
        == 2
    )
    activity = demo_client.get("/activity/")
    assert activity.status_code == 200 and "Demo data" in activity.text


def test_reset_requires_origin_confirmation_and_current_generation(demo_client, session, settings):
    from farmcore.core.access import default_farm

    farm_id = default_farm(session, settings).id
    form = {"action": "reset", "preset": "demo-start", "farm_id": farm_id, "generation": 0}
    assert demo_client.post("/demo/apply", data={**form, "confirm": "yes"}).status_code == 403
    assert (
        demo_client.post(
            "/demo/apply",
            data={**form, "confirm": "yes"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert demo_client.post("/demo/apply", data=form, headers=ORIGIN).status_code == 422
    assert (
        demo_client.post(
            "/demo/apply", data={**form, "farm_id": 2**63, "confirm": "yes"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        demo_client.post(
            "/demo/preview", data={**form, "preset": "module:unregistered"}, headers=ORIGIN
        ).status_code
        == 422
    )
    assert _stage(demo_client, farm_id).status_code == 303
    assert (
        demo_client.post("/demo/apply", data={**form, "confirm": "yes"}, headers=ORIGIN).status_code
        == 409
    )
    assert demo_client.post("/demo/preview", data=form, headers=ORIGIN).status_code == 409


def failing_scene(session, farm, now):
    from farmcore.core.models.canonical import Log

    session.add(
        Log(farm_id=farm.id, type="spray", status="done", name="Must roll back", ts_start=now)
    )
    session.flush()
    raise ValueError("scene failure")


def test_scene_failure_rolls_back_deleted_rows_clock_and_generation(demo_client, session, settings):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from farmcore.core.access import default_farm
    from farmcore.core.clock import get_clock
    from farmcore.core.demo import Preset
    from farmcore.core.manifest import Manifest
    from farmcore.core.models.canonical import Log

    # A registered test app is a real preset provider, not a mocked reset collaborator.
    globals()["FAILURE"] = Preset(
        "failure",
        "Failing demo",
        "Transaction rollback test",
        datetime(2024, 5, 15, 6, tzinfo=ZoneInfo("Europe/Paris")),
        f"{__name__}:failing_scene",
    )
    demo_client.app.state.manifests.append(
        Manifest(name="failure-test", version="1", demo_presets=(f"{__name__}:FAILURE",))
    )
    farm = default_farm(session, settings)
    old = Log(
        farm_id=farm.id,
        type="spray",
        status="done",
        name="Keep on failure",
        ts_start=get_clock(session).now(),
    )
    session.add(old)
    session.commit()
    old_id, farm_id, before = old.id, farm.id, get_clock(session).now()
    response = _stage(demo_client, farm_id, preset="failure")
    assert response.status_code == 409
    assert "rolled back" in response.text
    session.expire_all()
    assert session.get(Log, old_id).name == "Keep on failure"
    assert get_clock(session).now() == before
    assert _generation(demo_client) == 0


def test_popover_wording_and_admin_demo_link(demo_client):
    message = "The administrator steps this clock to replay the season"
    admin = demo_client.get("/clock")
    assert message in admin.text and 'href="/demo/"' in admin.text
    demo_client.cookies.clear()
    anonymous = demo_client.get("/clock")
    assert message in anonymous.text and 'href="/demo/"' not in anonymous.text


def test_presets_step_and_back_to_replay_record_activity(demo_client, session, settings):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from farmcore.core.access import default_farm
    from farmcore.core.clock import get_clock
    from farmcore.core.models.canonical import Event, Log

    farm_id = default_farm(session, settings).id
    assert _stage(demo_client, farm_id, preset="season-end", action="preset").status_code == 303
    session.expire_all()
    assert get_clock(session).now() == datetime(2024, 5, 15, 4, tzinfo=UTC)
    before_ids = list(session.scalars(select(Log.id)))
    assert _stage(demo_client, farm_id, action="step", to="2024-05-16T06:00").status_code == 303
    session.expire_all()
    assert get_clock(session).now() == datetime(2024, 5, 16, 4, tzinfo=UTC)
    assert list(session.scalars(select(Log.id))) == before_ids
    assert _stage(demo_client, farm_id, action="replay").status_code == 303
    session.expire_all()
    assert get_clock(session).now() == datetime(2024, 5, 15, 4, tzinfo=UTC)
    kinds = set(session.scalars(select(Event.kind).where(Event.kind.like("demo.%"))))
    assert {"demo.preset", "demo.step", "demo.replay"} <= kinds


def test_live_request_refuses_honestly_without_changing_replay_clock(
    demo_client, session, settings
):
    from sqlalchemy import select

    from farmcore.core.access import default_farm
    from farmcore.core.clock import get_clock
    from farmcore.core.models.canonical import Event
    from farmcore.core.models.kernel import ClockState

    farm_id, before = default_farm(session, settings).id, get_clock(session).now()
    response = _stage(demo_client, farm_id, action="live")
    assert response.status_code == 303
    session.expire_all()
    assert session.get(ClockState, 1).mode == "replay"
    assert get_clock(session).now() == before
    page = demo_client.get("/demo/")
    assert "No station data" in page.text and "No forecast" in page.text
    assert "scheduler" in page.text and "saved CSV" in page.text
    assert session.scalar(select(Event.id).where(Event.kind == "demo.live_refused")) is not None


def test_failed_due_job_is_visible_after_committed_reset(demo_client, session, settings):
    from datetime import timedelta

    from farmcore.core.access import default_farm
    from farmcore.core.manifest import JobSpec, Manifest

    demo_client.app.state.manifests.append(
        Manifest(
            name="failing-job-test",
            version="1",
            jobs=(JobSpec("demo-test.failure", "tests.core.fake_jobs:fail", timedelta(hours=1)),),
        )
    )
    farm_id = default_farm(session, settings).id
    assert _stage(demo_client, farm_id).status_code == 303
    page = demo_client.get("/demo/")
    assert "demo-test.failure" in page.text and "FAILED (RuntimeError)" in page.text
    assert _generation(demo_client) == 1


def test_concurrent_confirmations_apply_one_reset(committed_session_factory, settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import func, select

    from farmcore.apps.operations.seed import ensure_products
    from farmcore.core.models.canonical import Event, Log

    factory = committed_session_factory
    with factory() as session:
        ensure_products(session)
        farm = ensure_default_farm(session, settings)
        farm_id = farm.id
        make_parcel(session, farm, 1)
        clock_row(session, settings)
        session.commit()
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr("demo-test")}), session_factory=factory
    )
    barrier = Barrier(2)

    def confirm():
        with TestClient(app) as client:
            client.cookies.set("admin_token", "demo-test")
            barrier.wait(timeout=10)
            return client.post(
                "/demo/apply",
                headers=ORIGIN,
                follow_redirects=False,
                data={
                    "action": "reset",
                    "preset": "demo-start",
                    "farm_id": farm_id,
                    "generation": 0,
                    "confirm": "yes",
                },
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(statuses) == [303, 409]
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Event).where(Event.kind == "demo.reset")
            )
            == 1
        )
        assert (
            session.scalar(select(func.count()).select_from(Log).where(Log.farm_id == farm_id)) == 1
        )


def test_legacy_clock_step_invalidates_demo_confirmation(demo_client, session, settings):
    from farmcore.core.access import default_farm

    farm_id = default_farm(session, settings).id
    generation = _generation(demo_client)
    form = {
        "action": "reset",
        "preset": "demo-start",
        "farm_id": farm_id,
        "generation": generation,
        "confirm": "yes",
    }
    assert demo_client.post("/clock/step", data={"to": "2024-04-10T06:00"}).status_code == 200
    response = demo_client.post("/demo/apply", data=form, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 409


def test_scoped_demo_jobs_do_not_suppress_the_next_global_step(demo_client, session, settings):
    from datetime import timedelta

    from farmcore.core.access import default_farm
    from farmcore.core.manifest import JobSpec, Manifest
    from farmcore.core.models.kernel import ClockState
    from tests.core import fake_jobs

    demo_client.app.state.manifests.append(
        Manifest(
            name="scope-test",
            version="1",
            jobs=(JobSpec("scope-test.job", "tests.core.fake_jobs:record", timedelta(hours=1)),),
        )
    )
    row = session.get(ClockState, 1)
    previous_global = {
        spec.name: "2024-04-10T04:00:00+00:00"
        for manifest in demo_client.app.state.manifests
        for spec in manifest.jobs
    }
    previous_global["untouched.job"] = "2024-04-01T04:00:00+00:00"
    row.job_runs = {"last_run": previous_global}
    session.commit()
    fake_jobs.CALLS.clear()
    assert _stage(demo_client, default_farm(session, settings).id).status_code == 303
    session.expire_all()
    assert session.get(ClockState, 1).job_runs["last_run"] == previous_global
    for _ in range(2):
        assert demo_client.post("/clock/step", data={"to": "2024-04-10T06:00"}).status_code == 200
        assert len([c for c in fake_jobs.CALLS if c[0] == "record"]) == 2
    # A subsequent genuinely global evaluation must also reconcile the scoped schedule.
    for _ in range(2):
        assert demo_client.post("/clock/step", data={"to": "2024-04-10T07:00"}).status_code == 200
        assert len([c for c in fake_jobs.CALLS if c[0] == "record"]) == 3


@pytest.mark.data
@pytest.mark.parametrize("entrypoint", ["web", "cli"])
def test_reset_then_legacy_step_revisits_spray_day_without_touching_other_farm(
    settings, session_factory, data_dir, monkeypatch, entrypoint
):
    from sqlalchemy import select, text

    from farmcore.apps.chat import push
    from farmcore.apps.chat.telegram import FakeTelegramSender
    from farmcore.apps.operations import seed as ops_seed
    from farmcore.core import jobs
    from farmcore.core import seed as core_seed
    from farmcore.core.access import default_farm, ensure_owner_grant
    from farmcore.core.commands.seed import run_seeds
    from farmcore.core.models.canonical import Advice, Asset, Farm
    from farmcore.core.models.kernel import ClockState
    from tests.apps.advisor.scenes import local

    configured = settings.model_copy(
        update={
            "admin_token": SecretStr("demo-test"),
            "telegram_bot_token": SecretStr("123:fake-test-token"),
        }
    )
    monkeypatch.setattr(core_seed, "get_settings", lambda: configured)
    monkeypatch.setattr(ops_seed, "get_settings", lambda: configured)
    app = create_app(configured, session_factory=session_factory)
    with session_factory() as session:
        run_seeds(session, app.state.manifests, scene="B", data_dir=data_dir)
        farm = default_farm(session, configured)
        farm.props = {**farm.props, "chat": {"telegram_chat_ids": [4242]}}
        farm_id = farm.id
        other = Farm(name="Synthetic other farm", props={"chat": {"telegram_chat_ids": [4243]}})
        session.add(other)
        session.flush()
        other_id = other.id
        ensure_owner_grant(session, other)
        make_parcel(session, other, 1)
        # An empty station still produces advice and audited reads if this farm is evaluated.
        session.add(Asset(farm_id=other_id, kind="station", name="Synthetic empty station"))
        session.commit()

    def other_snapshot():
        with session_factory() as session:
            result = {}
            for table in (
                "farm",
                "parcel",
                "asset",
                "data_grant",
                "access_log",
                "advice",
                "event",
                "log",
            ):
                column = "id" if table == "farm" else "farm_id"
                result[table] = session.scalars(
                    text(
                        f'SELECT row_to_json(t)::text FROM "{table}" t '
                        f"WHERE {column}=:farm ORDER BY id"
                    ),
                    {"farm": other_id},
                ).all()
            return result

    sender = FakeTelegramSender()
    push.set_sender(sender)
    try:
        with TestClient(app) as client:
            client.cookies.set("admin_token", "demo-test")

            def advance():
                if entrypoint == "web":
                    assert (
                        client.post("/clock/step", data={"to": "2024-04-10T06:00"}).status_code
                        == 200
                    )
                else:
                    runs = jobs.step_to(
                        session_factory, app.state.manifests, local("2024-04-10 06:00"), configured
                    )
                    assert all(run.ok for run in runs), runs

            def decisions():
                features = client.get("/fields/parcels.geojson").json()["features"]
                return {f["properties"]["alias"]: f["properties"]["decision"] for f in features}

            advance()
            assert decisions() == {f"P{n}": "spray" for n in range(1, 11)}
            assert len(sender.sent) == 1
            assert sender.sent[0].text.startswith("10 parcels → spray")
            original_other = other_snapshot()
            assert original_other["advice"]  # isolation is checked against an evaluated farm
            assert _stage(client, farm_id).status_code == 303
            assert decisions() == {f"P{n}": "none" for n in range(1, 11)}
            assert len(sender.sent) == 1
            assert other_snapshot() == original_other

            advance()
            assert decisions() == {f"P{n}": "spray" for n in range(1, 11)}
            assert len(sender.sent) == 2
            assert sender.sent[-1].chat_id == 4242
            assert sender.sent[-1].text.startswith("10 parcels → spray")
            assert other_snapshot() == original_other
            with session_factory() as session:
                last_step = session.get(ClockState, 1).job_runs["last_step"]
                assert {run["name"] for run in last_step["runs"]} >= {
                    "advisor.evaluate",
                    "chat.push_advice",
                }
                advice_ids = session.scalars(
                    select(Advice.id).where(Advice.farm_id == farm_id).order_by(Advice.id)
                ).all()
            advance()
            assert len(sender.sent) == 2
            assert other_snapshot() == original_other
            with session_factory() as session:
                assert session.get(ClockState, 1).job_runs["last_step"]["runs"] == []
                assert (
                    session.scalars(
                        select(Advice.id).where(Advice.farm_id == farm_id).order_by(Advice.id)
                    ).all()
                    == advice_ids
                )
    finally:
        push.set_sender(None)
