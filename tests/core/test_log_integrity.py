"""Committed-session operation integrity; all inputs are synthetic."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from random import Random
from time import monotonic, sleep

import pytest
from sqlalchemy import func, select, text

from farmcore.apps.operations import isoxml, isoxml_demo
from farmcore.core import logs
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Event, Farm, Log, LogSource
from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm
from tests.core.test_logs import MACHINE, T0, _farm, _spray


def test_source_identity_outlives_merge_window_and_pending_state(session):
    farm, parcels = _farm(session)
    first, _ = _spray(session, farm, parcels[:1], MACHINE, T0, status="pending")
    again, merged = _spray(session, farm, parcels[1:], MACHINE, T0 + timedelta(days=2))
    assert again.id == first.id
    assert merged
    assert again.status == "pending"
    assert [p.id for p in again.parcels] == [parcels[0].id]
    assert session.scalar(select(func.count()).select_from(LogSource)) == 1
    assert session.scalar(select(func.count()).select_from(Event)) == 1


def _wait_for_block_or_finish(engine, pid, future):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with engine.connect() as conn:
            blocked = conn.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            )
        if blocked or future.done():
            return bool(blocked)
        sleep(0.01)
    # Failure-only watchdog: a stalled test must fail rather than hang the suite.
    raise AssertionError(  # pragma: no cover
        "second public call neither completed nor waited on the first transaction"
    )


def _repo(session, farm_id):
    farm = session.get(Farm, farm_id)
    return ScopedRepository(session, farm_id, ensure_owner_grant(session, farm), "test", "test")


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("port", ["record", "import"])
def test_concurrent_source_receipt_is_atomic(
    committed_session_factory, settings, engine, port, rollback
):
    factory = committed_session_factory
    with factory.begin() as setup:
        farm = make_farm(setup, settings)
        farm_id, parcel_id = farm.repo.farm_id, farm.parcels["P5"].id

    def write(session):
        if port == "import":
            result = isoxml.import_taskdata(
                _repo(session, farm_id), isoxml_demo.demo_taskdata(), now=local(CLOCK), actor="test"
            )
            return tuple(t.log_id for t in result.tasks), all(t.unchanged for t in result.tasks)
        log, merged = logs.record_operation(
            session,
            farm_id=farm_id,
            type="harvest",
            parcel_ids=[parcel_id],
            ts_start=T0,
            ts_end=None,
            quantities=[],
            source=MACHINE,
            actor="test",
            occurred_at=T0,
        )
        return (log.id,), merged

    entered = Queue()

    def second():
        with factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '8s'"))
            entered.put(session.scalar(text("SELECT pg_backend_pid()")))
            return write(session)

    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        first_result = write(first)
        future = pool.submit(second)
        pid = entered.get(timeout=5)
        try:
            blocked = _wait_for_block_or_finish(engine, pid, future)
        finally:
            first.rollback() if rollback else first.commit()
        second_result = future.result(timeout=10)
    assert blocked, "same-farm source decisions must wait for the first transaction"
    assert second_result[1] is (not rollback)
    if not rollback:
        assert second_result[0] == first_result[0]
    expected = 2 if port == "import" else 1
    with factory() as check:
        assert check.scalar(select(func.count()).select_from(Log)) == expected
        assert check.scalar(select(func.count()).select_from(LogSource)) == expected
        assert check.scalar(select(func.count()).select_from(Event)) == expected


@pytest.mark.parametrize("first_action", ["confirm", "cancel"])
def test_pending_transitions_take_farm_lock_before_row_lock(
    committed_session_factory, engine, first_action
):
    factory = committed_session_factory
    with factory.begin() as setup:
        farm, parcels = _farm(setup)
        farm_id, parcel_id = farm.id, parcels[0].id
        pending, _ = _spray(setup, farm, parcels[:1], MACHINE, T0, status="pending")
        pending_id = pending.id
    entered = Queue()

    def second():
        with factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            entered.put(session.scalar(text("SELECT pg_backend_pid()")))
            action = logs.cancel if first_action == "confirm" else logs.confirm
            with pytest.raises(ValueError if first_action == "confirm" else LookupError):
                action(session, pending_id, "test", T0)

    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        # A real public operation acquires the farm lock, without locking the pending row.
        logs.record_operation(
            first,
            farm_id=farm_id,
            type="harvest",
            parcel_ids=[parcel_id],
            ts_start=T0,
            ts_end=None,
            quantities=[],
            source=logs.SourceInput("file", "other", {}, None, 20),
            actor="test",
            occurred_at=T0,
        )
        future = pool.submit(second)
        pid = entered.get(timeout=5)
        try:
            assert _wait_for_block_or_finish(engine, pid, future)
            first.execute(text("SET LOCAL lock_timeout = '2s'"))
            getattr(logs, first_action)(first, pending_id, "test", T0)
        finally:
            first.commit()
        future.result(timeout=10)
    with factory() as check:
        row = check.get(Log, pending_id)
        assert (row.status if row else None) == ("done" if first_action == "confirm" else None)


def test_identical_receipt_on_another_farm_does_not_wait_or_deduplicate(
    committed_session_factory,
):
    factory = committed_session_factory
    with factory.begin() as setup:
        farm_a, parcels_a = _farm(setup, "A")
        farm_b, parcels_b = _farm(setup, "B")

    def write_other():
        with factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '1s'"))
            return _spray(session, farm_b, parcels_b[:1], MACHINE, T0)[0].id

    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        first_id = _spray(first, farm_a, parcels_a[:1], MACHINE, T0)[0].id
        try:
            other_id = pool.submit(write_other).result(timeout=3)
        finally:
            first.commit()
    assert first_id != other_id
    with factory() as check:
        assert check.scalar(select(func.count()).select_from(LogSource)) == 2


def test_merge_refreshes_preloaded_sources_before_comparing_precision(committed_session_factory):
    from tests.core.test_logs import VOICE

    factory = committed_session_factory
    with factory.begin() as setup:
        farm, parcels = _farm(setup)
        log_id = _spray(setup, farm, parcels[:1], VOICE, T0)[0].id
    with factory() as stale:
        old = stale.get(Log, log_id)
        assert len(old.sources) == len(old.quantities) == len(old.parcels) == 1
        with factory.begin() as winner:
            _spray(winner, farm, parcels[:2], MACHINE, T0 + timedelta(hours=1), dose="9")
        lower = logs.SourceInput("file", "lower", {}, None, 20)
        result, merged = _spray(stale, farm, parcels[2:], lower, T0 + timedelta(hours=2), dose="4")
        # Parcel 3 does not overlap: no merge, so also exercise the stale source retry below.
        assert not merged
        assert result.id != log_id
        result, merged = _spray(stale, farm, parcels[:1], lower, T0 + timedelta(hours=2), dose="4")
        assert merged and result.id != log_id  # exact receipt cannot move to another log
        fresh_key = logs.SourceInput("file", "fresh", {}, None, 20)
        result, merged = _spray(
            stale, farm, parcels[:1], fresh_key, T0 + timedelta(hours=2), dose="4"
        )
        assert merged and result.id == log_id
        assert result.ts_start == T0 + timedelta(hours=1)
        assert [q.value for q in result.quantities] == [9]
        assert len(result.sources) == 3
        assert {p.id for p in result.parcels} == {p.id for p in parcels[:2]}
        stale.commit()


PROPERTY_SEED = 20261017


def test_cutoff_generated_properties_preserve_boundaries_and_default(session):
    edges = (-43200, -43199, -1, 0, 1, 43199, 43200)
    cases = [(delta, cutoff) for delta in edges for cutoff in edges]
    rng = Random(PROPERTY_SEED)
    cases.extend((rng.randint(-43200, 43200), rng.randint(-43200, 43200)) for _ in range(151))
    assert len(cases) == 200
    for delta_seconds, cutoff_seconds in cases:
        savepoint = session.begin_nested()
        try:
            farm, parcels = _farm(session)
            target_time = T0 + timedelta(seconds=delta_seconds)
            target, _ = _spray(session, farm, parcels[:1], MACHINE, target_time)
            incoming = logs.SourceInput("voice", "bounded", {}, None, 10)
            result, merged = _spray(
                session,
                farm,
                parcels[:1],
                incoming,
                T0,
                not_after=T0 + timedelta(seconds=cutoff_seconds),
            )
            assert merged is (delta_seconds <= cutoff_seconds)
            assert (result.id == target.id) is merged
            assert result.ts_start == (target_time if merged else T0)
            default = logs.SourceInput("file", "default", {}, None, 20)
            unbounded, merged_default = _spray(session, farm, parcels[:1], default, target_time)
            assert merged_default and unbounded.id == target.id
        finally:
            savepoint.rollback()


def test_cancel_removes_receipt_and_allows_a_new_pending_message_retry(session):
    farm, parcels = _farm(session)
    first, _ = _spray(session, farm, parcels[:1], MACHINE, T0, status="pending")
    logs.cancel(session, first.id, "test", T0)
    retry, merged = _spray(session, farm, parcels[:1], MACHINE, T0, status="pending")
    assert retry.id != first.id and not merged
    assert retry.status == "pending"
    assert session.scalar(select(func.count()).select_from(LogSource)) == 1


def test_confirmed_source_retry_retains_canonical_log(session):
    farm, parcels = _farm(session)
    pending, _ = _spray(session, farm, parcels[:1], MACHINE, T0, status="pending")
    confirmed, _ = logs.confirm(session, pending.id, "test", T0)
    retry, merged = _spray(session, farm, parcels[:1], MACHINE, T0 + timedelta(days=2))
    assert retry.id == confirmed.id and merged
    assert retry.status == "done"
    assert session.scalar(select(func.count()).select_from(LogSource)) == 1
    assert session.scalar(select(func.count()).select_from(Event)) == 2


def test_source_identity_includes_farm_and_source_type(session):
    farm_a, parcels_a = _farm(session, "A")
    farm_b, parcels_b = _farm(session, "B")
    a, _ = _spray(session, farm_a, parcels_a[:1], MACHINE, T0, type="harvest")
    b, merged = _spray(session, farm_b, parcels_b[:1], MACHINE, T0, type="harvest")
    assert b.id != a.id and not merged
    other_type = logs.SourceInput("voice", MACHINE.external_key, {}, None, 10)
    c, merged = _spray(session, farm_a, parcels_a[:1], other_type, T0, type="harvest")
    assert c.id not in {a.id, b.id} and not merged
    assert session.scalar(select(func.count()).select_from(LogSource)) == 3


def test_import_locks_before_explicit_validation_flush_of_dirty_pending(
    committed_session_factory, settings, engine
):
    factory = committed_session_factory
    with factory.begin() as setup:
        farm = make_farm(setup, settings)
        farm_id, parcel_id = farm.repo.farm_id, farm.parcels["P5"].id
        pending = logs.create_pending(
            setup,
            farm_id=farm_id,
            type="spray",
            parcel_ids=[parcel_id],
            ts_start=T0,
            ts_end=None,
            quantities=[],
            source=logs.SourceInput("voice", "dirty-pending", {}, None, 10),
            actor="test",
            occurred_at=T0,
        )
        pending_id = pending.id
    entered = Queue()

    def second():
        with factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            repo = _repo(session, farm_id)
            pending = session.get(Log, pending_id)
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            pending.name = "caller update retained"
            entered.put(pid)
            return isoxml.import_taskdata(
                repo, isoxml_demo.demo_taskdata(), now=local(CLOCK), actor="test"
            )

    with factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        logs.record_operation(
            first,
            farm_id=farm_id,
            type="harvest",
            parcel_ids=[parcel_id],
            ts_start=T0,
            ts_end=None,
            quantities=[],
            source=logs.SourceInput("file", "harvest", {}, None, 20),
            actor="test",
            occurred_at=T0,
        )
        future = pool.submit(second)
        pid = entered.get(timeout=5)
        try:
            assert _wait_for_block_or_finish(engine, pid, future)
            first.execute(text("SET LOCAL lock_timeout = '1s'"))
            logs.confirm(first, pending_id, "test", T0)
            first.commit()
        finally:
            first.rollback()
        result = future.result(timeout=10)
    assert result.tasks[0].log_id == pending_id
    with factory() as check:
        pending = check.get(Log, pending_id)
        assert pending.status == "done" and pending.name == "caller update retained"


@pytest.mark.parametrize("action", [logs.confirm, logs.cancel])
def test_missing_pending_log_fails_without_writes(session, action):
    with pytest.raises(LookupError, match="not found"):
        action(session, 987654321, "test", T0)
    assert session.scalar(select(func.count()).select_from(Event)) == 0
    assert session.scalar(select(func.count()).select_from(LogSource)) == 0
