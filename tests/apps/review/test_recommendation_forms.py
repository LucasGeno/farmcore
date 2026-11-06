"""The advice card's form path: owner asks, assigned advisor answers, both with CSRF."""

import re

from fastapi.testclient import TestClient

from farmcore.main import create_app
from tests.apps.review.test_recommendation_review import _farm_with_assessment
from tests.core.test_member_sessions import _login


def _csrf(browser):
    return browser.cookies.get("farmcore_csrf")


def _post(browser, path, data):
    return browser.post(
        path,
        data=data,
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )


def test_owner_requests_from_the_card_and_advisor_answers_from_the_list(
    committed_session_factory, settings
):
    farm_id, parcel_id, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        params = {"farm": farm_id}
        card = owner.get(f"/advisor/parcels/{parcel_id}", params=params)
        assert card.status_code == 200
        assert f"/review/assessments/{advice_id}/card" in card.text
        # No-JS fallback: the fragment is hidden and a plain link remains.
        assert "fc-script-only { display: none; }" in card.text
        # A full page with the same request form.
        assert f'href="/review/assessments/{advice_id}?farm={farm_id}"' in card.text
        full = owner.get(f"/review/assessments/{advice_id}", params=params)
        assert full.status_code == 200
        assert f'action="/review/assessments/{advice_id}/request-form?farm={farm_id}"' in (
            full.text.replace("&amp;", "&")
        )
        check = owner.get(f"/review/assessments/{advice_id}/card", params=params)
        assert check.status_code == 200
        assert f'action="/review/assessments/{advice_id}/request-form?farm={farm_id}"' in (
            check.text.replace("&amp;", "&")
        )
        assert 'name="csrf_token"' in check.text
        asked = _post(
            owner,
            f"/review/assessments/{advice_id}/request-form",
            {"note": "Please check the window", "csrf_token": _csrf(owner)},
        )
        assert asked.status_code == 303
        assert "requested=" in asked.headers["location"]
        page = advisor.get("/review/recommendations/", params=params)
        assert "Please check the window" in page.text
        request_id = int(re.search(r"/review/requests/(\d+)/response-form", page.text).group(1))
        assert 'name="csrf_token"' in page.text
        answered = _post(
            advisor,
            f"/review/requests/{request_id}/response-form",
            {
                "opinion": "disagree",
                "note": "The window is too early",
                "csrf_token": _csrf(advisor),
            },
        )
        assert answered.status_code == 303
        final = owner.get(f"/review/assessments/{advice_id}/card", params=params)
        assert "expert" in final.text
        assert "The window is too early" in final.text
        assert "Disagrees with this saved check" in final.text
        assert f'action="/review/assessments/{advice_id}/request-form' not in final.text


def test_advisor_view_has_no_request_form_and_anonymous_view_has_no_check_block(
    committed_session_factory, settings
):
    farm_id, parcel_id, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as advisor,
        TestClient(app, base_url="https://testserver") as visitor,
    ):
        assert _login(advisor, login="expert").status_code == 303
        params = {"farm": farm_id}
        check = advisor.get(f"/review/assessments/{advice_id}/card", params=params)
        assert check.status_code == 200
        assert "request-form" not in check.text
        assert "No advisor check requested" in check.text
        card = visitor.get(f"/advisor/parcels/{parcel_id}", params=params)
        assert card.status_code in (403, 200)
        assert "/review/assessments/" not in card.text
