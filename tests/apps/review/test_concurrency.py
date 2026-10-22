"""Actual job and HTTP writers share the farm transaction lock, including first evaluation."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from farmcore.apps.advisor.job import evaluate
from farmcore.core.logs import lock_farm
from farmcore.core.manifest import JobContext
from farmcore.core.models.canonical import Advice, Farm, Parcel
from farmcore.main import create_app
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.chat.test_handlers import SQUARE
from tests.core.test_member_sessions import _account, _login

NOW = datetime(2024, 4, 10, 4, tzinfo=UTC)


@pytest.mark.parametrize("writer", ["membership", "activation"])
def test_first_evaluation_waits_for_owner_mutation_transaction(
    committed_session_factory, settings, writer
):
    with committed_session_factory.begin() as setup:
        farm = Farm(name="First evaluation farm", props={"key": "chateau", "region": "FRI12"})
        setup.add(farm)
        setup.flush()
        farm_id = farm.id
        setup.add(Parcel(farm_id=farm_id, alias="P1", name="First parcel", area_ha=1, geom=SQUARE))
        complete_station(setup, farm_id, NOW)
    _account(committed_session_factory, farm_id=farm_id)
    _account(committed_session_factory, login="expert")

    def run_job():
        with committed_session_factory.begin() as session:
            evaluate(JobContext(session, NOW, settings, (farm_id,)))

    def waiters():
        with committed_session_factory() as observer:
            return observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                    "AND wait_event='advisory' AND pid<>pg_backend_pid()"
                )
            )

    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as browser:
        assert _login(browser).status_code == 303
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }
        if writer == "activation":
            proposal = browser.post(
                "/review/versions",
                params={"farm": farm_id},
                headers=headers,
                json={"overrides": {"downy_rain_min_mm": 12.0}, "evidence": "Concurrent trial"},
            )
            assert proposal.status_code == 201
            version = proposal.json()["id"]
            assert (
                browser.post(
                    f"/review/versions/{version}/reviews",
                    params={"farm": farm_id},
                    headers=headers,
                    json={"verdict": "approved", "note": "Concurrent opinion", "evidence": "Desk"},
                ).status_code
                == 201
            )
        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            committed_session_factory() as owner_transaction,
        ):
            lock_farm(owner_transaction, farm_id)
            owner = pool.submit(
                browser.post,
                "/farms/members"
                if writer == "membership"
                else f"/review/versions/{version}/activate",
                params={"farm": farm_id},
                data={"login": "expert", "role": "advisor"} if writer == "membership" else {},
                headers=headers,
                follow_redirects=False,
            )
            job = None
            try:
                deadline = time.monotonic() + 10
                while not waiters():
                    assert time.monotonic() < deadline, "HTTP owner writer did not reach farm lock"
                    time.sleep(0.01)
                job = pool.submit(run_job)
                deadline = time.monotonic() + 10
                while waiters() < 2 and not job.done():
                    assert time.monotonic() < deadline, "Job did not reach shared farm lock"
                    time.sleep(0.01)
                assert not job.done(), "Job committed while the HTTP owner mutation was pending"
                with committed_session_factory() as observer:
                    assert (
                        observer.scalar(
                            select(func.count())
                            .select_from(Advice)
                            .where(Advice.farm_id == farm_id)
                        )
                        == 0
                    )
            finally:
                owner_transaction.rollback()
                owner_response = owner.result(timeout=20)
                if job:
                    job.result(timeout=20)
            assert owner_response.status_code == (303 if writer == "membership" else 201)
    with committed_session_factory() as observer:
        assert (
            observer.scalar(
                select(func.count()).select_from(Advice).where(Advice.farm_id == farm_id)
            )
            == 1
        )


def test_concurrent_activations_have_one_latest_scope_and_complete_history(
    committed_session_factory, settings
):
    from farmcore.apps.review.models import ParameterActivation
    from farmcore.core.access import ensure_owner_grant

    with committed_session_factory.begin() as session:
        farm = Farm(name="Concurrent activation farm", props={"region": "FRI12"})
        session.add(farm)
        session.flush()
        farm_id = farm.id
        ensure_owner_grant(session, farm)
    app = create_app(settings, session_factory=committed_session_factory)
    headers = {"X-Admin-Token": settings.admin_token.get_secret_value()}
    params = {"farm": farm_id}
    versions = []
    with TestClient(app, headers=headers) as browser:
        for threshold in (11.0, 12.0):
            created = browser.post(
                "/review/versions",
                params=params,
                json={
                    "overrides": {"downy_rain_min_mm": threshold},
                    "evidence": "Concurrent trial",
                },
            )
            assert created.status_code == 201
            version = created.json()["id"]
            versions.append(version)
            assert (
                browser.post(
                    f"/review/versions/{version}/reviews",
                    params=params,
                    json={"verdict": "approved", "note": "Bounded trial", "evidence": "Desk"},
                ).status_code
                == 201
            )

    def activate(version):
        with TestClient(app, headers=headers) as browser:
            return browser.post(f"/review/versions/{version}/activate", params=params)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(activate, versions))
    assert [r.status_code for r in responses] == [201, 201]
    with committed_session_factory() as session:
        activations = session.scalars(
            select(ParameterActivation)
            .where(ParameterActivation.farm_id == farm_id)
            .order_by(ParameterActivation.id)
        ).all()
        assert len(activations) == 2
        assert {a.version_id for a in activations} == set(versions)
        latest = activations[-1].version_id
    with TestClient(app, headers=headers) as browser:
        assert (
            browser.get("/review/parameters.json", params=params).json()["version"]["id"] == latest
        )


def test_review_waiting_on_farm_lock_rechecks_membership_revoked_in_that_transaction(
    committed_session_factory, settings
):
    from farmcore.apps.review.models import ParameterVersion
    from farmcore.core.models.identity import FarmMembership

    identity, farm_id = _account(committed_session_factory)
    _account(committed_session_factory, login="remaining-owner", farm_id=farm_id)
    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as browser:
        assert _login(browser).status_code == 303
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }
        with ThreadPoolExecutor(max_workers=1) as pool, committed_session_factory() as revoker:
            lock_farm(revoker, farm_id)
            pending = pool.submit(
                browser.post,
                "/review/versions",
                params={"farm": farm_id},
                headers=headers,
                json={
                    "overrides": {"downy_rain_min_mm": 12.0},
                    "evidence": "Stale principal trial",
                },
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    with committed_session_factory() as observer:
                        waiting = observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND wait_event='advisory' AND pid<>pg_backend_pid()"
                            )
                        )
                    if waiting:
                        break
                    assert time.monotonic() < deadline, "Review did not reach farm lock"
                    time.sleep(0.01)
                membership = revoker.scalar(
                    select(FarmMembership).where(
                        FarmMembership.identity_id == identity,
                        FarmMembership.farm_id == farm_id,
                    )
                )
                membership.revoked_at = datetime.now(UTC)
                revoker.commit()
            finally:
                revoker.rollback()
                response = pending.result(timeout=20)
        assert response.status_code == 403
        assert response.json()["detail"] == "Assigned owner or advisor required"
        with committed_session_factory() as observer:
            assert (
                observer.scalar(
                    select(func.count())
                    .select_from(ParameterVersion)
                    .where(
                        ParameterVersion.farm_id == farm_id,
                    )
                )
                == 0
            )
