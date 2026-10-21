"""Explicit review mutations are assigned-member authority; never arbitrary farm writes."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.operations.seed import ensure_products
from farmcore.apps.review.models import ParameterVersion
from farmcore.core.models.canonical import Advice, Farm
from farmcore.core.models.identity import FarmMembership
from farmcore.main import create_app
from tests.apps.share.conftest import make_parcel
from tests.core.test_member_sessions import _account, _login


@pytest.mark.parametrize("role", ["owner", "advisor"])
def test_assigned_members_review_with_csrf_but_operation_authority_stays_owner(
    settings, committed_session_factory, role
):
    identity_id, farm_id = _account(committed_session_factory, login="expert", role=role)
    _account(committed_session_factory, login="keeper", farm_id=farm_id)
    _, foreign_farm = _account(committed_session_factory, login="foreign")
    with committed_session_factory.begin() as session:
        product = next(iter(ensure_products(session).values()))
        stock = InventoryStock(
            farm_id=farm_id, product_id=product.id, season=2024, purchased=20, unit="kg"
        )
        session.add(stock)
        session.flush()
        stock_id = stock.id
        parcel = make_parcel(session, session.get(Farm, farm_id), 1)
        saved = Advice(
            farm_id=farm_id,
            parcel_id=parcel.id,
            evaluated_at=datetime(2024, 4, 1, tzinfo=UTC),
            decision="none",
            reasons=[],
            products=[],
            props={"input_snapshot": {"private": "assigned-only"}},
        )
        session.add(saved)
        session.flush()
        evidence_path = f"/advisor/parcels/{parcel.id}/assessments/{saved.id}.json"
    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as browser:
        assert _login(browser, login="expert").status_code == 303
        params = {"farm": farm_id}
        payload = {"overrides": {"downy_rain_min_mm": 12.0}, "evidence": "Assigned expert trial"}
        assert browser.get("/review/parameters.json", params=params).status_code == 200
        exact = browser.get(evidence_path, params=params)
        assert exact.status_code == 200
        assert exact.json()["input_snapshot"] == {"private": "assigned-only"}
        assert browser.get(evidence_path, params={"farm": foreign_farm}).status_code == 403
        assert browser.post("/review/versions", params=params, json=payload).status_code == 403
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }
        assert (
            browser.post(
                "/review/versions",
                params=params,
                json=payload,
                headers={**headers, "Origin": "https://foreign.invalid"},
            ).status_code
            == 403
        )
        result = browser.post("/review/versions", params=params, json=payload, headers=headers)
        assert result.status_code == 201
        version_id = result.json()["id"]
        review = browser.post(
            f"/review/versions/{version_id}/reviews",
            params=params,
            json={"verdict": "unsure", "note": "Needs local evidence", "evidence": "Desk review"},
            headers=headers,
        )
        assert review.status_code == 201
        assert review.json()["reviewer"] == f"user:{identity_id}"
        assert (
            browser.post(
                f"/review/versions/{version_id}/activate", params=params, headers=headers
            ).status_code
            == 409
        )
        denied = browser.get(f"/review/versions/{version_id}.json", params={"farm": foreign_farm})
        assert denied.status_code == 403
        assert "Assigned expert trial" not in denied.text
        arbitrary = browser.post(
            f"/inventory/{stock_id}/low-stock",
            params=params,
            data={"low_stock": "2.5"},
            headers=headers,
            follow_redirects=False,
        )
        assert arbitrary.status_code == (303 if role == "owner" else 403)
        with committed_session_factory.begin() as session:
            membership = session.scalar(
                select(FarmMembership).where(
                    FarmMembership.identity_id == identity_id, FarmMembership.farm_id == farm_id
                )
            )
            membership.revoked_at = datetime.now(UTC)
        assert (
            browser.post(
                "/review/versions", params=params, json=payload, headers=headers
            ).status_code
            == 403
        )
        denied_history = browser.get(evidence_path, params=params)
        assert denied_history.status_code == 403
        assert "assigned-only" not in denied_history.text
        with committed_session_factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ParameterVersion)
                    .where(
                        ParameterVersion.farm_id == farm_id, ParameterVersion.baseline.is_(False)
                    )
                )
                == 1
            )


def test_demo_and_anonymous_cannot_read_review_or_exact_private_evidence(client, session, settings):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Advice, Farm
    from tests.apps.share.conftest import make_parcel

    farm = Farm(name="Private reviewed farm", props={"region": "FRI12"})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    parcel = make_parcel(session, farm, 1)
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=datetime(2024, 4, 1, tzinfo=UTC),
        decision="none",
        reasons=[],
        products=[],
        props={"input_snapshot": {"private": "hidden input"}},
    )
    session.add(row)
    session.flush()
    created = client.post(
        "/review/versions",
        params={"farm": farm.id},
        json={"overrides": {"downy_rain_min_mm": 12.0}, "evidence": "private proposal"},
    )
    assert created.status_code == 201
    version = created.json()["id"]
    for demo_mode in (False, True):
        configured = settings.model_copy(
            update={"demo_mode": demo_mode, "demo_farm_ids": (farm.id,)}
        )
        app = create_app(configured, session_factory=client.app.state.session_factory)
        with TestClient(app) as anonymous:
            for path in (
                "/review/",
                "/review/parameters.json",
                f"/review/versions/{version}.json",
                f"/advisor/parcels/{parcel.id}/assessments/{row.id}.json",
            ):
                response = anonymous.get(path, params={"farm": farm.id})
                assert response.status_code == 403
                assert "private proposal" not in response.text
                assert "hidden input" not in response.text
