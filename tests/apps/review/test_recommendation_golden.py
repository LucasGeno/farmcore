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
        farm = session.get(Farm, other_id)
        parcel_id = session.get(Advice, other_advice).parcel_id
        session.add(
            Event(
                farm_id=other_id,
                res_model="advice",
                res_id=other_advice,
                kind="advice.reviewed",
                body="Foreign answer",
                payload={
                    "request_id": 1,
                    "advice_id": other_advice,
                    "parcel_id": parcel_id,
                    "opinion": "agree",
                    "note": "Foreign private note",
                    "reviewer": "other-advisor",
                },
                actor="user:0",
                source_app="review",
                occurred_at=NOW,
            )
        )
        set_replay(session, NOW)
        assert farm.id == other_id
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
