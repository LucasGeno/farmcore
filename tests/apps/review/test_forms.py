"""Expert forms exercise typed values, explicit opinions, CSRF and immutable children."""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from farmcore.apps.review.models import ParameterActivation, ParameterReview, ParameterVersion
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Farm
from farmcore.main import create_app
from tests.core.test_member_sessions import _account, _login


def test_cookie_forms_build_typed_children_and_require_explicit_approved_activation(
    committed_session_factory, settings
):
    identity, farm = _account(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    params = {"farm": farm}
    with TestClient(app, base_url="https://testserver") as browser:
        assert _login(browser).status_code == 303
        page = browser.get("/review/", params=params)
        assert page.status_code == 200
        csrf = browser.cookies.get("farmcore_csrf")
        assert f'value="{csrf}"' in page.text
        headers = {"Origin": "https://testserver"}

        def post(path, data):
            return browser.post(
                path,
                params=params,
                data={"csrf_token": csrf, **data},
                headers=headers,
                follow_redirects=False,
            )

        proposal = {"parameter": "downy_rain_min_mm", "value": "12.5", "evidence": "Local trial"}
        denied = browser.post("/review/propose-form", params=params, data=proposal, headers=headers)
        assert denied.status_code == 403
        for invalid in ("nan", "1.5"):
            rejected = post(
                "/review/propose-form",
                {"parameter": "downy_window_h", "value": invalid, "evidence": "Typo"},
            )
            assert rejected.status_code == 303 and "error=" in rejected.headers["location"]
            assert 'role="alert"' in browser.get(rejected.headers["location"]).text
        versions = []
        for key, raw in [
            ("downy_rain_min_mm", "12.5"),
            ("downy_window_h", "72"),
            ("season_gate", "2024-04-06"),
        ]:
            data = {"parameter": key, "value": raw, "evidence": f"Trial {key}"}
            if versions:
                data["parent_version_id"] = versions[-1]
            response = post("/review/propose-form", data)
            assert response.status_code == 303
            version = int(parse_qs(urlsplit(response.headers["location"]).query)["version"][0])
            detail = browser.get(f"/review/versions/{version}.json", params=params).json()
            versions.append(version)
            assert detail["status"] == "unreviewed"
            assert detail["effective_config"]["downy_rain_min_mm"]["value"] == 12.5
        detail = browser.get(f"/review/versions/{versions[-1]}.json", params=params).json()
        assert detail["effective_config"]["downy_window_h"]["value"] == 72
        assert type(detail["effective_config"]["downy_window_h"]["value"]) is int
        assert detail["effective_config"]["season_gate"]["value"] == "2024-04-06"
        parent = browser.get(f"/review/versions/{versions[0]}.json", params=params).json()
        assert parent["effective_config"]["downy_window_h"]["value"] == 48
        assert parent["effective_config"]["season_gate"]["value"] == "2024-04-05"
        version = versions[-1]
        activate = f"/review/versions/{version}/activate-form"
        assert "error=" in post(activate, {}).headers["location"]
        opinion_path = f"/review/versions/{version}/review-form"
        invalid = post(opinion_path, {"verdict": "maybe", "note": "?", "evidence": "Desk"})
        assert invalid.status_code == 303 and "error=" in invalid.headers["location"]
        blank = post(opinion_path, {"verdict": "approved", "note": "", "evidence": ""})
        assert blank.status_code == 303 and "error=" in blank.headers["location"]
        for verdict in ("unsure", "rejected", "approved"):
            response = post(
                opinion_path,
                {
                    "verdict": verdict,
                    "note": f"Human {verdict}",
                    "evidence": "Desk opinion, not accuracy proof",
                },
            )
            assert response.status_code == 303 and "error=" not in response.headers["location"]
            result = post(activate, {})
            assert result.status_code == 303
            assert ("error=" not in result.headers["location"]) is (verdict == "approved")
        current = browser.get("/review/parameters.json", params=params).json()
        assert current["version"]["id"] == version and current["accuracy_claim"] is False
        history = browser.get(f"/review/versions/{version}.json", params=params).json()
        assert [r["verdict"] for r in history["reviews"]] == ["unsure", "rejected", "approved"]
        assert {r["reviewer"] for r in history["reviews"]} == {f"user:{identity}"}
        assert len(history["activations"]) == 1
        for path in (
            "/review/versions/9223372036854775807/review-form",
            "/review/versions/9223372036854775807/activate-form",
        ):
            assert (
                post(path, {"verdict": "approved", "note": "Valid", "evidence": "Desk"}).status_code
                == 404
            )
    with committed_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ParameterVersion)) == 4
        assert session.scalar(select(func.count()).select_from(ParameterReview)) == 3
        assert session.scalar(select(func.count()).select_from(ParameterActivation)) == 1


@pytest.mark.parametrize("scope_change", ["year", "region"])
def test_scope_change_prevents_activation_and_parent_inheritance(
    client, session, settings, scope_change
):
    from datetime import UTC, datetime

    from farmcore.core.access import ensure_owner_grant

    farm = Farm(name="Scope-changed farm", props={"region": "FRI12"})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    params = {"farm": farm.id}
    body = {"overrides": {"downy_rain_min_mm": 12.0}, "evidence": "Scope trial"}
    result = client.post("/review/versions", params=params, json=body)
    assert result.status_code == 201
    version = result.json()["id"]
    assert (
        client.post(
            f"/review/versions/{version}/reviews",
            params=params,
            json={"verdict": "approved", "note": "Opinion", "evidence": "Desk"},
        ).status_code
        == 201
    )
    if scope_change == "year":
        set_replay(session, datetime(2025, 4, 9, tzinfo=UTC), settings)
    else:
        session.get(Farm, farm.id).props = {"region": "ES418"}
    session.commit()
    assert client.post(f"/review/versions/{version}/activate", params=params).status_code == 409
    child = client.post(
        "/review/versions", params=params, json={**body, "parent_version_id": version}
    )
    assert child.status_code == 422
    assert child.json()["detail"] == "Parent version is unavailable for this scope"
    for opinion in (
        {"verdict": "approved", "note": " ", "evidence": "Desk"},
        {"verdict": "approved", "note": "Opinion", "evidence": " "},
    ):
        assert (
            client.post(
                f"/review/versions/{version}/reviews", params=params, json=opinion
            ).status_code
            == 422
        )
