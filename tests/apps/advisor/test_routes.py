"""Advisor pages: the parcel advice card, its `.json`, and the parcel list.

Data tests seed the demo farm and step the replay clock to the hero scenes, so the
card shows real engine reasons. Expected strings come from the scenes and the engine's rule names.
"""

from __future__ import annotations

import html

import pytest
from sqlalchemy import func, select

from farmcore.core import jobs
from farmcore.core.models.canonical import AccessLog, Advice, Parcel
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local

# Shared fixtures (scenes.py): seed the demo farm, step the replay clock.
manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step


def parcel(session, alias: str = "P1") -> Parcel:
    return session.scalar(select(Parcel).where(Parcel.alias == alias))


def latest_row(session, parcel_id: int) -> Advice:
    return session.scalar(
        select(Advice)
        .where(Advice.parcel_id == parcel_id)
        .order_by(Advice.evaluated_at.desc(), Advice.id.desc())
        .limit(1)
    )


def advice_reads(session) -> int:
    return session.scalar(
        select(func.count())
        .select_from(AccessLog)
        .where(AccessLog.scope == "advice:read", AccessLog.resource == "advice")
    )


@pytest.mark.data
def test_card_json_matches_the_latest_advice_row(client, session, seeded, step) -> None:
    seeded()
    p1 = parcel(session)
    step("2024-04-10 06:00")
    row = latest_row(session, p1.id)

    before = advice_reads(session)
    resp = client.get(f"/advisor/parcels/{p1.id}.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == row.id
    assert body["decision"] == row.decision == "spray"
    assert body["reasons"] == row.reasons
    assert body["products"] == row.products
    assert body["evaluated_at"] == local("2024-04-10 06:00").isoformat()
    assert body["next_window"] == [
        row.next_window_start.isoformat(),
        row.next_window_end.isoformat(),
    ]
    assert body["flags"] == row.props["flags"]
    assert body["headline"] == row.props["headline"]
    assert body["parcel"]["alias"] == "P1"
    assert advice_reads(session) > before


@pytest.mark.data
def test_card_renders_headline_every_reason_with_source_and_timeline(
    client, session, seeded, step
) -> None:
    seeded()
    p1 = parcel(session)
    step("2024-04-10 06:00")
    row = latest_row(session, p1.id)

    before = advice_reads(session)
    # Debug mode: the stored source strings render verbatim only there; the farmer view
    # shows them cleaned (tests/apps/advisor/test_card_plain.py). No admin token: we are admin.
    resp = client.get(f"/advisor/parcels/{p1.id}", params={"debug": "1"})

    assert resp.status_code == 200
    page = html.unescape(resp.text)
    # Window opens 06:00 local (hero golden) and stays open to the end of the loaded forecast, so
    # the card names no end hour; the 3-10 event of 8 Apr 22:00 UTC followed 10.3 mm in 48 h
    # (summed from the station CSV).
    headline = "Spray copper from today 06 h: infection risk after 10.3 mm rain"
    assert f"{headline}, not protected" in page, row.props["headline"]
    for reason in row.reasons:
        assert reason["rule"] in page
        assert reason["verdict"] in page
        assert reason["source"] in page
    table = resp.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert ">assumed</span>" in table  # assumed thresholds are marked in the reasons table
    assert "replay: observed weather used as forecast" in page
    assert f'hx-get="/operations/parcels/{p1.id}/timeline"' in resp.text
    assert advice_reads(session) > before


@pytest.mark.data
def test_protected_card_shows_until_and_mm_since_spray(
    client, session, settings, session_factory, manifests, seeded, step
) -> None:
    seeded()
    p1 = parcel(session)
    step("2024-04-10 06:00")
    confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok

    page = html.unescape(client.get(f"/advisor/parcels/{p1.id}").text)

    assert "Protected until" in page
    assert "mm rain since spray" in page


@pytest.mark.data
def test_scene_b_card_flags_washed_off(client, session, seeded, step) -> None:
    seeded("B")
    p1 = parcel(session)
    step("2024-05-15 06:00")

    page = html.unescape(client.get(f"/advisor/parcels/{p1.id}").text)

    assert "washed off" in page.lower()


@pytest.mark.data
def test_index_lists_every_parcel_with_its_decision(client, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")

    resp = client.get("/advisor/")

    assert resp.status_code == 200
    for n in range(1, 11):
        assert f">P{n}<" in resp.text
    # The parcels by decision (the summary and the expert table carry chips too).
    grouped = resp.text.split('id="by-decision"', 1)[1].split("</section>", 1)[0]
    assert grouped.count('data-decision="spray"') == 10


@pytest.mark.data
def test_card_before_any_advice_says_no_advice_yet(client, session, seeded) -> None:
    seeded()
    p1 = parcel(session)

    page = client.get(f"/advisor/parcels/{p1.id}")
    data = client.get(f"/advisor/parcels/{p1.id}.json")

    assert page.status_code == 200 and "No advice yet" in page.text
    assert data.status_code == 200
    assert data.json()["decision"] == "none" and data.json()["id"] is None


def test_unknown_parcel_is_404(client, session, settings) -> None:
    from farmcore.core import seed as core_seed

    core_seed.ensure_default_farm(session, settings)
    session.commit()

    assert client.get("/advisor/parcels/999999").status_code == 404
    assert client.get("/advisor/parcels/999999.json").status_code == 404


@pytest.mark.data
def test_card_says_a_spray_without_a_known_product_was_ignored(
    client, session, settings, session_factory, manifests, seeded, step
) -> None:
    seeded()
    p1 = parcel(session)
    step("2024-04-10 06:00")
    confirm_spray(session, settings, "P1", local("2024-04-10 05:30"), None)
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok

    page = html.unescape(client.get(f"/advisor/parcels/{p1.id}").text)

    assert "Spray without a known product ignored: it gives no cover" in page
