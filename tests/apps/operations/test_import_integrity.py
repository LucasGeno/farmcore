"""Synthetic replay and receipt regressions over disposable PostGIS databases."""

import xml.etree.ElementTree as ET

from farmcore.apps.operations import isoxml, isoxml_demo, register
from farmcore.core import logs
from tests.apps.operations.isoxml_farm import local, make_farm


def single_task():
    root = ET.fromstring(isoxml_demo.demo_taskdata())
    tasks = root.findall("TSK")
    root.remove(tasks[1])
    tasks[0].find("TIM").set("A", "2024-04-10T03:00:00Z")
    tasks[0].find("TIM").set("B", "2024-04-10T03:30:00Z")
    return ET.tostring(root)


def record(session, farm, *, key="future", at="2024-04-10 08:00", **kwargs):
    return logs.record_operation(
        session,
        farm_id=farm.repo.farm_id,
        type="spray",
        parcel_ids=[farm.parcels["P5"].id],
        ts_start=local(at),
        ts_end=None,
        quantities=[],
        source=logs.SourceInput("isoxml", key, {}, 1.0, logs.RANK_MACHINE),
        actor="test",
        occurred_at=local("2024-04-10 10:00"),
        **kwargs,
    )


def test_import_does_not_hide_past_task_in_future_log(session, settings):
    farm = make_farm(session, settings)
    future, _ = record(session, farm)
    now = local("2024-04-10 06:00")
    result = isoxml.import_taskdata(farm.repo, single_task(), now=now, actor="test")
    assert result.tasks[0].log_id != future.id
    assert not result.tasks[0].merged
    rows = register.register_rows(farm.repo, now)
    assert rows and {row["start_time"] for row in rows} == {"05:00"}


def test_bounded_record_selects_eligible_target_instead_of_closer_future(session, settings):
    farm = make_farm(session, settings)
    older, _ = record(session, farm, key="older", at="2024-04-09 18:00")
    future, _ = record(session, farm, at="2024-04-10 08:00")
    assert older.id != future.id
    incoming, merged = record(
        session, farm, key="incoming", at="2024-04-10 05:00", not_after=local("2024-04-10 06:00")
    )
    assert incoming.id == older.id and merged
    assert incoming.ts_start == local("2024-04-09 18:00")
    assert {s.external_key for s in incoming.sources} == {"older", "incoming"}


def test_cached_receipt_returns_canonical_log_after_pending_confirmation(
    committed_session_factory, settings
):
    import hashlib

    from sqlalchemy import select

    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.models.canonical import Farm, Log, LogSource

    factory = committed_session_factory
    data = single_task()
    receipt = f"{hashlib.sha256(data).hexdigest()[:12]}:TSK1"
    with factory.begin() as setup:
        farm = make_farm(setup, settings)
        target, _ = logs.record_operation(
            setup,
            farm_id=farm.repo.farm_id,
            type="spray",
            parcel_ids=[farm.parcels["P5"].id],
            ts_start=local("2024-04-10 04:00"),
            ts_end=None,
            quantities=[],
            source=logs.SourceInput("voice", "earlier", {}, 1.0, logs.RANK_VOICE),
            actor="test",
            occurred_at=local("2024-04-10 04:00"),
        )
        pending = logs.create_pending(
            setup,
            farm_id=farm.repo.farm_id,
            type="spray",
            parcel_ids=[farm.parcels["P5"].id],
            ts_start=local("2024-04-10 05:00"),
            ts_end=None,
            quantities=[],
            source=logs.SourceInput("isoxml", receipt, {}, 1.0, logs.RANK_MACHINE),
            actor="test",
            occurred_at=local("2024-04-10 05:00"),
        )
        farm_id, target_id, pending_id = farm.repo.farm_id, target.id, pending.id
    with factory() as stale:
        cached_target = stale.get(Log, target_id)
        assert cached_target.ts_start == local("2024-04-10 04:00")
        cached_pending = stale.get(Log, pending_id)
        cached_source = stale.scalar(select(LogSource).where(LogSource.external_key == receipt))
        assert cached_source.log_id == cached_pending.id
        with factory.begin() as winner:
            confirmed, merged = logs.confirm(winner, pending_id, "test", local("2024-04-10 10:00"))
            assert merged and confirmed.id == target_id
        f = stale.get(Farm, farm_id)
        repo = ScopedRepository(stale, farm_id, ensure_owner_grant(stale, f), "test", "test")
        result = isoxml.import_taskdata(repo, data, now=local("2024-04-10 10:00"), actor="test")
        task = result.tasks[0]
        assert task.log_id == target_id and task.log_id != cached_pending.id
        assert task.unchanged and task.merged and not task.replaced
        assert task.badges == ("voice", "isoxml")
        assert cached_source.log_id == target_id
        assert cached_target.ts_start == local("2024-04-10 05:00")
    with factory() as check:
        assert check.get(Log, pending_id) is None
