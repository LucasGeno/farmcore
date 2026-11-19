"""Stock moves exactly once: real competing sessions, retries and rollbacks (failure model 3, 4
in `test_records.py`). 1000 kg of CAN 27 bought; parcel A is 2 ha."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from farmcore.core import drafts
from farmcore.core.models.canonical import Log
from farmcore.core.models.catalog import TypedQuantity
from farmcore.core.models.corrections import OperationCorrection
from farmcore.core.models.drafts import Draft
from tests.apps.fertilization.conftest import APPLIED, application, confirm, propose, seed, used

WORKERS = 6


@pytest.fixture
def committed(settings, committed_session_factory):
    with committed_session_factory() as session:
        case = seed(session, settings)
        session.commit()
    return case


def race(factory, action, n=WORKERS):
    """Run `action(session)` in `n` real sessions released together; each commits or raises."""
    barrier = threading.Barrier(n)

    def run(_):
        with factory() as session:
            barrier.wait()
            try:
                result = action(session)
            except drafts.DraftError as exc:
                return exc
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def test_stock_moves_once_under_retry_concurrency_and_rollback(
    committed, committed_session_factory
):
    factory = committed_session_factory
    with factory() as session:
        draft = propose(session, committed, application(committed))
        planned = propose(
            session,
            committed,
            application(committed, ts_start=APPLIED.replace(day=20).isoformat()),
            key="planned",
        )
        session.commit()
    with factory() as session:
        assert used(session, committed) == 0  # an open draft consumes nothing
        with pytest.raises(drafts.DraftInvalid, match="planned work"):
            confirm(session, committed, planned.id)
        confirm(session, committed, draft.id)
        session.rollback()  # the request failed after confirming: nothing stays
    with factory() as session:
        assert used(session, committed) == 0

    outcomes = race(factory, lambda s: confirm(s, committed, draft.id)[1])
    with factory() as session:
        retried = confirm(session, committed, draft.id)[1]
        session.commit()
    assert (outcomes.count(True), outcomes.count(False), retried) == (1, WORKERS - 1, False)
    with factory() as session:
        assert used(session, committed) == Decimal("600")  # 300 kg/ha x 2 ha, once
        assert session.scalar(select(func.count(Log.id)).where(Log.type == "fertilise")) == 1


def test_competing_corrections_apply_once_and_keep_the_original(
    committed, committed_session_factory
):
    factory = committed_session_factory
    with factory() as session:
        draft = propose(session, committed, application(committed))
        log_id = confirm(session, committed, draft.id)[0].result_id
        corrections = [
            propose(
                session,
                committed,
                {"log_id": log_id, "reason": reason, "changes": {"amount": amount}},
                kind="fertilization-correction",
                key=reason,
            ).id
            for reason, amount in (
                ("spreader calibrated at 200", {"value": "200", "unit": "kg/ha"}),
                ("weighed the hopper: 250", {"value": "250", "unit": "kg/ha"}),
            )
        ]
        session.commit()

    pending = list(corrections)
    outcomes = race(
        factory,
        lambda s: confirm(s, committed, pending.pop(), actor="operator")[0].id,
        n=2,
    )
    won = [o for o in outcomes if not isinstance(o, Exception)]
    lost = [o for o in outcomes if isinstance(o, Exception)]
    assert len(won) == 1 and "changed since" in lost[0].reason
    with factory() as session:
        assert confirm(session, committed, won[0])[1] is False  # retried: applied already
        session.commit()

    with factory() as session:
        winner = session.get(Draft, won[0])
        amount = Decimal(winner.payload["changes"]["amount"]["value"])
        assert used(session, committed) == amount * 2  # stock moved by the delta only
        (typed,) = session.scalars(select(TypedQuantity).where(TypedQuantity.log_id == log_id))
        assert (typed.entered_value, typed.entered_unit) == (amount, "kg/ha")
        (receipt,) = session.scalars(select(OperationCorrection))
        assert receipt.before_snapshot["amount"] == {"value": "300", "unit": "kg/ha"}
        assert receipt.after_snapshot["amount"]["value"] == str(amount)
        assert (receipt.reason, receipt.actor_key, receipt.finalized_actor_key) == (
            winner.payload["reason"],
            "operator",
            "operator",
        )
