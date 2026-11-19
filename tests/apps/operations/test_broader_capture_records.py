"""Confirmed field observations and irrigation facts on the operations pages.

An irrigation's amount and unit are read from its one entered `typed_quantity`, the gross/net
basis from validated `log.props`; a pending draft is not a record, and facts that do not validate
are never guessed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.apps.operations import facts
from farmcore.core import clock, logs
from farmcore.core.models.canonical import Log
from farmcore.core.models.catalog import TypedQuantity

T_FACT = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
T_CLOCK = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)


def _at(session, settings, t=T_CLOCK):
    clock.set_replay(session, t, settings)
    session.commit()


def _fact(session, demo, kind, props, *, status="done", key="fact-1", at=T_FACT):
    """A record as capture writes it: an irrigation's amount also as its entered quantity."""
    amount = (props.get("irrigation") or {}) if kind == "irrigate" else {}
    try:
        entered = [logs.EnteredQuantity(Decimal(amount["amount"]), amount["unit"])]
    except (KeyError, ArithmeticError):
        entered = []
    log, _ = logs.record_operation(
        session,
        farm_id=demo.farm.id,
        type=kind,
        parcel_ids=[demo.parcels[0].id],
        ts_start=at,
        ts_end=None,
        quantities=entered,
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


def _entered(session, log):
    return [
        (q.entered_value, q.entered_unit)
        for q in session.scalars(
            select(TypedQuantity).where(TypedQuantity.log_id == log.id).order_by(TypedQuantity.id)
        )
    ]


def test_an_irrigation_amount_is_read_from_its_entered_quantity_only(
    client, session, settings, demo
):
    """One reader: when the props copy disagrees, the page shows the entered quantity; with no
    entered quantity (a pre-core_0017 row the backfill could not read) it says so."""
    _at(session, settings)
    log = _fact(
        session, demo, "irrigate", {"irrigation": {"amount": "20", "unit": "mm", "basis": "net"}}
    )
    log.props = {"irrigation": {"amount": "99", "unit": "L", "basis": "net"}}
    session.flush()

    page = client.get("/operations/").text
    assert "Irrigation 20 mm net" in page and "Irrigation 99 L" not in page

    session.execute(TypedQuantity.__table__.delete().where(TypedQuantity.log_id == log.id))
    session.flush()
    assert "Irrigation (details unavailable)" in client.get("/operations/").text


def test_an_irrigation_correction_updates_both_copies_or_neither(session, demo, monkeypatch):
    log = _fact(
        session, demo, "irrigate", {"irrigation": {"amount": "20", "unit": "mm", "basis": "gross"}}
    )

    facts.record_irrigation(session, log, facts.IrrigationFact(amount="12.50", unit="m3"))

    assert _entered(session, log) == [(Decimal("12.50"), "m3")]
    assert log.props == {"irrigation": {"amount": "12.50", "unit": "m3"}}
    assert facts.irrigation(log) == facts.IrrigationFact(amount="12.50", unit="m3")

    def fail(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(facts, "enter_quantity", fail)
    with pytest.raises(RuntimeError):
        facts.record_irrigation(session, log, facts.IrrigationFact(amount="30", unit="mm"))

    session.refresh(log)
    assert _entered(session, log) == [(Decimal("12.50"), "m3")]
    assert log.props == {"irrigation": {"amount": "12.50", "unit": "m3"}}
