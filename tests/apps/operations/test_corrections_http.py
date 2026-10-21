"""Audited correction contracts through real HTTP and migrated PostGIS."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as property_settings
from hypothesis import strategies as st
from sqlalchemy import select

from farmcore.core import logs
from farmcore.core.logs import QuantityInput, SourceInput
from farmcore.core.models.canonical import LogSource, Parcel, Quantity
from farmcore.main import create_app
from tests.apps.chat.test_handlers import COPPER, NOW_LOCAL, seed_farm
from tests.core.test_member_sessions import _account, _login


def _sign_in(browser):
    """Sign in the account `_account` made; drop to a member session with CSRF."""
    assert _login(browser).status_code == 303
    browser.headers.update(
        {"Origin": "https://testserver", "X-CSRF-Token": browser.cookies["farmcore_csrf"]}
    )


@pytest.mark.parametrize("actor", ["operator", "member"])
def test_correction_preview_cancel_confirm_retains_original_receipt(
    settings, committed_session_factory, actor
):
    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        parcel = session.get(Parcel, ids["P1"])
        parcel.area_ha = Decimal("2")
        farm_id = parcel.farm_id
        result = logs.record_operation(
            session,
            farm_id=farm_id,
            type="spray",
            parcel_ids=[parcel.id],
            ts_start=NOW_LOCAL.replace(hour=5),
            ts_end=None,
            quantities=[QuantityInput("rate", Decimal("2"), "kg/ha", ids[COPPER])],
            source=SourceInput("file", "original", {"receipt": "unchanged"}, None, 20),
            actor="original-import",
            occurred_at=NOW_LOCAL,
        )
        log_id = result[0].id
        session.commit()
    if actor == "member":
        _account(committed_session_factory, farm_id=farm_id)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        if actor == "operator":
            browser.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        else:
            assert _login(browser).status_code == 303
            browser.headers.update(
                {"Origin": "https://testserver", "X-CSRF-Token": browser.cookies["farmcore_csrf"]}
            )
        body = {
            "request_key": "cancel-first",
            "reason": "Dose entered twice",
            "changes": {
                "quantities": [
                    {"product_id": ids[COPPER], "measure": "rate", "value": "1", "unit": "kg/ha"}
                ]
            },
        }
        endpoint = f"/operations/logs/{log_id}/corrections?farm={farm_id}"
        preview = browser.post(endpoint, json=body)
        assert preview.status_code == 200, preview.text
        data = preview.json()
        assert data["status"] == "pending"
        assert data["before"]["quantities"][0]["value"] == "2"
        assert data["after"]["quantities"][0]["value"] == "1"
        cancel = f"/operations/corrections/{data['id']}/cancel?farm={farm_id}"
        assert browser.post(cancel).json()["status"] == "cancelled"
        assert browser.post(cancel).json()["status"] == "cancelled"
        with committed_session_factory() as session:
            assert session.scalar(select(Quantity.value).where(Quantity.log_id == log_id)) == 2
        body["request_key"] = "confirm-second"
        data = browser.post(endpoint, json=body).json()
        confirm = f"/operations/corrections/{data['id']}/confirm?farm={farm_id}"
        assert browser.post(confirm).json()["status"] == "confirmed"
        assert browser.post(confirm).json()["status"] == "confirmed"
        with committed_session_factory() as session:
            assert session.scalar(select(Quantity.value).where(Quantity.log_id == log_id)) == 1
            [source] = session.scalars(select(LogSource).where(LogSource.log_id == log_id)).all()
            assert source.raw_payload == {"receipt": "unchanged"}


@pytest.fixture
def correction_case(settings, committed_session_factory):
    from types import SimpleNamespace

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        parcel = session.get(Parcel, ids["P1"])
        farm_id = parcel.farm_id
        parcel.area_ha = Decimal("2")
        log, _ = logs.record_operation(
            session,
            farm_id=farm_id,
            type="spray",
            parcel_ids=[parcel.id],
            ts_start=NOW_LOCAL.replace(hour=5),
            ts_end=None,
            quantities=[QuantityInput("rate", Decimal("2"), "kg/ha", ids[COPPER])],
            source=SourceInput("file", "original", {"private_receipt": "retain-me"}, None, 20),
            actor="original-import",
            occurred_at=NOW_LOCAL,
        )
        session.commit()
        log_id, quantity_id = log.id, log.quantities[0].id
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as browser:
        yield SimpleNamespace(
            browser=browser,
            farm_id=farm_id,
            log_id=log_id,
            ids=ids,
            quantity_id=quantity_id,
            endpoint=f"/operations/logs/{log_id}/corrections?farm={farm_id}",
            body={
                "request_key": "one",
                "reason": "Private correction reason",
                "changes": {
                    "quantities": [
                        {
                            "product_id": ids[COPPER],
                            "measure": "rate",
                            "value": "1",
                            "unit": "kg/ha",
                        }
                    ]
                },
            },
        )


def test_generic_log_events_do_not_copy_private_audit_receipts(
    correction_case, committed_session_factory
):
    from farmcore.core.access import ScopedRepository
    from farmcore.core.models.canonical import DataGrant, Event

    c = correction_case
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    assert (
        c.browser.post(
            f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    with committed_session_factory() as session:
        grant = DataGrant(
            farm_id=c.farm_id, scopes=["logs:read"], grantee="limited", purpose="test"
        )
        session.add(grant)
        session.flush()
        repo = ScopedRepository(session, c.farm_id, grant, "limited", "test")
        [event] = repo.list(Event, Event.kind == "log.corrected", scope="logs:read")
        assert event.payload == {"log_id": c.log_id, "correction_id": correction_id}
        assert event.body == "Spray corrected"
    timeline = c.browser.get(f"/operations/parcels/{c.ids['P1']}/timeline?farm={c.farm_id}")
    assert timeline.status_code == 200
    assert "Private correction reason" not in timeline.text
    assert "retain-me" not in timeline.text


@pytest.mark.parametrize("kind", ["preview", "confirm", "cancel", "edit", "receipt", "formpreview"])
def test_foreign_hostile_or_unknown_targets_are_uniformly_not_found(
    correction_case, committed_session_factory, kind
):
    """Another farm's real spray and correction, ids past bigint and ids that do not exist all
    answer 404 on the selected farm's correction routes, and nothing is written."""
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.corrections import OperationCorrection
    from tests.apps.chat.test_handlers import SQUARE

    c = correction_case
    with committed_session_factory() as session:
        other = Farm(name="Foreign farm")
        session.add(other)
        session.flush()
        ensure_owner_grant(session, other)
        parcel = Parcel(farm_id=other.id, alias="F1", name="Foreign", area_ha=1, geom=SQUARE)
        session.add(parcel)
        session.flush()
        foreign_log, _ = logs.record_operation(
            session,
            farm_id=other.id,
            type="spray",
            parcel_ids=[parcel.id],
            ts_start=NOW_LOCAL.replace(hour=5),
            ts_end=None,
            quantities=[QuantityInput("rate", Decimal("2"), "kg/ha", c.ids[COPPER])],
            source=SourceInput("file", "foreign", {}, None, 20),
            actor="original-import",
            occurred_at=NOW_LOCAL,
        )
        session.commit()
        other_id, foreign_log_id = other.id, foreign_log.id
    foreign_correction = c.browser.post(
        f"/operations/logs/{foreign_log_id}/corrections?farm={other_id}", json=c.body
    ).json()["id"]
    foreign = foreign_log_id if kind in {"preview", "edit", "formpreview"} else foreign_correction
    form = {
        "request_key": "absent-log",
        "reason": "Correct notes",
        "product_id": [str(c.ids[COPPER])],
        "dose_value": ["2"],
        "dose_unit": ["kg/ha"],
        "parcel_ids": [str(c.ids["P1"])],
        "ts_start": "2024-04-10T05:00",
        "notes": "Updated notes",
    }
    for target in (foreign, 2**63, 99999999):
        if kind == "edit":
            response = c.browser.get(f"/operations/logs/{target}/corrections?farm={c.farm_id}")
        elif kind == "receipt":
            response = c.browser.get(f"/operations/corrections/{target}?farm={c.farm_id}")
        elif kind == "formpreview":
            response = c.browser.post(
                f"/operations/logs/{target}/corrections/preview?farm={c.farm_id}", data=form
            )
        elif kind == "preview":
            response = c.browser.post(
                f"/operations/logs/{target}/corrections?farm={c.farm_id}", json=c.body
            )
        else:
            response = c.browser.post(f"/operations/corrections/{target}/{kind}?farm={c.farm_id}")
        assert response.status_code == 404, (kind, target)
    with committed_session_factory() as session:
        [only] = session.scalars(select(OperationCorrection)).all()
        assert (only.id, only.status) == (foreign_correction, "pending")


def test_independent_previews_conflict_after_one_commits(
    correction_case, committed_session_factory
):
    from farmcore.core.models.canonical import Event
    from farmcore.core.models.corrections import OperationCorrection

    c = correction_case
    first = c.browser.post(c.endpoint, json=c.body).json()
    c.body["request_key"] = "independent"
    second = c.browser.post(c.endpoint, json=c.body).json()
    assert first["id"] != second["id"]
    assert (
        c.browser.post(
            f"/operations/corrections/{first['id']}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    assert (
        c.browser.post(
            f"/operations/corrections/{second['id']}/confirm?farm={c.farm_id}"
        ).status_code
        == 409
    )
    with committed_session_factory() as session:
        assert session.scalar(select(Quantity.value).where(Quantity.log_id == c.log_id)) == 1
        assert session.get(OperationCorrection, second["id"]).status == "pending"
        assert len(session.scalars(select(Event).where(Event.kind == "log.corrected")).all()) == 1


@pytest.mark.parametrize("actions", [("confirm", "confirm"), ("confirm", "cancel")])
def test_concurrent_terminal_actions_have_one_durable_outcome(
    correction_case, committed_session_factory, actions
):
    from concurrent.futures import ThreadPoolExecutor

    from farmcore.core.models.canonical import Event
    from farmcore.core.models.corrections import OperationCorrection

    c = correction_case
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda action: c.browser.post(
                    f"/operations/corrections/{correction_id}/{action}?farm={c.farm_id}"
                ),
                actions,
            )
        )
    if actions[0] == actions[1]:
        assert [r.status_code for r in replies] == [200, 200]
    else:
        assert sorted(r.status_code for r in replies) == [200, 409]
    with committed_session_factory() as session:
        row = session.get(OperationCorrection, correction_id)
        expected = 1 if row.status == "confirmed" else 2
        assert session.scalar(select(Quantity.value).where(Quantity.log_id == c.log_id)) == expected
        assert len(session.scalars(select(Event).where(Event.kind == "log.corrected")).all()) == (
            1 if row.status == "confirmed" else 0
        )
        assert row.finalized_at and row.finalized_actor_key == "operator"


def test_retried_request_key_returns_the_same_correction_and_never_rewrites_it(
    correction_case, committed_session_factory
):
    from farmcore.core.models.canonical import Farm

    c = correction_case
    first = c.browser.post(c.endpoint, json=c.body).json()
    assert c.browser.post(c.endpoint, json=c.body).json() == first
    c.body["changes"]["quantities"][0]["value"] = "0.5"
    assert c.browser.post(c.endpoint, json=c.body).status_code == 409
    c.body["changes"]["quantities"][0]["value"] = "1"
    terminal = c.browser.post(
        f"/operations/corrections/{first['id']}/confirm?farm={c.farm_id}"
    ).json()
    # A retry after confirmation replays the receipt, even once the product left the catalogue.
    with committed_session_factory() as session:
        farm = session.get(Farm, c.farm_id)
        farm.props = {**farm.props, "product_ids": []}
        session.commit()
    replay = c.browser.post(c.endpoint, json=c.body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == terminal
    c.body["changes"]["quantities"][0]["value"] = "2"
    assert c.browser.post(c.endpoint, json=c.body).status_code == 409


def test_computed_stock_changes_once_after_confirm_only(correction_case, committed_session_factory):
    import re

    from farmcore.apps.inventory.models import InventoryStock

    c = correction_case
    with committed_session_factory() as session:
        session.add(
            InventoryStock(
                farm_id=c.farm_id, product_id=c.ids[COPPER], season=2024, unit="kg", purchased=20
            )
        )
        session.commit()

    def stock():
        response = c.browser.get(f"/inventory/?farm={c.farm_id}")
        assert response.status_code == 200
        return re.search(
            rf'<article data-stock="{c.ids[COPPER]}".*?</article>', response.text, re.S
        ).group()

    assert ">16</span>" in stock()  # 20 purchased - 2 kg/ha × 2 ha = 16
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    assert ">16</span>" in stock()
    assert (
        c.browser.post(
            f"/operations/corrections/{correction_id}/cancel?farm={c.farm_id}"
        ).status_code
        == 200
    )
    assert ">16</span>" in stock()
    c.body["request_key"] = "actually-confirm"
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    assert (
        c.browser.post(
            f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    assert ">18</span>" in stock()  # 20 - 1 kg/ha × 2 ha = 18
    assert (
        c.browser.post(
            f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    assert ">18</span>" in stock()


def test_postcommit_advice_runs_once_for_selected_farm_only(
    settings, correction_case, committed_session_factory
):
    """On a pooled engine whose lock waits give up after 1 s: an advisor run that waited on the
    request's farm lock would fail (or hang the installation) instead of writing advice."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from farmcore.core.models.canonical import Advice, Farm
    from tests.apps.chat.test_handlers import SQUARE
    from tests.apps.operations.test_spray_weather_capture import add_station, hours

    c = correction_case
    with committed_session_factory() as session:
        other = Farm(name="Other private farm")
        session.add(other)
        session.flush()
        other_id = other.id
        session.add(
            Parcel(farm_id=other_id, name="Other parcel", alias="P1", area_ha=1, geom=SQUARE)
        )
        add_station(session, c.farm_id, hours(3, 4, 5))
        add_station(session, other_id, hours(3, 4, 5))
        session.commit()

    def advice_counts():
        with committed_session_factory() as session:
            return [
                len(session.scalars(select(Advice).where(Advice.farm_id == farm_id)).all())
                for farm_id in (c.farm_id, other_id)
            ]

    engine = create_engine(settings.database_url, connect_args={"options": "-c lock_timeout=1000"})
    app = create_app(settings, session_factory=sessionmaker(bind=engine, expire_on_commit=False))
    try:
        with TestClient(app, base_url="https://testserver", headers=dict(c.browser.headers)) as b:
            correction_id = b.post(c.endpoint, json=c.body).json()["id"]
            assert advice_counts() == [0, 0]
            confirm = f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
            assert b.post(confirm).status_code == 200
            assert advice_counts() == [3, 0]
            assert b.post(confirm).status_code == 200
            assert advice_counts() == [3, 0]
            c.body["request_key"] = "cancel-no-advice"
            c.body["changes"]["quantities"][0]["value"] = "0.5"
            correction_id = b.post(c.endpoint, json=c.body).json()["id"]
            cancel = f"/operations/corrections/{correction_id}/cancel?farm={c.farm_id}"
            assert b.post(cancel).status_code == 200
            assert advice_counts() == [3, 0]
    finally:
        engine.dispose()


def test_real_constraint_failure_rolls_back_canonical_audit_and_postcommit(
    correction_case, committed_session_factory
):
    from sqlalchemy import text

    from farmcore.core.models.canonical import Advice, Event
    from farmcore.core.models.corrections import OperationCorrection
    from tests.apps.operations.test_spray_weather_capture import add_station, hours

    c = correction_case
    with committed_session_factory() as session:
        add_station(session, c.farm_id, hours(3, 4, 5))
        session.commit()
    preview = c.browser.post(c.endpoint, json=c.body).json()
    with committed_session_factory() as session:
        session.execute(
            text(
                "ALTER TABLE operation_correction ADD CONSTRAINT qa_force_rollback "
                "CHECK(status <> 'confirmed') NOT VALID"
            )
        )
        session.commit()
    try:
        with TestClient(
            c.browser.app,
            base_url="https://testserver",
            raise_server_exceptions=False,
            headers=dict(c.browser.headers),
        ) as browser:
            response = browser.post(
                f"/operations/corrections/{preview['id']}/confirm?farm={c.farm_id}"
            )
        assert response.status_code == 500
        with committed_session_factory() as session:
            [quantity] = session.scalars(select(Quantity).where(Quantity.log_id == c.log_id)).all()
            assert (quantity.id, quantity.value) == (c.quantity_id, Decimal("2"))
            row = session.get(OperationCorrection, preview["id"])
            assert row.status == "pending" and row.finalized_at is None
            assert (
                row.before_snapshot == preview["before"] and row.after_snapshot == preview["after"]
            )
            assert session.scalars(select(Event).where(Event.kind == "log.corrected")).all() == []
            assert session.scalars(select(Advice)).all() == []
    finally:
        with committed_session_factory() as session:
            session.execute(
                text("ALTER TABLE operation_correction DROP CONSTRAINT qa_force_rollback")
            )
            session.commit()
    # Same durable intent can be retried successfully after the external DB failure is removed.
    assert (
        c.browser.post(
            f"/operations/corrections/{preview['id']}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("dose", "0", id="non-positive-dose"),
        pytest.param("dose", "NaN", id="non-finite-dose"),
        pytest.param("dose", "2", id="unchanged-dose"),
        pytest.param("dose", "1e1000000", id="past-numeric-storage"),
        pytest.param("dose", "9" * 4097, id="past-rendering-budget"),
        pytest.param("unit", "kg", id="not-a-rate-unit"),
        pytest.param("product", 2**100, id="unknown-product"),
        pytest.param("changes", {"parcel_ids": [2**63 - 1]}, id="unknown-parcel"),
        pytest.param("changes", {}, id="no-change"),
        pytest.param("changes", {"notes": None}, id="required-value-cleared"),
        pytest.param("changes", {"ts_start": "2024-04-01T05:00:00"}, id="naive-time"),
        pytest.param(
            "changes",
            {"ts_start": "2024-04-10T05:00:00Z", "ts_end": "2024-04-09T05:00:00Z"},
            id="end-before-start",
        ),
        pytest.param("changes", {"status": "done"}, id="immutable-field"),
        pytest.param("reason", "   ", id="blank-reason"),
    ],
)
def test_invalid_correction_intent_is_rejected_without_writes(
    correction_case, committed_session_factory, field, value
):
    from farmcore.core.models.corrections import OperationCorrection

    c = correction_case
    dose = c.body["changes"]["quantities"][0]
    if field == "dose":
        dose["value"] = value
    elif field == "unit":
        dose["unit"] = value
    elif field == "product":
        dose["product_id"] = value
    else:
        c.body[field] = value
    assert c.browser.post(c.endpoint, json=c.body).status_code == 422
    with committed_session_factory() as session:
        assert session.scalars(select(OperationCorrection)).all() == []
        [q] = session.scalars(select(Quantity).where(Quantity.log_id == c.log_id)).all()
        assert (q.id, q.value) == (c.quantity_id, Decimal("2"))


def test_actual_machine_import_invalidates_preview_and_retains_weather_evidence(
    settings, committed_session_factory
):
    import copy

    from farmcore.apps.operations import isoxml_demo, spray_weather
    from farmcore.core.clock import set_replay
    from farmcore.core.models.canonical import Log
    from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm, voice_spray
    from tests.apps.operations.test_spray_weather_capture import add_station, hours

    with committed_session_factory() as session:
        farm = make_farm(session, settings)
        log = voice_spray(session, farm)
        log_id, farm_id = log.id, farm.repo.farm_id
        add_station(session, farm_id, hours(4, 5, 6))
        set_replay(session, local(CLOCK), settings)
        session.commit()
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as browser:
        endpoint = f"/operations/logs/{log_id}/corrections?farm={farm_id}"
        body = {
            "request_key": "before-import",
            "reason": "Correct notes",
            "changes": {"notes": "Farmer's corrected note"},
        }
        old = browser.post(endpoint, json=body).json()

        def upload():
            return browser.post(
                f"/operations/isoxml?farm={farm_id}",
                files={"file": ("TASKDATA.XML", isoxml_demo.demo_taskdata(), "application/xml")},
            )

        assert upload().status_code == 200
        assert (
            browser.post(f"/operations/corrections/{old['id']}/confirm?farm={farm_id}").status_code
            == 409
        )
        body["request_key"] = "after-import"
        fresh = browser.post(endpoint, json=body).json()
        machine = [s for s in fresh["before"]["sources"] if s["source_type"] == "isoxml"]
        assert len(machine) == 2
        assert {s["raw_payload"][spray_weather.NAMESPACE]["status"] for s in machine} == {
            "captured"
        }
        receipts = copy.deepcopy(fresh["before"]["sources"])
        assert (
            browser.post(
                f"/operations/corrections/{fresh['id']}/confirm?farm={farm_id}"
            ).status_code
            == 200
        )
        assert upload().status_code == 200  # original import replay cannot revert correction
        with committed_session_factory() as session:
            saved = session.get(Log, log_id)
            assert saved.notes == "Farmer's corrected note"
            assert [s.id for s in sorted(saved.sources, key=lambda s: s.id)] == [
                s["id"] for s in receipts
            ]
            assert [s.raw_payload for s in sorted(saved.sources, key=lambda s: s.id)] == [
                s["raw_payload"] for s in receipts
            ]
            assert [
                (q.id, str(q.value.normalize()))
                for q in sorted(saved.quantities, key=lambda q: q.id)
            ] == [(q["id"], q["value"]) for q in fresh["before"]["quantities"]]

        # Existing bounded operator reset retains this audit even when LogSource cascades.
        body["request_key"] = "cancel-before-reset"
        body["changes"]["notes"] = "Cancelled changed note"
        cancelled = browser.post(endpoint, json=body).json()
        assert (
            browser.post(
                f"/operations/corrections/{cancelled['id']}/cancel?farm={farm_id}"
            ).status_code
            == 200
        )
        body["request_key"] = "pending-before-reset"
        pending = browser.post(endpoint, json=body).json()
        reset = browser.post(
            "/demo/apply",
            data={
                "action": "reset",
                "preset": "demo-start",
                "farm_id": farm_id,
                "generation": 0,
                "confirm": "yes",
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert reset.status_code == 303, reset.text
        terminal = browser.post(f"/operations/corrections/{fresh['id']}/confirm?farm={farm_id}")
        assert terminal.status_code == 200 and terminal.json()["status"] == "confirmed"
        assert (
            terminal.json()["before"] == fresh["before"]
            and terminal.json()["after"] == fresh["after"]
        )
        assert (
            browser.post(f"/operations/corrections/{cancelled['id']}/cancel?farm={farm_id}").json()[
                "status"
            ]
            == "cancelled"
        )
        assert (
            browser.post(
                f"/operations/corrections/{pending['id']}/confirm?farm={farm_id}"
            ).status_code
            == 409
        )
        assert (
            browser.post(
                f"/operations/corrections/{pending['id']}/cancel?farm={farm_id}"
            ).status_code
            == 409
        )
        assert browser.post(endpoint, json=body).status_code == 409
        from farmcore.core.models.corrections import OperationCorrection

        with committed_session_factory() as session:
            retained = session.get(OperationCorrection, fresh["id"])
            assert retained.original_log_id == log_id and retained.live_log_id is None
            assert retained.before_snapshot["sources"] == receipts
            assert session.get(Log, log_id) is None


@pytest.mark.parametrize(
    "role,preview,finalize",
    [
        ("anonymous", 403, 403),
        ("advisor", 403, 403),
        ("other-farm", 403, 403),
        ("tour", 403, 403),
        ("co-owner", 200, 404),  # an owner previews their own, never finalizes another's
    ],
)
def test_only_the_author_can_finalize_and_only_owners_can_preview(
    correction_case, committed_session_factory, role, preview, finalize
):
    c = correction_case
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    if role == "tour":
        c.browser.cookies.set("farmcore_tour", "1")
    else:
        del c.browser.headers["X-Admin-Token"]
        if role != "anonymous":
            _account(
                committed_session_factory,
                farm_id=None if role == "other-farm" else c.farm_id,
                role="advisor" if role == "advisor" else "owner",
            )
            _sign_in(c.browser)
    assert c.browser.post(c.endpoint, json=c.body).status_code == preview
    for action in ("confirm", "cancel"):
        assert (
            c.browser.post(
                f"/operations/corrections/{correction_id}/{action}?farm={c.farm_id}"
            ).status_code
            == finalize
        )
    with committed_session_factory() as session:
        assert session.get(Quantity, c.quantity_id).value == Decimal("2")


@pytest.mark.parametrize("start", ["/operations/", "/operations/register"])
def test_owner_reaches_correction_from_records_and_confirms_in_the_browser(
    correction_case, committed_session_factory, start
):
    import re

    c = correction_case
    _account(committed_session_factory, farm_id=c.farm_id)
    del c.browser.headers["X-Admin-Token"]
    _sign_in(c.browser)
    page = c.browser.get(f"{start}?farm={c.farm_id}")
    assert page.status_code == 200
    link = re.search(rf'href="([^"]*/operations/logs/{c.log_id}/corrections[^"]*)"', page.text)
    assert link, "no correction screen reachable from the records"
    edit = c.browser.get(link.group(1).replace("&amp;", "&"))
    assert edit.status_code == 200
    key = re.search(r'name="request_key" value="([^"]+)"', edit.text).group(1)
    preview = c.browser.post(
        f"/operations/logs/{c.log_id}/corrections/preview?farm={c.farm_id}",
        data={
            "request_key": key,
            "reason": "Correct duplicated dose",
            "parcel_ids": [str(c.ids["P1"])],
            "product_id": [str(c.ids[COPPER])],
            "dose_value": ["1"],
            "dose_unit": ["kg/ha"],
            "ts_start": "2024-04-10T05:00",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert preview.status_code == 303, preview.text
    receipt = c.browser.get(preview.headers["location"])
    assert receipt.status_code == 200
    assert "Before" in receipt.text and "After" in receipt.text
    assert COPPER in receipt.text and "P1 Le Plateau" in receipt.text
    assert "Correct duplicated dose" in receipt.text
    correction_id = re.search(r"/operations/corrections/(\d+)/confirm", receipt.text).group(1)
    saved = c.browser.post(
        f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}",
        data={"confirm": "yes"},
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    history = c.browser.get(c.endpoint)
    assert history.status_code == 200 and "Confirmed" in history.text
    assert "Correct duplicated dose" in history.text


@pytest.mark.parametrize("revoke", ["membership", "identity", "session"])
def test_authority_revoked_while_request_waits_for_farm_lock_cannot_confirm(
    correction_case, committed_session_factory, revoke
):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime

    from sqlalchemy import text, update

    from farmcore.core.models.corrections import OperationCorrection
    from farmcore.core.models.identity import FarmMembership, LocalIdentity, LocalSession

    c = correction_case
    identity_id, _ = _account(committed_session_factory, farm_id=c.farm_id)
    del c.browser.headers["X-Admin-Token"]
    _sign_in(c.browser)
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    with committed_session_factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        logs.lock_farm(blocker, c.farm_id)
        pending = pool.submit(
            c.browser.post, f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        )
        deadline = time.monotonic() + 8
        while True:
            with committed_session_factory() as observer:
                waiting = observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event='advisory' "
                        "AND query LIKE '%pg_advisory_xact_lock%'"
                    )
                )
            if waiting:
                break
            assert time.monotonic() < deadline, "HTTP writer never reached the farm lock"
            time.sleep(0.01)
        now = datetime.now(UTC)
        model, column = {
            "membership": (FarmMembership, "revoked_at"),
            "identity": (LocalIdentity, "disabled_at"),
            "session": (LocalSession, "revoked_at"),
        }[revoke]
        criterion = (
            model.id == identity_id if model is LocalIdentity else model.identity_id == identity_id
        )
        blocker.execute(update(model).where(criterion).values(**{column: now}))
        blocker.commit()
        assert pending.result(timeout=10).status_code == 403
    with committed_session_factory() as session:
        assert session.get(OperationCorrection, correction_id).status == "pending"
        assert session.scalar(select(Quantity.value).where(Quantity.log_id == c.log_id)) == 2


def test_unchanged_html_form_shows_error_without_recording_correction(
    correction_case, committed_session_factory
):
    import re

    from farmcore.core.models.corrections import OperationCorrection

    c = correction_case
    edit = c.browser.get(c.endpoint)
    key = re.search(r'name="request_key" value="([^"]+)"', edit.text).group(1)
    result = c.browser.post(
        f"/operations/logs/{c.log_id}/corrections/preview?farm={c.farm_id}",
        data={
            "request_key": key,
            "reason": "No actual changed facts",
            "parcel_ids": [str(c.ids["P1"])],
            "product_id": [str(c.ids[COPPER])],
            "dose_value": ["2"],
            "dose_unit": ["kg/ha"],
            "ts_start": "2024-04-10T05:00",
            "notes": "",
        },
    )
    assert result.status_code == 422
    assert 'role="alert"' in result.text and "Supply changed values" in result.text
    with committed_session_factory() as session:
        assert session.scalars(select(OperationCorrection)).all() == []
        assert (
            session.scalar(select(Quantity.id).where(Quantity.log_id == c.log_id)) == c.quantity_id
        )


@pytest.mark.parametrize(
    "reader,pending,history,terminal",
    [
        ("co-owner", 404, 200, 200),
        ("advisor", 404, 200, 200),
        ("foreign-farm", 404, 404, 404),
        ("tour", 403, 403, 403),
        ("demo-visitor", 403, 403, 403),
    ],
)
def test_correction_audit_is_private_until_final_and_scoped_to_the_farm(
    correction_case, committed_session_factory, reader, pending, history, terminal
):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm

    c = correction_case
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    token = c.browser.headers.pop("X-Admin-Token")
    farm = c.farm_id
    if reader in {"co-owner", "advisor"}:
        role = "owner" if reader == "co-owner" else "advisor"
        _account(committed_session_factory, farm_id=c.farm_id, role=role)
        _sign_in(c.browser)
    elif reader == "foreign-farm":
        with committed_session_factory() as session:
            other = Farm(name="Foreign farm")
            session.add(other)
            session.flush()
            ensure_owner_grant(session, other)
            session.commit()
            farm = other.id
        c.browser.headers["X-Admin-Token"] = token
    elif reader == "tour":
        c.browser.headers["X-Admin-Token"] = token
        c.browser.cookies.set("farmcore_tour", "1")
    else:
        c.browser.app.state.settings = c.browser.app.state.settings.model_copy(
            update={"demo_mode": True, "demo_farm_ids": (c.farm_id,)}
        )
    receipt = f"/operations/corrections/{correction_id}?farm={farm}"
    assert c.browser.get(receipt).status_code == pending
    page = c.browser.get(f"/operations/logs/{c.log_id}/corrections?farm={farm}")
    assert page.status_code == history
    assert "Private correction reason" not in page.text
    if history == 200:
        assert ('name="request_key"' in page.text) is (reader == "co-owner")
    with TestClient(
        c.browser.app, base_url="https://testserver", headers={"X-Admin-Token": token}
    ) as operator:
        confirm = f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        assert operator.post(confirm).status_code == 200
    final = c.browser.get(receipt)
    assert final.status_code == terminal
    if terminal == 200:
        assert "Private correction reason" in final.text and "retain-me" in final.text
        assert "Confirm correction" not in final.text


@pytest.mark.parametrize("writer", ["replace-row-same-value", "change-last-digit"])
def test_another_writer_changing_the_record_invalidates_a_pending_preview(
    correction_case, committed_session_factory, writer
):
    from sqlalchemy import delete

    c = correction_case
    precise = Decimal("1.1234567890123456789012345678901")
    with committed_session_factory() as session:
        session.get(Quantity, c.quantity_id).value = precise
        session.commit()
    c.body["changes"] = {"notes": "Corrected notes"}
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    with committed_session_factory() as session:
        if writer == "replace-row-same-value":
            session.execute(delete(Quantity).where(Quantity.id == c.quantity_id))
            session.add(
                Quantity(
                    log_id=c.log_id,
                    product_id=c.ids[COPPER],
                    measure="rate",
                    value=precise,
                    unit="kg/ha",
                )
            )
        else:
            session.get(Quantity, c.quantity_id).value = Decimal(
                "1.1234567890123456789012345678902"
            )
        session.commit()
        written = [(q.id, q.value) for q in session.scalars(select(Quantity))]
    assert (
        c.browser.post(
            f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        ).status_code
        == 409
    )
    with committed_session_factory() as session:
        assert [(q.id, q.value) for q in session.scalars(select(Quantity))] == written


@property_settings(
    max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    dose=st.tuples(st.integers(1, 10**40), st.integers(-60, 20)).map(
        lambda parts: Decimal(f"{parts[0]}e{parts[1]}")
    )
)
def test_arbitrary_valid_doses_preserve_one_winner_and_original_receipt(
    correction_case, committed_session_factory, dose
):
    import copy
    import uuid

    c = correction_case
    key = uuid.uuid4().hex
    body = copy.deepcopy(c.body)
    body["request_key"] = key + "-first"
    body["changes"]["quantities"][0]["value"] = str(dose)
    body["changes"]["notes"] = key
    first = c.browser.post(c.endpoint, json=body)
    assert first.status_code == 200, first.text
    body["request_key"] = key + "-second"
    second = c.browser.post(c.endpoint, json=body)
    assert second.status_code == 200, second.text
    confirm = f"/operations/corrections/{first.json()['id']}/confirm?farm={c.farm_id}"
    saved = c.browser.post(confirm)
    assert saved.status_code == 200
    assert c.browser.post(confirm).json() == saved.json()
    assert (
        c.browser.post(
            f"/operations/corrections/{second.json()['id']}/confirm?farm={c.farm_id}"
        ).status_code
        == 409
    )
    with committed_session_factory() as session:
        [quantity] = session.scalars(select(Quantity).where(Quantity.log_id == c.log_id)).all()
        assert quantity.value == dose
        [source] = session.scalars(select(LogSource).where(LogSource.log_id == c.log_id)).all()
        assert source.raw_payload == {"private_receipt": "retain-me"}


@pytest.mark.parametrize(
    "original",
    [
        NOW_LOCAL.replace(hour=5, second=37, microsecond=123456),
        NOW_LOCAL.replace(
            month=10, day=27, hour=2, minute=30, second=37, microsecond=123456, fold=1
        ),
    ],
)
def test_dose_only_form_edit_retains_treatment_seconds_and_precision(
    correction_case, committed_session_factory, original
):
    import re
    from datetime import UTC

    from farmcore.core.models.canonical import Log

    c = correction_case
    with committed_session_factory() as session:
        session.get(Log, c.log_id).ts_start = original
        session.commit()
    edit = c.browser.get(c.endpoint)
    key = re.search(r'name="request_key" value="([^"]+)"', edit.text).group(1)
    shown = re.search(r'name="ts_start" value="([^"]+)"', edit.text).group(1)
    response = c.browser.post(
        f"/operations/logs/{c.log_id}/corrections/preview?farm={c.farm_id}",
        data={
            "request_key": key,
            "reason": "Only dose changed",
            "parcel_ids": [str(c.ids["P1"])],
            "product_id": [str(c.ids[COPPER])],
            "dose_value": ["1"],
            "dose_unit": ["kg/ha"],
            "ts_start": shown,
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    correction_id = re.search(r"/corrections/(\d+)", response.headers["location"]).group(1)
    saved = c.browser.post(f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}")
    assert saved.status_code == 200
    assert saved.json()["before"]["ts_start"] == original.astimezone(UTC).isoformat()
    assert saved.json()["after"]["ts_start"] == original.astimezone(UTC).isoformat()


def test_html_notes_edit_preserves_each_historical_duplicate_dose(
    correction_case, committed_session_factory
):
    import re

    c = correction_case
    with committed_session_factory() as session:
        session.add(
            Quantity(
                log_id=c.log_id,
                product_id=c.ids[COPPER],
                measure="rate",
                value=Decimal("0.5"),
                unit="kg/ha",
                label="Second recorded dose",
            )
        )
        session.commit()
        original = [
            (q.id, q.value, q.label)
            for q in session.scalars(
                select(Quantity).where(Quantity.log_id == c.log_id).order_by(Quantity.id)
            )
        ]
    edit = c.browser.get(c.endpoint)
    key = re.search(r'name="request_key" value="([^"]+)"', edit.text).group(1)
    result = c.browser.post(
        f"/operations/logs/{c.log_id}/corrections/preview?farm={c.farm_id}",
        data={
            "request_key": key,
            "reason": "Only notes changed",
            "parcel_ids": [str(c.ids["P1"])],
            "product_id": [str(c.ids[COPPER]), str(c.ids[COPPER])],
            "dose_value": ["2", "0.5"],
            "dose_unit": ["kg/ha", "kg/ha"],
            "ts_start": "2024-04-10T05:00",
            "notes": "Updated note",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303, result.text
    correction_id = re.search(r"/corrections/(\d+)", result.headers["location"]).group(1)
    assert (
        c.browser.post(
            f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    with committed_session_factory() as session:
        assert [
            (q.id, q.value, q.label)
            for q in session.scalars(
                select(Quantity).where(Quantity.log_id == c.log_id).order_by(Quantity.id)
            )
        ] == original


def test_changing_one_product_dose_retains_unchanged_quantity_identity(
    correction_case, committed_session_factory
):
    from tests.apps.chat.test_handlers import SULPHUR

    c = correction_case
    with committed_session_factory() as session:
        other = Quantity(
            log_id=c.log_id,
            product_id=c.ids[SULPHUR],
            measure="rate",
            value=Decimal("3"),
            unit="kg/ha",
        )
        session.add(other)
        session.commit()
        other_id = other.id
    c.body["changes"]["quantities"].append(
        {"product_id": c.ids[SULPHUR], "value": "3", "unit": "kg/ha"}
    )
    preview = c.browser.post(c.endpoint, json=c.body).json()
    assert (
        c.browser.post(
            f"/operations/corrections/{preview['id']}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    with committed_session_factory() as session:
        rows = session.scalars(select(Quantity).where(Quantity.log_id == c.log_id)).all()
        assert len(rows) == 2
        assert sorted((q.product_id, q.value) for q in rows) == [
            (c.ids[COPPER], Decimal("1")),
            (c.ids[SULPHUR], Decimal("3")),
        ]
        assert session.get(Quantity, other_id) is not None
        assert session.get(Quantity, other_id).value == 3


def test_historical_shortlist_row_stays_selected_and_notes_only_edit_keeps_identity(
    correction_case, committed_session_factory
):
    import re

    from farmcore.core.models.canonical import Farm, Log

    c = correction_case
    with committed_session_factory() as session:
        farm = session.get(Farm, c.farm_id)
        farm.props = {**farm.props, "product_ids": []}
        session.commit()
    page = c.browser.get(c.endpoint)
    assert page.status_code == 200
    assert f'value="{c.ids[COPPER]}" selected' in page.text
    assert COPPER in page.text and "Recorded product" in page.text
    key = re.search(r'name="request_key" value="([^"]+)"', page.text).group(1)
    form = {
        "request_key": key,
        "reason": "Correct notes only",
        "parcel_ids": [str(c.ids["P1"])],
        "product_id": [str(c.ids[COPPER])],
        "dose_value": ["2"],
        "dose_unit": ["kg/ha"],
        "ts_start": "2024-04-10T05:00",
        "notes": "Historical dose unchanged",
    }
    response = c.browser.post(
        f"/operations/logs/{c.log_id}/corrections/preview?farm={c.farm_id}",
        data=form,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    correction_id = re.search(r"/corrections/(\d+)", response.headers["location"]).group(1)
    assert (
        c.browser.post(
            f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    with committed_session_factory() as session:
        assert session.get(Log, c.log_id).notes == "Historical dose unchanged"
        [quantity] = session.scalars(select(Quantity).where(Quantity.log_id == c.log_id)).all()
        assert (quantity.id, quantity.value) == (c.quantity_id, Decimal("2"))
    # An actual dose change of the unavailable historical product still needs eligibility.
    c.body["request_key"] = "new-ineligible-dose"
    assert c.browser.post(c.endpoint, json=c.body).status_code == 422


def test_large_historical_numeric_value_is_retained_without_new_input_budget(
    correction_case, committed_session_factory
):
    c = correction_case
    historical = Decimal("1e5000")
    with committed_session_factory() as session:
        session.get(Quantity, c.quantity_id).value = historical
        session.commit()
    c.body["changes"]["quantities"][0]["value"] = str(historical)
    c.body["changes"]["notes"] = "Retain exact imported dose"
    response = c.browser.post(c.endpoint, json=c.body)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert Decimal(preview["before"]["quantities"][0]["value"]) == historical
    assert preview["before"]["quantities"] == preview["after"]["quantities"]
    assert (
        c.browser.post(
            f"/operations/corrections/{preview['id']}/confirm?farm={c.farm_id}"
        ).status_code
        == 200
    )
    with committed_session_factory() as session:
        quantity = session.get(Quantity, c.quantity_id)
        assert quantity is not None and quantity.value == historical
