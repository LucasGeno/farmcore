"""Confirmed field observations and irrigation facts on the operations pages.

The amount, its unit and the gross/net basis are shown from validated `log.props`; a pending
draft is not a record, and props that do not validate are never guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from farmcore.core import clock, logs
from farmcore.core.models.canonical import Log

T_FACT = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
T_CLOCK = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)


def _at(session, settings, t=T_CLOCK):
    clock.set_replay(session, t, settings)
    session.commit()


def _fact(session, demo, kind, props, *, status="done", key="fact-1", at=T_FACT):
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type=kind,
        parcel_ids=[demo.parcels[0].id],
        ts_start=at,
        ts_end=None,
        quantities=[],
        source=logs.SourceInput("voice", key, {}, 0.9, logs.RANK_VOICE),
        actor="owner",
        occurred_at=at,
        status=status,
    )
    log.props = props
    session.flush()
    return log


def test_a_confirmed_irrigation_shows_its_entered_amount_unit_and_basis(
    client, session, settings, demo
):
    _at(session, settings)
    _fact(
        session,
        demo,
        "irrigate",
        {"irrigation": {"amount": "20", "unit": "mm", "basis": "gross"}},
    )

    page = client.get("/operations/").text
    timeline = client.get(f"/operations/parcels/{demo.parcels[0].id}/timeline").text

    assert "Irrigation" in page and "Irrigation 20 mm gross" in page
    assert "Irrigation 20 mm gross" in timeline
    assert "data-facts" in page and "data-facts" in timeline


def test_a_confirmed_irrigation_without_a_stated_basis_says_so(client, session, settings, demo):
    _at(session, settings)
    _fact(session, demo, "irrigate", {"irrigation": {"amount": "12", "unit": "m3"}})

    page = client.get("/operations/").text

    assert "Irrigation 12 m3 (basis not stated)" in page
    assert "efficiency" not in page.casefold()


def test_a_confirmed_observation_shows_what_was_seen(client, session, settings, demo):
    _at(session, settings)
    _fact(session, demo, "observation", {"observation": {"text": "Powdery mildew on leaves"}})

    page = client.get("/operations/").text
    timeline = client.get(f"/operations/parcels/{demo.parcels[0].id}/timeline").text

    assert "Observation: Powdery mildew on leaves" in page
    assert "Observation: Powdery mildew on leaves" in timeline


def test_a_pending_draft_is_not_a_confirmed_record(client, session, settings, demo):
    _at(session, settings)
    pending = _fact(
        session,
        demo,
        "irrigate",
        {"irrigation": {"amount": "20", "unit": "mm", "basis": "gross"}},
        status="pending",
    )

    page = client.get("/operations/").text
    register = client.get("/operations/register").text
    timeline = client.get(f"/operations/parcels/{demo.parcels[0].id}/timeline").text

    assert f'data-record="{pending.id}"' not in page
    assert "Irrigation 20 mm gross" not in page
    assert "20 mm" not in register
    # The timeline may mention the draft event, but never as a fact with its amount.
    assert "Irrigation 20 mm gross" not in timeline


def test_invalid_fact_props_are_not_guessed_or_rendered(client, session, settings, demo):
    _at(session, settings)
    _fact(session, demo, "irrigate", {"irrigation": {"amount": "abc", "unit": "mm"}})

    page = client.get("/operations/").text

    assert "Irrigation (details unavailable)" in page
    assert "abc" not in page


def test_the_spray_register_never_lists_an_irrigation(client, session, settings, demo):
    _at(session, settings)
    log = _fact(
        session,
        demo,
        "irrigate",
        {"irrigation": {"amount": "20", "unit": "mm", "basis": "net"}},
    )

    rows = client.get("/operations/register.json").json()

    assert all(row.get("log_id") != log.id for row in rows)
    assert session.scalar(select(Log).where(Log.id == log.id)).type == "irrigate"
