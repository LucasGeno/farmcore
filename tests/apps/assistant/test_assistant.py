import pytest
from sqlalchemy import select

from farmcore.core.models.canonical import AccessLog
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


@pytest.mark.spanish_data
def test_last_spray_question_uses_audited_read_only_tools(client, session, seeded, step):
    from datetime import datetime

    from farmcore.core.models.canonical import Farm, Log, Parcel

    seeded("B")
    step("2024-04-10 06:00")
    farm = session.scalar(select(Farm).order_by(Farm.id))
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == farm.id, Parcel.alias == "P5"))
    for when in ("2024-04-09T05:00:00+02:00", "2024-04-11T05:00:00+02:00"):
        session.add(
            Log(
                farm_id=farm.id,
                type="spray",
                status="done",
                name="Demo data spray",
                ts_start=datetime.fromisoformat(when),
                parcels=[parcel],
            )
        )
    session.commit()
    response = client.get("/assistant/ask", params={"q": "When was P5 last sprayed?"})
    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == "operations_last_spray"
    assert "P5" in body["text"] and "09 Apr 05:00" in body["text"]
    assert body["facts"]["date"].startswith("2024-04-09")
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    other = client.get("/assistant/ask", params={"q": "When was P5 last sprayed?", "farm": es.id})
    assert other.json()["facts"].get("date") is None
    assert "09 Apr" not in other.json()["text"]
    assert body["links"] and all("farm=" in link[1] for link in body["links"])
    reads = session.scalars(select(AccessLog).where(AccessLog.actor == "web:assistant")).all()
    assert reads and all(r.purpose == "assistant" for r in reads)


def test_plan_answer_uses_same_ranking_and_validated_farm_busy_cookie(
    client, session, seeded, step
):
    from datetime import date

    from farmcore.apps.forecast.planning import plan_days
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.clock import get_clock
    from farmcore.core.models.canonical import Farm

    seeded()
    step("2024-04-10 06:00")
    farm = session.scalar(select(Farm).order_by(Farm.id))
    repo = ScopedRepository(
        session, farm.id, ensure_owner_grant(session, farm), "test", "assistant"
    )
    expected = plan_days(repo, get_clock(session, client.app.state.settings).now(), "Europe/Paris")
    good = next(d for d in expected["days"][:7] if d["grade"] in {"best", "good"})
    client.cookies.set(f"farmcore_forecast_busy_{farm.id}", good["day"] + ",garbage")
    response = client.get(
        "/assistant/ask", params={"q": "Which days this week are good?", "farm": farm.id}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == "assistant_plan"
    assert "Weather only" in body["text"] and "not treatment clearance" in body["text"]
    expected = plan_days(
        repo,
        get_clock(session, client.app.state.settings).now(),
        "Europe/Paris",
        {date.fromisoformat(good["day"])},
    )
    assert body["facts"]["days"] == [
        {k: d[k] for k in ("day", "grade", "reason")} for d in expected["days"][:7]
    ]
    assert body["links"] == [["Open Plan", f"/forecast/plan?farm={farm.id}"]]


@pytest.mark.spanish_data
def test_tour_advice_is_scoped_withheld_for_es_and_never_writes_domain_rows(
    client, session, seeded, step
):
    from farmcore.core.models.canonical import Farm
    from tests.apps.chat.assistant.toolkit import write_counts

    seeded("B")
    step("2024-04-10 06:00")
    before = write_counts(session)
    client.cookies.set("farmcore_tour", "1")
    fr = client.get("/assistant/ask", params={"q": "Do I need to spray anything?"})
    assert fr.status_code == 200 and fr.json()["tool"] == "advisor_today"
    assert "to spray" in fr.json()["text"]
    es = session.scalar(select(Farm).where(Farm.props["country"].astext == "ES"))
    response = client.get(
        "/assistant/ask", params={"q": "Do I need to spray anything?", "farm": es.id}
    )
    assert response.status_code == 200
    assert "withheld" in response.json()["text"]
    assert "Nothing to spray" not in response.json()["text"]
    assert "P5" not in response.json()["text"]
    assert write_counts(session) == before


def test_browser_rate_limit_bounds_questions_without_blocking_other_browsers(
    client, session, settings
):
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    for _ in range(10):
        assert client.get("/assistant/ask?q=hello").status_code == 200
    limited = client.get("/assistant/ask?q=hello")
    assert limited.status_code == 429 and limited.headers["Retry-After"]
    client.cookies.clear()
    assert client.get("/assistant/ask?q=hello").status_code == 200


def test_launcher_is_farm_qualified_and_assistant_off_hides_it(client, session, settings):
    from farmcore.core.seed import ensure_default_farm

    farm = ensure_default_farm(session, settings)
    session.commit()
    page = client.get("/apps", params={"farm": farm.id}).text
    assert 'id="assistant-launcher"' in page and 'id="assistant-dialog"' in page
    assert f"/assistant/ask?farm={farm.id}" in page
    assert client.post("/apps/assistant/uninstall").status_code == 200
    assert 'id="assistant-launcher"' not in client.get("/apps").text
    assert client.get("/assistant/ask?q=hello").status_code == 404


def test_hidden_tool_and_invalid_questions_do_not_invent_an_answer(client, session, settings):
    from farmcore.core.models.kernel import AppModule
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.get(AppModule, "forecast").state = "uninstalled"
    session.commit()
    response = client.get("/assistant/ask", params={"q": "Which days this week are good?"})
    assert response.status_code == 200 and response.json()["tool"] is None
    assert response.json()["facts"] == {}
    assert client.get("/assistant/ask", params={"q": "x" * 501}).status_code == 422
    assert client.get("/assistant/ask?q=").status_code == 422
    assert client.post("/assistant/ask", json={"q": "log a spray"}).status_code == 405


def test_plan_answer_explicitly_labels_non_as_issued_forecast(client, session, seeded, step):
    from farmcore.apps.forecast.models import ForecastRun

    seeded()
    step("2024-04-10 06:00")
    for run in session.scalars(select(ForecastRun)):
        run.props = {**run.props, "as_issued": False}
    session.commit()
    response = client.get("/assistant/ask", params={"q": "Which days this week are good?"})
    assert response.status_code == 200
    body = response.json()
    assert body["facts"]["run"]["as_issued"] is False
    assert "Not as-issued forecast" in body["text"]
    assert "As-issued forecast ·" not in body["text"]


def test_launcher_is_a_compact_round_icon_button(client, session, settings):
    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    page = client.get("/apps").text
    button = page.split('id="assistant-launcher"', 1)[1].split("</button>", 1)[0]
    assert 'aria-label="Ask your farm"' in button
    assert "h-11 w-11" in button and "rounded-full" in button
    assert ">Ask your farm<" not in button


def test_panel_offers_a_conversation_and_preserves_the_archived_mascot(client, session, settings):
    from pathlib import Path

    from farmcore.core.seed import ensure_default_farm

    ensure_default_farm(session, settings)
    session.commit()
    page = client.get("/apps").text
    assert 'aria-label="Ask your farm"' in page
    assert "data-duck" not in page
    assert 'id="assistant-new"' in page and "New conversation" in page
    assert 'role="log"' in page and 'id="assistant-status"' in page
    assert "30 minutes" in page
    # Lean panel: no instructions paragraph, examples live in the status and placeholder.
    assert (
        "Logging stays in Telegram" not in page
        and "Try: What was sprayed recently?" not in page.split('id="assistant-status"')[0]
    )
    assert Path("src/farmcore/core/templates/core/_duck.html").exists()
    client.cookies.set("farmcore_tour", "1")
    read_only = client.get("/apps").text
    assert "Try: What was sprayed recently?" in read_only
    assert 'placeholder="What was sprayed recently?"' in read_only
    assert "When was P5 last sprayed?" not in read_only
