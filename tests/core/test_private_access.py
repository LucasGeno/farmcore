"""Private HTTP boundary: anonymous, operator and explicit showcase are distinct."""

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from farmcore.main import create_app


@pytest.mark.parametrize(
    "path",
    [
        "/today/",
        "/fields/parcels.geojson",
        "/operations/register.json",
        "/farms/switcher",
        "/assistant/ask?text=hello",
        "/tour",
        "/clock",
        "/share/as/1/register.json",
    ],
)
@pytest.mark.parametrize("tour", [False, True])
def test_private_default_rejects_anonymous_even_without_token(
    settings, session_factory, path, tour
):
    private = settings.model_copy(update={"admin_token": None})
    with TestClient(create_app(private, session_factory=session_factory)) as client:
        if tour:
            client.cookies.set("farmcore_tour", "1")
        client.cookies.set("farmcore_farm", "1")
        response = client.get(path)
    assert response.status_code == 403
    assert response.json() == {"detail": "Authentication required."}


def test_explicit_demo_never_exposes_undesignated_farms(settings, session_factory, session):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm
    from farmcore.core.seed import ensure_default_farm

    demo = ensure_default_farm(session, settings)
    private = Farm(name="Private orchard secret")
    session.add(private)
    session.flush()
    ensure_owner_grant(session, private)
    session.commit()
    showcase = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [demo.id]})
    with TestClient(create_app(showcase, session_factory=session_factory)) as client:
        assert client.get("/fields/parcels.geojson").status_code == 200
        response = client.get("/farms/switcher")
        assert response.status_code == 200
        assert "Private orchard secret" not in response.text
        for path in [f"/fields/?farm={private.id}", f"/farms/select?farm={private.id}"]:
            assert client.get(path).status_code == 403
        client.cookies.set("farmcore_farm", str(private.id))
        assert client.get("/tour").status_code == 403


def test_demo_cannot_read_private_grant_or_global_audit(settings, session_factory, session):
    from farmcore.core.models.canonical import DataGrant, Farm, LlmAudit
    from farmcore.core.seed import ensure_default_farm

    demo = ensure_default_farm(session, settings)
    private = Farm(name="Secret farm")
    session.add(private)
    session.flush()
    grant = DataGrant(
        farm_id=private.id,
        grantee="secret adviser",
        scopes=["register:read", "audit:read"],
        purpose="secret",
    )
    session.add_all(
        [
            grant,
            LlmAudit(
                adapter="private-adapter",
                model="secret-model",
                endpoint="secret",
                request_body={},
                status="ok",
            ),
        ]
    )
    session.commit()
    showcase = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [demo.id]})
    with TestClient(create_app(showcase, session_factory=session_factory)) as client:
        for path in [f"/share/as/{grant.id}/", f"/share/as/{grant.id}/audit.json", "/chat/"]:
            response = client.get(path)
            assert response.status_code == (403 if path == "/chat/" else 404)
            assert "secret" not in response.text


def test_operator_and_public_bootstrap_remain_usable(settings, session_factory, session):
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    with TestClient(create_app(settings, session_factory=session_factory)) as client:
        assert client.get("/health").json() == {"db": "ok"}
        assert client.get("/admin/sign-in").status_code == 200
        assert client.get("/static/core/navigation.js").status_code == 200
        assert client.get("/today/").status_code == 403
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert client.get("/today/").status_code == 200


def test_demo_without_designation_is_closed(settings, session_factory, session):
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    showcase = settings.model_copy(update={"demo_mode": True})
    with TestClient(create_app(showcase, session_factory=session_factory)) as client:
        assert client.get("/today/").status_code == 403
        assert client.post("/clock/step", data={"to": "2024-04-10T06:00"}).status_code == 403


def test_hostile_selector_sweep_never_authorizes_or_audits_private_reads(
    settings, session_factory, session
):
    from sqlalchemy import func, select

    from farmcore.core.models.canonical import AccessLog
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    before = session.scalar(select(func.count()).select_from(AccessLog))
    selectors = ["-1", "0", "1", "999999999", str(2**100), "owner", "../1", "1 OR 1=1"]
    with TestClient(create_app(settings, session_factory=session_factory)) as client:
        for selector in selectors:
            client.cookies.set("farmcore_farm", selector)
            client.cookies.set("admin_token", selector)
            client.headers["X-Admin-Token"] = selector
            for path in ["/today/", "/fields/parcels.geojson", "/assistant/ask"]:
                response = client.get(path, params={"farm": selector, "text": "secret"})
                assert response.status_code == 403
                assert response.json() == {"detail": "Authentication required."}
    assert session.scalar(select(func.count()).select_from(AccessLog)) == before


def test_operator_keeps_read_authority_in_read_only_tour(settings, session_factory, session):
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    with TestClient(create_app(settings, session_factory=session_factory)) as client:
        client.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        assert client.get("/tour").status_code == 200
        assert client.get("/today/").status_code == 200
        assert client.post("/clock/step").status_code == 403


def test_demo_rejects_unknown_selectors_without_a_farm_existence_oracle(
    settings, session_factory, session
):
    from farmcore.core.seed import ensure_default_farm

    demo = ensure_default_farm(session, settings)
    session.commit()
    showcase = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [demo.id]})
    with TestClient(create_app(showcase, session_factory=session_factory)) as client:
        for selector in ["-1", "0", "999999999", str(2**100), "owner", "1 OR 1=1"]:
            response = client.get("/today/", params={"farm": selector})
            assert response.status_code == 403
            assert response.json() == {"detail": "Authentication required."}


def test_demo_audit_grant_never_queries_global_audit(settings, session_factory, session):
    from sqlalchemy import select

    from farmcore.core.models.canonical import AccessLog, DataGrant
    from farmcore.core.seed import ensure_default_farm

    demo = ensure_default_farm(session, settings)
    grant = DataGrant(
        farm_id=demo.id, grantee="demo adviser", scopes=["audit:read"], purpose="showcase"
    )
    session.add(grant)
    session.commit()
    showcase = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [demo.id]})
    with TestClient(create_app(showcase, session_factory=session_factory)) as client:
        assert client.get(f"/share/as/{grant.id}/").status_code == 200
        assert client.get(f"/share/as/{grant.id}/audit.json").status_code == 403
    assert session.scalar(select(AccessLog).where(AccessLog.resource == "llm_audit")) is None


# Each example shares the function-scoped rollback connection intentionally: setup is
# idempotent, all requests must deny, and the audit count is checked after every example.
@hypothesis_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_generated_selectors_never_authorize_private_or_foreign_demo_reads(
    settings, session_factory, session, data
):
    from sqlalchemy import func, select

    from farmcore.core.models.canonical import AccessLog
    from farmcore.core.seed import ensure_default_farm

    demo = ensure_default_farm(session, settings)
    session.commit()
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_+./:%?="
    selector = data.draw(st.text(alphabet=alphabet, min_size=1, max_size=64), label="selector")
    forged_token = "forged-" + data.draw(st.text(alphabet=alphabet, max_size=64), label="token")
    tour = data.draw(st.booleans(), label="tour")
    # Construct foreign selectors without rejection/filtering; neither branch can
    # equal the designated positive demo ID, including during shrinking.
    foreign = data.draw(
        st.one_of(
            st.integers(min_value=1, max_value=2**100).map(lambda n: str(demo.id + n)),
            st.text(alphabet=alphabet, max_size=64).map(lambda text: "x" + text),
        ),
        label="foreign demo selector",
    )
    before = session.scalar(select(func.count()).select_from(AccessLog))
    for configured, farm_selector in [
        (settings, selector),
        (settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [demo.id]}), foreign),
    ]:
        with TestClient(create_app(configured, session_factory=session_factory)) as client:
            client.headers["X-Admin-Token"] = forged_token
            client.cookies.set("admin_token", forged_token)
            client.cookies.set("farmcore_farm", farm_selector)
            if tour:
                client.cookies.set("farmcore_tour", "1")
            for path in ["/today/", "/fields/parcels.geojson", "/assistant/ask"]:
                response = client.get(path, params={"farm": farm_selector, "text": "hello"})
                assert response.status_code == 403
                assert response.json() == {"detail": "Authentication required."}
    assert session.scalar(select(func.count()).select_from(AccessLog)) == before
