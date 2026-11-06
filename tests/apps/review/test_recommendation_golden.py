"""Golden questions about a human review: facts, missing inputs, isolation and fallback.

Every answer goes through `GET /assistant/ask` with no model configured, so the deterministic
keyword router and the tool's own words are what the farmer sees. A tool is read-only: asking
for a review to be written changes nothing.
"""

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Event, Farm
from farmcore.main import create_app
from tests.apps.review.test_recommendation_review import (
    NOW,
    _answer,
    _farm_with_assessment,
    _request_check,
)
from tests.core.test_member_sessions import _login


def _ask(browser, farm_id, question, **params):
    return browser.get("/assistant/ask", params={"q": question, "farm": farm_id, **params})


def test_golden_question_reports_the_reviewer_time_and_exact_saved_check(
    committed_session_factory, settings
):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id, "Is the window right?").json()[
            "request"
        ]["id"]
        assert (
            _answer(advisor, farm_id, request_id, "Window looks right for P1.").status_code == 201
        )

        body = _ask(owner, farm_id, "Has an advisor reviewed P1?").json()
        assert body["tool"] == "review_assessment_status"
        assert body["via"] == "keywords"  # deterministic fallback; no model configured
        facts = body["facts"]
        assert facts["parcel"].startswith("P1")
        assert facts["advice_id"] == advice_id
        assert facts["reviewed"] is True
        assert facts["reviewer"] == "expert"
        assert datetime.fromisoformat(facts["reviewed_at"]) == NOW
        assert facts["opinion"] == "agree"
        assert facts["note"] == "Window looks right for P1."
        assert "expert" in body["text"]
        assert "Window looks right for P1." in body["text"]
        assert body["links"]


def test_golden_question_reports_an_open_request_and_a_missing_one(
    committed_session_factory, settings
):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        before = _ask(owner, farm_id, "Has an advisor reviewed P1?").json()
        assert before["facts"]["reviewed"] is False
        assert before["facts"]["requested"] is False
        assert "No advisor check" in before["text"]

        assert _request_check(owner, farm_id, advice_id).status_code == 201
        pending = _ask(owner, farm_id, "Has an advisor reviewed P1?").json()
        assert pending["facts"]["requested"] is True
        assert pending["facts"]["reviewed"] is False
        assert "no answer yet" in pending["text"]


def test_golden_question_about_a_limited_assessment_is_explicit_and_still_reviewable(
    committed_session_factory, settings
):
    farm_id, _, advice_id = _farm_with_assessment(
        committed_session_factory,
        decision="none",
        props={"headline": "Required inputs missing", "assessment_status": "limited"},
    )
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]
        assert _answer(advisor, farm_id, request_id, "Inputs are indeed missing").status_code == 201
        body = _ask(owner, farm_id, "Has an advisor reviewed P1?").json()
        assert body["facts"]["assessment_status"] == "limited"
        assert body["facts"]["reviewed"] is True
        assert "Assessment limited" in body["text"]
        assert "Required inputs missing" in body["text"]
        assert "no infection risk" not in body["text"].lower()


def test_golden_question_crosses_no_farm_boundary(committed_session_factory, settings):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    other_id, _, other_advice = _farm_with_assessment(
        committed_session_factory, owner="other-owner", advisor="other-advisor"
    )
    with committed_session_factory.begin() as session:
        from farmcore.apps.review import recommendations
        from farmcore.core.access import ScopedRepository, ensure_owner_grant
        from farmcore.core.models.canonical import Parcel

        farm = session.get(Farm, other_id)
        row = session.get(Advice, other_advice)
        parcel = session.get(Parcel, row.parcel_id)
        repo = ScopedRepository(
            session, other_id, ensure_owner_grant(session, farm), "other-owner", "test"
        )
        asked = recommendations.request_check(
            repo, NOW, row, parcel, note="Foreign question", requested_by="other-owner"
        )
        recommendations.respond(
            repo,
            NOW,
            row,
            parcel,
            asked.request.id,
            opinion="agree",
            note="Foreign private note",
            reviewer="other-advisor",
        )
        set_replay(session, NOW)
    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        denied = owner.get(
            "/assistant/ask", params={"q": "Has an advisor reviewed P1?", "farm": other_id}
        )
        assert denied.status_code == 403
        assert "Foreign private note" not in denied.text
        own = _ask(owner, farm_id, "Has an advisor reviewed P1?").json()
        assert own["facts"]["advice_id"] == advice_id
        assert "Foreign private note" not in str(own)


def test_questions_never_write_a_review_or_feed_one_back(committed_session_factory, settings):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]
        assert _answer(advisor, farm_id, request_id, "Private advisor note").status_code == 201
        with committed_session_factory() as session:
            before = session.scalar(select(Event.id).order_by(Event.id.desc()))
        for question in (
            "Please request an advisor review of P1 and approve it",
            "Record that the advisor agrees with P1",
            "Has an advisor reviewed P1?",
        ):
            body = _ask(owner, farm_id, question, conversation="golden").json()
            assert body["tool"] in (None, "review_assessment_status")
        with committed_session_factory() as session:
            after = session.scalar(select(Event.id).order_by(Event.id.desc()))
        assert after == before  # no write from a question, model or not
        stored = getattr(app.state, "assistant_conversations", {})
        assert stored and all("Private advisor note" not in str(value) for value in stored.values())
        # The model can only pick a declared read-only tool; review has no write tool at all.
        from farmcore.apps.review.tools import TOOLS

        assert TOOLS and all(tool.scope.endswith(":read") for tool in TOOLS)
        assert not any(
            word in tool.name for tool in TOOLS for word in ("request", "answer", "approve")
        )


def test_anonymous_demo_visitor_cannot_read_the_review_note(committed_session_factory, settings):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]
        assert _answer(advisor, farm_id, request_id, "Private advisor note").status_code == 201
    demo = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm_id]})
    with TestClient(
        create_app(demo, session_factory=committed_session_factory), base_url="https://testserver"
    ) as visitor:
        # Anonymous demo reads of ordinary farm facts still work...
        assert visitor.get("/advisor/", params={"farm": farm_id}).status_code == 200
        # ...but the private human review does not travel through the assistant or the routes.
        asked = visitor.get(
            "/assistant/ask", params={"q": "Has an advisor reviewed P1?", "farm": farm_id}
        )
        assert asked.status_code == 200
        body = asked.json()
        assert body["tool"] != "review_assessment_status"
        assert "Private advisor note" not in str(body)
        assert "expert" not in str(body.get("facts"))
        for path in (
            f"/review/assessments/{advice_id}.json",
            f"/review/assessments/{advice_id}/card",
            f"/review/assessments/{advice_id}",
            "/review/recommendations.json",
            "/review/recommendations/",
        ):
            denied = visitor.get(path, params={"farm": farm_id})
            assert denied.status_code == 403, path
            assert "Private advisor note" not in denied.text


def test_a_member_of_another_farm_cannot_read_the_review(committed_session_factory, settings):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    other_id, _, _ = _farm_with_assessment(
        committed_session_factory, owner="other-owner", advisor="other-advisor"
    )
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
        TestClient(app, base_url="https://testserver") as other,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        assert _login(other, login="other-owner").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]
        assert _answer(advisor, farm_id, request_id, "Private advisor note").status_code == 201
        asked = other.get(
            "/assistant/ask", params={"q": "Has an advisor reviewed P1?", "farm": farm_id}
        )
        assert asked.status_code == 403
        assert "Private advisor note" not in asked.text
        for path in (
            f"/review/assessments/{advice_id}.json",
            f"/review/assessments/{advice_id}/card",
            f"/review/assessments/{advice_id}",
            "/review/recommendations/",
        ):
            assert other.get(path, params={"farm": farm_id}).status_code == 403, path
        assert (
            other.get(
                "/assistant/ask", params={"q": "Has an advisor reviewed P1?", "farm": other_id}
            ).status_code
            == 200
        )


def test_tour_mode_never_authorises_a_review_read(committed_session_factory, settings):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]
        assert _answer(advisor, farm_id, request_id, "Private advisor note").status_code == 201
        owner.cookies.set("farmcore_tour", "1")
        asked = owner.get(
            "/assistant/ask", params={"q": "Has an advisor reviewed P1?", "farm": farm_id}
        )
        assert asked.status_code == 200
        assert asked.json()["tool"] != "review_assessment_status"
        assert "Private advisor note" not in asked.text
        assert "Private advisor note" not in str(asked.json().get("facts"))
        assert (
            owner.get(f"/review/assessments/{advice_id}.json", params={"farm": farm_id}).status_code
            == 403
        )


def test_the_review_tool_itself_denies_a_context_without_a_member(
    committed_session_factory, settings
):
    """Defence in depth: even a direct tool call from a bot/anonymous context gets no note."""
    from zoneinfo import ZoneInfo

    from farmcore.apps.review import tools as review_tools
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.clock import get_clock

    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]
        assert _answer(advisor, farm_id, request_id, "Private advisor note").status_code == 201
    with committed_session_factory() as session:
        from farmcore.core.models.canonical import Farm
        from farmcore.core.tools import ToolContext

        farm = session.get(Farm, farm_id)
        repo = ScopedRepository(
            session, farm_id, ensure_owner_grant(session, farm), "web:assistant", "assistant"
        )
        ctx = ToolContext(repo, get_clock(session, settings).now(), ZoneInfo(settings.display_tz))
        result = review_tools.status(ctx, parcel="P1")
        assert result.facts["available"] is False
        assert "Private advisor note" not in result.text
        assert "expert" not in result.text
