"""Audited correction contracts through real HTTP and migrated PostGIS."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.operations import corrections
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
def test_preview_cancel_confirm_moves_computed_stock_once_and_keeps_the_receipt(
    correction_case, committed_session_factory, actor
):
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
    if actor == "member":
        _account(committed_session_factory, farm_id=c.farm_id)
        del c.browser.headers["X-Admin-Token"]
        _sign_in(c.browser)

    def stock():
        page = c.browser.get(f"/inventory/?farm={c.farm_id}")
        assert page.status_code == 200
        return re.search(rf'<article data-stock="{c.ids[COPPER]}".*?</article>', page.text, re.S)[0]

    assert ">16</span>" in stock()  # 20 kg bought - 2 kg/ha x 2 ha
    preview = c.browser.post(c.endpoint, json=c.body)
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["status"] == "pending"
    assert (data["before"]["quantities"][0]["value"], data["after"]["quantities"][0]["value"]) == (
        "2",
        "1",
    )
    assert ">16</span>" in stock()
    cancel = f"/operations/corrections/{data['id']}/cancel?farm={c.farm_id}"
    assert c.browser.post(cancel).json()["status"] == "cancelled"
    assert c.browser.post(cancel).json()["status"] == "cancelled"
    assert ">16</span>" in stock()
    c.body["request_key"] = "confirm-second"
    data = c.browser.post(c.endpoint, json=c.body).json()
    confirm = f"/operations/corrections/{data['id']}/confirm?farm={c.farm_id}"
    assert c.browser.post(confirm).json()["status"] == "confirmed"
    assert ">18</span>" in stock()  # 20 kg - 1 kg/ha x 2 ha
    assert c.browser.post(confirm).json()["status"] == "confirmed"
    assert ">18</span>" in stock()
    with committed_session_factory() as session:
        [source] = session.scalars(select(LogSource).where(LogSource.log_id == c.log_id)).all()
        assert source.raw_payload == {"private_receipt": "retain-me"}


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
            source=SourceInput(
                "file", "upload:receipt-7731", {"private_receipt": "retain-me"}, None, 20
            ),
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
    import json

    from farmcore.core.access import ScopedRepository
    from farmcore.core.models.canonical import DataGrant, Event

    c = correction_case
    correction_id = c.browser.post(c.endpoint, json=c.body).json()["id"]
    confirm = f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
    assert c.browser.post(confirm).status_code == 200
    with committed_session_factory() as session:
        grant = DataGrant(
            farm_id=c.farm_id, scopes=["logs:read"], grantee="limited", purpose="test"
        )
        session.add(grant)
        session.flush()
        repo = ScopedRepository(session, c.farm_id, grant, "limited", "test")
        [event] = repo.list(Event, Event.kind == "log.corrected", scope="logs:read")
        visible = event.body + json.dumps(event.payload)
        assert "Private correction reason" not in visible and "retain-me" not in visible
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
    from farmcore.core.models.corrections import OperationCorrection

    def stored(correction_id):
        with committed_session_factory() as session:
            row = session.get(OperationCorrection, correction_id)
            return (row.status, row.before_snapshot, row.after_snapshot, row.finalized_at)

    c = correction_case
    first = c.browser.post(c.endpoint, json=c.body).json()
    assert c.browser.post(c.endpoint, json=c.body).json() == first
    c.body["changes"]["quantities"][0]["value"] = "0.5"
    assert c.browser.post(c.endpoint, json=c.body).status_code == 409
    c.body["changes"]["quantities"][0]["value"] = "1"
    confirm = f"/operations/corrections/{first['id']}/confirm?farm={c.farm_id}"
    terminal = c.browser.post(confirm).json()
    receipt = stored(first["id"])
    # Retrying the confirm, or the preview once the product left the catalogue, replays the
    # final receipt and never rewrites it.
    assert c.browser.post(confirm).json() == terminal
    with committed_session_factory() as session:
        farm = session.get(Farm, c.farm_id)
        farm.props = {**farm.props, "product_ids": []}
        session.commit()
    replay = c.browser.post(c.endpoint, json=c.body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == terminal
    assert stored(first["id"]) == receipt
    c.body["changes"]["quantities"][0]["value"] = "2"
    assert c.browser.post(c.endpoint, json=c.body).status_code == 409
    assert stored(first["id"]) == receipt


def test_postcommit_advice_runs_once_for_selected_farm_only(
    settings, correction_case, committed_session_factory, pooled_session_factory
):
    """On a pooled engine whose lock waits give up after 1 s: an advisor run that waited on the
    request's farm lock would fail (or hang the installation) instead of writing advice."""
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

    app = create_app(settings, session_factory=pooled_session_factory)
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
            assert corrections.view(row) == preview
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
        pytest.param("dose", "2", id="unchanged-dose"),
        pytest.param("dose", "1e1000000", id="past-numeric-storage"),
        pytest.param("product", 2**100, id="unknown-product"),
        pytest.param("catalogue", None, id="product-outside-farm-catalogue"),
        pytest.param("changes", {"parcel_ids": [2**63 - 1]}, id="unknown-parcel"),
        pytest.param("changes", {"notes": None}, id="required-value-cleared"),
        pytest.param(
            "changes",
            {"ts_start": "2024-04-10T05:00:00Z", "ts_end": "2024-04-09T05:00:00Z"},
            id="end-before-start",
        ),
    ],
)
def test_invalid_correction_intent_is_rejected_without_writes(
    correction_case, committed_session_factory, field, value
):
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.corrections import OperationCorrection

    c = correction_case
    dose = c.body["changes"]["quantities"][0]
    if field == "dose":
        dose["value"] = value
    elif field == "product":
        dose["product_id"] = value
    elif field == "catalogue":
        with committed_session_factory() as session:
            farm = session.get(Farm, c.farm_id)
            farm.props = {**farm.props, "product_ids": []}
            session.commit()
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
        from farmcore.core.models.corrections import OperationCorrection

        with committed_session_factory() as session:
            stored = session.get(OperationCorrection, fresh["id"]).before_snapshot
        # The audit keeps every receipt; the reader's view shows only each source's type and time.
        assert fresh["before"]["sources"] == [
            {"source_type": s["source_type"], "received_at": s["received_at"]}
            for s in stored["sources"]
        ]
        machine = [s for s in stored["sources"] if s["source_type"] == "isoxml"]
        assert len(machine) == 2
        assert {s["raw_payload"][spray_weather.NAMESPACE]["status"] for s in machine} == {
            "captured"
        }
        receipts = copy.deepcopy(stored["sources"])
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
        assert "Private correction reason" in final.text
        assert "retain-me" not in final.text and "receipt-7731" not in final.text
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


@pytest.mark.parametrize(
    "case",
    [
        "dose-edit-keeps-treatment-seconds",
        "dose-edit-keeps-dst-fold",
        "dose-edit-keeps-other-product-row",
        "notes-edit-keeps-duplicate-doses",
        "notes-edit-keeps-delisted-product",
        "notes-edit-keeps-huge-historical-dose",
    ],
)
def test_an_edit_changes_only_the_edited_fact(correction_case, committed_session_factory, case):
    """Through the HTML form: untouched doses keep their row, exact value and label, and an
    untouched treatment time keeps its seconds and DST fold."""
    import re
    from datetime import UTC

    from farmcore.apps.operations.corrections import decimal_text
    from farmcore.core.models.canonical import Farm, Log
    from tests.apps.chat.test_handlers import SULPHUR

    c = correction_case
    started = NOW_LOCAL.replace(hour=5, second=37, microsecond=123456)
    if case == "dose-edit-keeps-dst-fold":
        started = started.replace(month=10, day=27, hour=2, minute=30, fold=1)
    with committed_session_factory() as session:
        session.get(Log, c.log_id).ts_start = started
        if case == "dose-edit-keeps-other-product-row":
            session.add(
                Quantity(
                    log_id=c.log_id,
                    product_id=c.ids[SULPHUR],
                    measure="rate",
                    value=Decimal("3"),
                    unit="kg/ha",
                )
            )
        elif case == "notes-edit-keeps-duplicate-doses":
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
        elif case == "notes-edit-keeps-delisted-product":
            farm = session.get(Farm, c.farm_id)
            farm.props = {**farm.props, "product_ids": []}
        elif case == "notes-edit-keeps-huge-historical-dose":
            session.get(Quantity, c.quantity_id).value = Decimal("1e5000")
        session.commit()
        before = [
            (q.id, q.product_id, q.value, q.label)
            for q in session.scalars(
                select(Quantity).where(Quantity.log_id == c.log_id).order_by(Quantity.id)
            )
        ]
    edit = c.browser.get(c.endpoint)
    assert edit.status_code == 200
    if case == "notes-edit-keeps-delisted-product":
        assert f'value="{c.ids[COPPER]}" selected' in edit.text and "Recorded product" in edit.text
    dose_edit = case.startswith("dose-edit")
    values = [decimal_text(value) for _, _, value, _ in before]
    if dose_edit:
        values[0] = "1"
    response = c.browser.post(
        f"/operations/logs/{c.log_id}/corrections/preview?farm={c.farm_id}",
        data={
            "request_key": re.search(r'name="request_key" value="([^"]+)"', edit.text)[1],
            "reason": "One fact changed",
            "parcel_ids": [str(c.ids["P1"])],
            "product_id": [str(product_id) for _, product_id, _, _ in before],
            "dose_value": values,
            "dose_unit": ["kg/ha"] * len(before),
            "ts_start": re.search(r'name="ts_start" value="([^"]+)"', edit.text)[1],
            "notes": "" if dose_edit else "Edited note",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    correction_id = re.search(r"/corrections/(\d+)", response.headers["location"])[1]
    confirm = f"/operations/corrections/{correction_id}/confirm?farm={c.farm_id}"
    assert c.browser.post(confirm).status_code == 200
    with committed_session_factory() as session:
        log = session.get(Log, c.log_id)
        assert log.ts_start.astimezone(UTC) == started.astimezone(UTC)
        after = [
            (q.id, q.product_id, q.value, q.label)
            for q in session.scalars(
                select(Quantity).where(Quantity.log_id == c.log_id).order_by(Quantity.id)
            )
        ]
    if dose_edit:
        untouched = before[1:]
        assert [row for row in after if row in untouched] == untouched
        kept = {row[0] for row in untouched}
        edited = [(product, value) for row_id, product, value, _ in after if row_id not in kept]
        assert edited == [(c.ids[COPPER], Decimal("1"))]
    else:
        assert after == before
