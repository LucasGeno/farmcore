"""ScopedRepository: farm isolation, grant scopes, one access_log row per read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import DependencyScopeError
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from farmcore.core.access import (
    FarmNotSeeded,
    Repo,
    ScopeDenied,
    ScopedRepository,
    default_farm,
    ensure_owner_grant,
    get_repo,
    repo_for_job,
)
from farmcore.core.models.canonical import (
    AccessLog,
    Advice,
    Asset,
    DataGrant,
    Event,
    Farm,
    LlmAudit,
    Log,
    LogSource,
    Observation,
    Parcel,
    Product,
    Quantity,
)
from farmcore.core.seed import ensure_default_farm

T0 = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
# Synthetic square near Saint-Emilion.
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"


def _farm_with_data(session, name: str) -> Farm:
    farm = Farm(name=name)
    session.add(farm)
    session.flush()
    parcel = Parcel(
        farm_id=farm.id, name=f"{name} P1", alias="P1", area_ha=Decimal("1.2"), geom=SQUARE
    )
    station = Asset(farm_id=farm.id, kind="station", name=f"{name} station")
    session.add_all([parcel, station])
    session.flush()
    log = Log(farm_id=farm.id, type="spray", status="done", name="spray", ts_start=T0)
    log.parcels.append(parcel)
    log.quantities.append(Quantity(measure="rate", value=Decimal("2"), unit="kg/ha"))
    log.sources.append(LogSource(source_type="seed", external_key=name, precision_rank=5))
    session.add(log)
    session.add(Observation(asset_id=station.id, ts=T0, variable="t_c", value=11.5))
    session.flush()
    return farm


def _grant(session, farm: Farm, scopes: list[str]) -> DataGrant:
    grant = DataGrant(farm_id=farm.id, grantee="co-op adviser", scopes=scopes, purpose="advice")
    session.add(grant)
    session.flush()
    return grant


def _owner_repo(session, farm: Farm, actor: str = "owner") -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), actor, "test")


def _access_rows(session) -> list[AccessLog]:
    return list(session.scalars(select(AccessLog).order_by(AccessLog.id)))


def test_other_farms_parcels_are_invisible(session):
    farm_a = _farm_with_data(session, "A")
    farm_b = _farm_with_data(session, "B")
    repo_a = _owner_repo(session, farm_a)
    b_parcel_id = session.scalar(select(Parcel.id).where(Parcel.farm_id == farm_b.id))

    parcels = repo_a.list(Parcel, scope="fields:read")

    assert [p.farm_id for p in parcels] == [farm_a.id]
    assert repo_a.get(Parcel, b_parcel_id, scope="fields:read") is None


@pytest.mark.parametrize(
    ("model", "scope"),
    [
        (Log, "logs:read"),
        (Quantity, "logs:read"),
        (LogSource, "logs:read"),
        (Observation, "stations:read"),
        (Asset, "stations:read"),
        (Farm, "fields:read"),
    ],
)
def test_rows_reached_via_log_or_asset_are_filtered_by_farm(session, model, scope):
    farm_a = _farm_with_data(session, "A")
    _farm_with_data(session, "B")

    rows = _owner_repo(session, farm_a).list(model, scope=scope)

    assert len(rows) == 1


def test_logs_read_grant_reads_logs_and_is_denied_observations(session):
    farm = _farm_with_data(session, "A")
    repo = ScopedRepository(
        session, farm.id, _grant(session, farm, ["logs:read"]), "adviser", "advice"
    )

    assert len(repo.list(Log, scope="logs:read")) == 1
    with pytest.raises(ScopeDenied):
        repo.list(Observation, scope="stations:read")


@pytest.mark.parametrize(
    ("model", "scope"),
    [
        (Observation, "logs:read"),
        (Asset, "logs:read"),
        (Parcel, "stations:read"),
        (Log, "stations:read"),
        (Advice, "logs:read"),
        (AccessLog, "logs:read"),
    ],
)
def test_a_scope_reads_only_its_own_resources(session, model, scope):
    farm = _farm_with_data(session, "A")
    adviser = ScopedRepository(session, farm.id, _grant(session, farm, [scope]), "adviser", "x")

    with pytest.raises(ScopeDenied, match="does not cover"):
        adviser.list(model, scope=scope)
    # The owner grant (`*`) does not widen what a scope covers either.
    with pytest.raises(ScopeDenied, match="does not cover"):
        _owner_repo(session, farm).list(model, scope=scope)
    assert _access_rows(session) == []


@pytest.mark.parametrize(
    ("model", "scope"),
    [
        (Parcel, "fields:read"),
        (Parcel, "register:read"),
        (Log, "register:read"),
        (Quantity, "register:read"),
        (Event, "logs:read"),
        (Event, "advice:read"),
        (Advice, "advice:read"),
        (Product, "register:read"),
        (Farm, "stations:read"),
    ],
)
def test_a_scope_reads_the_resources_it_covers(session, model, scope):
    farm = _farm_with_data(session, "A")

    _owner_repo(session, farm).list(model, scope=scope)

    assert [(r.resource, r.scope) for r in _access_rows(session)] == [(model.__table__.name, scope)]


def test_execute_checks_a_known_resource_against_the_scope(session):
    farm = _farm_with_data(session, "A")
    repo = _owner_repo(session, farm)

    with pytest.raises(ScopeDenied, match="does not cover"):
        repo.execute(select(LlmAudit), scope="logs:read", resource="llm_audit")


def test_grant_that_starts_in_the_future_is_denied(session):
    farm = _farm_with_data(session, "A")
    grant = _grant(session, farm, ["logs:read"])
    grant.valid_from = datetime.now(UTC) + timedelta(days=1)

    with pytest.raises(ScopeDenied, match="not valid yet"):
        ScopedRepository(session, farm.id, grant, "adviser", "x").list(Log, scope="logs:read")


def test_each_read_writes_exactly_one_access_log_row(session):
    farm = _farm_with_data(session, "A")
    grant = ensure_owner_grant(session, farm)
    repo = ScopedRepository(session, farm.id, grant, "owner", "web")
    log_id = session.scalar(select(Log.id))

    repo.list(Log, Log.type == "spray", scope="logs:read", order_by=Log.ts_start.desc(), limit=5)
    repo.get(Log, log_id, scope="logs:read")
    repo.execute(select(func.count()).select_from(Log), scope="logs:read", resource="log")

    rows = _access_rows(session)
    assert [(r.actor, r.farm_id, r.grant_id, r.resource, r.scope, r.purpose) for r in rows] == [
        ("owner", farm.id, grant.id, "log", "logs:read", "web"),
    ] * 3


def test_denied_read_writes_no_access_log_row(session):
    farm = _farm_with_data(session, "A")
    repo = ScopedRepository(session, farm.id, _grant(session, farm, ["logs:read"]), "adviser", "x")

    with pytest.raises(ScopeDenied):
        repo.list(Parcel, scope="fields:read")

    assert _access_rows(session) == []


def test_grant_of_another_farm_or_revoked_grant_is_denied(session):
    farm_a = _farm_with_data(session, "A")
    farm_b = _farm_with_data(session, "B")
    foreign = ensure_owner_grant(session, farm_b)
    revoked = _grant(session, farm_a, ["logs:read"])
    revoked.revoked_at = T0

    with pytest.raises(ScopeDenied):
        ScopedRepository(session, farm_a.id, foreign, "owner", "x").list(Log, scope="logs:read")
    with pytest.raises(ScopeDenied):
        ScopedRepository(session, farm_a.id, revoked, "adviser", "x").list(Log, scope="logs:read")


def test_unknown_scope_is_rejected(session):
    farm = _farm_with_data(session, "A")

    with pytest.raises(ValueError, match="unknown scope"):
        _owner_repo(session, farm).list(Log, scope="logs:raed")


def test_llm_audit_is_read_only_through_execute_with_audit_scope(session):
    farm = _farm_with_data(session, "A")
    session.add(
        LlmAudit(
            adapter="fake", model="m", endpoint="e", request_body={"transcript": "x"}, status="ok"
        )
    )
    session.flush()
    repo = _owner_repo(session, farm)

    rows = repo.execute(select(LlmAudit), scope="audit:read", resource="llm_audit").scalars().all()

    assert len(rows) == 1
    assert [(r.resource, r.scope) for r in _access_rows(session)] == [("llm_audit", "audit:read")]
    with pytest.raises(TypeError, match="no farm path"):
        repo.list(LlmAudit, scope="audit:read")
    adviser = ScopedRepository(
        session, farm.id, _grant(session, farm, ["logs:read"]), "adviser", "x"
    )
    with pytest.raises(ScopeDenied):
        adviser.execute(select(LlmAudit), scope="audit:read", resource="llm_audit")


def test_repo_for_job_logs_reads_as_the_job(session):
    farm = _farm_with_data(session, "A")

    repo_for_job(session, farm.id, "advisor.evaluate").list(Parcel, scope="fields:read")

    [row] = _access_rows(session)
    assert (row.actor, row.purpose, row.farm_id) == ("job:advisor.evaluate", "job", farm.id)


def test_ensure_owner_grant_is_idempotent(session):
    farm = _farm_with_data(session, "A")

    g1 = ensure_owner_grant(session, farm)
    g2 = ensure_owner_grant(session, farm)

    assert g1.id == g2.id
    assert (g1.grantee, g1.scopes) == ("owner", ["*"])


def test_default_farm_needs_the_seed(session, settings):
    with pytest.raises(FarmNotSeeded):
        default_farm(session, settings)

    farm = ensure_default_farm(session, settings)

    assert default_farm(session, settings).id == farm.id


def _probe_app(settings, session_factory) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory

    @app.get("/probe")
    def probe(repo: Repo) -> dict[str, int]:
        return {"parcels": len(repo.list(Parcel, scope="fields:read"))}

    return app


def test_get_repo_reads_the_default_farm_as_owner_and_keeps_the_access_log(
    settings, session_factory
):
    with session_factory() as s:
        farm = ensure_default_farm(s, settings)
        s.commit()

    with TestClient(_probe_app(settings, session_factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        response = client.get("/probe")

    assert response.status_code == 200
    assert response.json() == {"parcels": 0}
    with session_factory() as s:
        rows = _access_rows(s)
    assert [(r.actor, r.farm_id, r.resource, r.scope) for r in rows] == [
        ("owner", farm.id, "parcel", "fields:read")
    ]


def test_get_repo_without_seed_is_503(settings, session_factory):
    with TestClient(_probe_app(settings, session_factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        response = client.get("/probe")

    assert response.status_code == 503
    assert "farmcore seed" in response.json()["detail"]


def test_get_repo_commits_before_the_response_is_sent(settings, session_factory):
    with session_factory() as s:
        ensure_default_farm(s, settings)
        s.commit()
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    order: list[str] = []
    event.listen(session_factory, "after_commit", lambda _s: order.append("commit"))

    @app.get("/stream")
    def stream(repo: Repo) -> StreamingResponse:
        repo.list(Parcel, scope="fields:read")

        def body():
            order.append("send")  # runs while the response is being sent
            yield b"ok"

        return StreamingResponse(body())

    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert client.get("/stream").status_code == 200

    assert order == ["commit", "send"]


def test_get_repo_must_be_function_scoped():
    app = FastAPI()

    with pytest.raises(DependencyScopeError):

        @app.get("/wrong")
        def wrong(repo: ScopedRepository = Depends(get_repo)) -> None:
            return None
