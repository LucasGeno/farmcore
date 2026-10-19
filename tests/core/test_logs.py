"""Log write service: record_operation + spray merge rule, pending → confirm / cancel, events."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from farmcore.core import logs
from farmcore.core.logs import QuantityInput, SourceInput
from farmcore.core.models.canonical import Event, Farm, Log, LogSource, Parcel, Product, Quantity

# Synthetic times and values.
T0 = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"

VOICE = SourceInput("voice", "tg:1", {"transcript": "copper this morning"}, 0.8, 10)
MACHINE = SourceInput("isoxml", "TASK1", {"task": "TSK1"}, 1.0, 30)
SEED = SourceInput("seed", "scene-b", {}, None, 5)


def _farm(session, name: str = "A", n_parcels: int = 3) -> tuple[Farm, list[Parcel]]:
    farm = Farm(name=name)
    session.add(farm)
    session.flush()
    parcels = [
        Parcel(farm_id=farm.id, name=f"P{i}", alias=f"P{i}", area_ha=Decimal("1"), geom=SQUARE)
        for i in range(1, n_parcels + 1)
    ]
    session.add_all(parcels)
    session.flush()
    return farm, parcels


def _dose(value: str, unit: str = "kg/ha") -> list[QuantityInput]:
    return [QuantityInput(measure="rate", value=Decimal(value), unit=unit, label="copper")]


def _spray(session, farm, parcels, source, ts, dose="2", **kw):
    return logs.record_operation(
        session,
        farm_id=farm.id,
        type=kw.pop("type", "spray"),
        parcel_ids=[p.id for p in parcels],
        ts_start=ts,
        ts_end=kw.pop("ts_end", None),
        quantities=_dose(dose),
        source=source,
        actor="owner",
        occurred_at=ts,
        **kw,
    )


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _events(session, kind: str | None = None) -> list[Event]:
    stmt = select(Event).order_by(Event.id)
    if kind:
        stmt = stmt.where(Event.kind == kind)
    return list(session.scalars(stmt))


def _doses(log: Log) -> list[Decimal]:
    return [q.value for q in log.quantities]


def test_new_operation_creates_log_with_parcels_quantities_source_and_events(session):
    farm, parcels = _farm(session)

    log, merged = _spray(session, farm, parcels[:2], VOICE, T0)

    assert merged is False
    assert (log.farm_id, log.type, log.status, log.ts_start) == (farm.id, "spray", "done", T0)
    assert {p.id for p in log.parcels} == {parcels[0].id, parcels[1].id}
    assert [(q.measure, q.value, q.unit) for q in log.quantities] == [
        ("rate", Decimal("2"), "kg/ha")
    ]
    [src] = log.sources
    assert (src.source_type, src.external_key, src.raw_payload, src.confidence) == (
        "voice",
        "tg:1",
        {"transcript": "copper this morning"},
        0.8,
    )
    assert src.precision_rank == 10
    created = _events(session, "log.created")
    assert sorted(e.res_id for e in created) == sorted([parcels[0].id, parcels[1].id])
    assert {(e.res_model, e.farm_id, e.occurred_at, e.payload["log_id"]) for e in created} == {
        ("parcel", farm.id, T0, log.id)
    }


def test_machine_spray_3h_after_voice_merges_and_takes_time_and_dose(session):
    farm, parcels = _farm(session)
    voice_log, _ = _spray(session, farm, parcels[:1], VOICE, T0, dose="2")
    machine_ts = T0 + timedelta(hours=3)

    log, merged = _spray(
        session,
        farm,
        parcels[:1],
        MACHINE,
        machine_ts,
        dose="1.75",
        ts_end=machine_ts + timedelta(minutes=40),
    )

    assert merged is True
    assert log.id == voice_log.id
    assert _count(session, Log) == 1
    assert sorted(s.source_type for s in log.sources) == ["isoxml", "voice"]
    assert log.ts_start == machine_ts
    assert log.ts_end == machine_ts + timedelta(minutes=40)
    assert _doses(log) == [Decimal("1.75")]
    assert [e.res_id for e in _events(session, "log.merged")] == [parcels[0].id]


def test_spray_13h_later_is_a_new_log(session):
    farm, parcels = _farm(session)
    first, _ = _spray(session, farm, parcels[:1], VOICE, T0)

    second, merged = _spray(session, farm, parcels[:1], MACHINE, T0 + timedelta(hours=13))

    assert merged is False
    assert second.id != first.id
    assert _count(session, Log) == 2


def test_merge_window_is_inclusive_at_12h(session):
    farm, parcels = _farm(session)
    _spray(session, farm, parcels[:1], VOICE, T0)

    _, merged = _spray(session, farm, parcels[:1], MACHINE, T0 - timedelta(hours=12))

    assert merged is True


def test_lower_ranked_seed_spray_attaches_source_but_keeps_voice_values(session):
    farm, parcels = _farm(session)
    voice_log, _ = _spray(session, farm, parcels[:1], VOICE, T0, dose="2")

    log, merged = _spray(session, farm, parcels[:1], SEED, T0 + timedelta(hours=2), dose="9")

    assert merged is True
    assert log.id == voice_log.id
    assert sorted(s.source_type for s in log.sources) == ["seed", "voice"]
    assert log.ts_start == T0
    assert _doses(log) == [Decimal("2")]


def test_overlapping_parcel_sets_merge_and_union_parcels(session):
    farm, parcels = _farm(session)
    _spray(session, farm, parcels[:2], VOICE, T0)

    log, merged = _spray(session, farm, parcels[1:], MACHINE, T0 + timedelta(hours=1))

    assert merged is True
    assert {p.id for p in log.parcels} == {p.id for p in parcels}
    assert sorted(e.res_id for e in _events(session, "log.merged")) == sorted(p.id for p in parcels)


@pytest.mark.parametrize("case", ["other_parcel", "other_farm", "not_spray"])
def test_no_merge_without_same_farm_overlapping_parcels_and_spray(session, case):
    farm, parcels = _farm(session)
    _spray(
        session, farm, parcels[:1], VOICE, T0, type="fertilise" if case == "not_spray" else "spray"
    )
    target_farm, target_parcels = (
        (farm, parcels[1:2]) if case == "other_parcel" else (farm, parcels[:1])
    )
    if case == "other_farm":
        target_farm, other_parcels = _farm(session, "B")
        target_parcels = other_parcels[:1]

    _, merged = _spray(
        session,
        target_farm,
        target_parcels,
        MACHINE,
        T0 + timedelta(hours=1),
        type="fertilise" if case == "not_spray" else "spray",
    )

    assert merged is False
    assert _count(session, Log) == 2


def test_parcels_of_another_farm_are_rejected(session):
    farm, _ = _farm(session, "A")
    _, other_parcels = _farm(session, "B")

    with pytest.raises(ValueError, match="parcel"):
        _spray(session, farm, other_parcels[:1], VOICE, T0)


def _pending(session, farm, parcels, source=VOICE, ts=T0, dose="2") -> Log:
    return logs.create_pending(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[p.id for p in parcels],
        ts_start=ts,
        ts_end=None,
        quantities=_dose(dose),
        source=source,
        actor="owner",
        occurred_at=ts,
    )


def test_create_pending_does_not_merge(session):
    farm, parcels = _farm(session)
    _spray(session, farm, parcels[:1], SEED, T0)

    pending = _pending(session, farm, parcels[:1], ts=T0 + timedelta(hours=1))

    assert pending.status == "pending"
    assert _count(session, Log) == 2


def test_pending_then_confirm_is_done_with_confirmed_events(session):
    farm, parcels = _farm(session)
    pending = _pending(session, farm, parcels[:2])
    confirm_at = T0 + timedelta(minutes=5)

    log, merged = logs.confirm(session, pending.id, actor="owner", occurred_at=confirm_at)

    assert merged is False
    assert log.id == pending.id
    assert log.status == "done"
    confirmed = _events(session, "log.confirmed")
    assert sorted(e.res_id for e in confirmed) == sorted(p.id for p in parcels[:2])
    assert {e.occurred_at for e in confirmed} == {confirm_at}


def test_confirm_merges_into_a_done_spray_and_moves_the_source(session):
    farm, parcels = _farm(session)
    seeded, _ = _spray(session, farm, parcels[:1], SEED, T0, dose="9")
    pending = _pending(session, farm, parcels[:1], ts=T0 + timedelta(hours=2), dose="2")
    pending_id = pending.id

    log, merged = logs.confirm(
        session, pending_id, actor="owner", occurred_at=T0 + timedelta(hours=2)
    )

    assert merged is True
    assert log.id == seeded.id
    assert session.get(Log, pending_id) is None
    assert _count(session, Log) == 1
    assert sorted(s.source_type for s in log.sources) == ["seed", "voice"]
    assert _count(session, LogSource) == 2
    # Voice (10) outranks seed (5): its time and dose win.
    assert log.ts_start == T0 + timedelta(hours=2)
    assert _doses(log) == [Decimal("2")]
    assert _count(session, Quantity) == 1
    assert [e.res_id for e in _events(session, "log.merged")] == [parcels[0].id]


def test_cancel_deletes_the_pending_log_and_posts_cancelled(session):
    farm, parcels = _farm(session)
    pending = _pending(session, farm, parcels[:1])
    pending_id = pending.id

    logs.cancel(session, pending_id, actor="owner", occurred_at=T0)

    assert session.get(Log, pending_id) is None
    assert _count(session, Quantity) == 0
    assert _count(session, LogSource) == 0
    assert [e.res_id for e in _events(session, "log.cancelled")] == [parcels[0].id]


def test_confirm_and_cancel_refuse_a_done_log(session):
    farm, parcels = _farm(session)
    done, _ = _spray(session, farm, parcels[:1], VOICE, T0)

    with pytest.raises(ValueError, match="pending"):
        logs.confirm(session, done.id, actor="owner", occurred_at=T0)
    with pytest.raises(ValueError, match="pending"):
        logs.cancel(session, done.id, actor="owner", occurred_at=T0)


def test_higher_ranked_merge_owns_the_interval_even_without_an_end(session):
    farm, parcels = _farm(session)
    _spray(session, farm, parcels[:1], VOICE, T0, ts_end=T0 + timedelta(minutes=30))
    machine_ts = T0 + timedelta(hours=3)

    log, merged = _spray(session, farm, parcels[:1], MACHINE, machine_ts)

    assert merged is True
    assert (log.ts_start, log.ts_end) == (machine_ts, None)


def test_confirm_merge_with_higher_rank_owns_the_interval(session):
    farm, parcels = _farm(session)
    _spray(session, farm, parcels[:1], SEED, T0, ts_end=T0 + timedelta(minutes=30))
    pending = _pending(session, farm, parcels[:1], ts=T0 + timedelta(hours=2))

    log, _ = logs.confirm(session, pending.id, actor="owner", occurred_at=T0 + timedelta(hours=2))

    assert (log.ts_start, log.ts_end) == (T0 + timedelta(hours=2), None)


def test_every_log_event_carries_source_types(session):
    farm, parcels = _farm(session)
    _spray(session, farm, parcels[:1], SEED, T0)
    _spray(session, farm, parcels[:1], MACHINE, T0 + timedelta(hours=1))
    merged_pending = _pending(session, farm, parcels[:1], ts=T0 + timedelta(hours=2))
    logs.confirm(session, merged_pending.id, actor="owner", occurred_at=T0)
    # Distinct Telegram messages are distinct operations; source retries are no-ops.
    alone = _pending(session, farm, parcels[1:2], source=replace(VOICE, external_key="tg:2"))
    logs.confirm(session, alone.id, actor="owner", occurred_at=T0)
    dropped = _pending(session, farm, parcels[2:3], source=replace(VOICE, external_key="tg:3"))
    logs.cancel(session, dropped.id, actor="owner", occurred_at=T0)

    by_kind = [
        (e.kind, e.payload.get("source_types"), "source_type" in e.payload)
        for e in _events(session)
    ]

    assert by_kind == [
        ("log.created", ["seed"], False),
        ("log.merged", ["isoxml"], False),
        ("log.created", ["voice"], False),  # merged_pending
        ("log.merged", ["voice"], False),
        ("log.created", ["voice"], False),  # alone
        ("log.confirmed", ["voice"], False),
        ("log.created", ["voice"], False),  # dropped
        ("log.cancelled", ["voice"], False),
    ]


def test_resending_the_same_source_changes_nothing(session):
    farm, parcels = _farm(session)
    _spray(session, farm, parcels[:1], VOICE, T0)
    first, _ = _spray(session, farm, parcels[:1], MACHINE, T0 + timedelta(hours=1), dose="1.5")
    events_before = len(_events(session))

    again, merged = _spray(session, farm, parcels[:1], MACHINE, T0 + timedelta(hours=1), dose="1.5")

    assert (again.id, merged) == (first.id, True)
    assert _count(session, LogSource) == 2
    assert len(_events(session)) == events_before


@pytest.mark.parametrize("rank", [logs.RANK_VOICE, logs.RANK_SEED])
def test_confirm_merge_keeps_target_product_and_moves_second_product(session, rank):
    farm, parcels = _farm(session)
    products = [
        Product(
            name=name,
            authorisation_no=f"TEST-{name}",
            active_substance=name,
            product_type="contact",
            targets=[],
        )
        for name in ("copper", "sulphur")
    ]
    session.add_all(products)
    session.flush()
    copper, sulphur = products
    target, _ = _spray(session, farm, parcels[:1], VOICE, T0)
    target.quantities[0].product_id = copper.id
    session.flush()
    pending = logs.create_pending(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[parcels[0].id],
        ts_start=T0 + timedelta(hours=1),
        ts_end=None,
        quantities=[
            QuantityInput("rate", Decimal("9"), "L/ha", copper.id, "different copper label"),
            QuantityInput("rate", Decimal("5"), "kg/ha", sulphur.id, "sulphur"),
        ],
        source=replace(VOICE, external_key="tg:second", precision_rank=rank),
        actor="owner",
        occurred_at=T0,
    )
    sulphur_quantity_id = pending.quantities[1].id
    pending_id = pending.id

    log, merged = logs.confirm(session, pending_id, actor="owner", occurred_at=T0)

    session.expire_all()
    assert merged is True
    assert log.id == target.id
    assert session.get(Log, pending_id) is None
    assert sorted((q.product_id, q.value, q.unit, q.measure, q.label) for q in log.quantities) == [
        (copper.id, Decimal("2"), "kg/ha", "rate", "copper"),
        (sulphur.id, Decimal("5"), "kg/ha", "rate", "sulphur"),
    ]
    assert session.get(Quantity, sulphur_quantity_id).log_id == target.id
    assert _count(session, Quantity) == 2
    assert _events(session, "log.merged")[-1].payload["added_products"] == [sulphur.id]


@pytest.mark.parametrize(
    ("measure", "label", "unit", "added"),
    [
        ("rate", "copper", "kg/ha", False),
        ("rate", "sulphur", "kg/ha", True),
        ("volume", "copper", "kg/ha", True),
        ("rate", "copper", "L/ha", True),
    ],
)
def test_confirm_merge_null_product_uses_measure_label_unit(session, measure, label, unit, added):
    farm, parcels = _farm(session)
    target, _ = _spray(session, farm, parcels[:1], VOICE, T0)
    pending = logs.create_pending(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[parcels[0].id],
        ts_start=T0,
        ts_end=None,
        quantities=[QuantityInput(measure, Decimal("5"), unit, label=label)],
        source=replace(VOICE, external_key="tg:second"),
        actor="owner",
        occurred_at=T0,
    )
    quantity_id = pending.quantities[0].id

    log, merged = logs.confirm(session, pending.id, actor="owner", occurred_at=T0)

    session.expire_all()
    assert merged is True
    assert log.id == target.id
    expected = [("rate", "copper", "kg/ha", Decimal("2"))] + (
        [(measure, label, unit, Decimal("5"))] if added else []
    )
    assert sorted((q.measure, q.label, q.unit, q.value) for q in log.quantities) == sorted(expected)
    if added:
        assert session.get(Quantity, quantity_id).log_id == target.id
    assert _count(session, Quantity) == 1 + added
    assert _events(session, "log.merged")[-1].payload["added_products"] == (
        [label] if added else []
    )


@pytest.mark.parametrize("product_ids", [False, True])
@pytest.mark.parametrize("pending_indices", [(0,), (1, 2)])
def test_confirm_keeps_new_product_on_its_reported_parcels(session, product_ids, pending_indices):
    farm, parcels = _farm(session)
    reported = [parcels[i] for i in pending_indices]
    copper_id = sulphur_id = None
    if product_ids:
        products = [
            Product(
                name=name,
                authorisation_no=f"TEST-{name}",
                active_substance=name,
                product_type="contact",
                targets=[],
            )
            for name in ("copper", "sulphur")
        ]
        session.add_all(products)
        session.flush()
        copper_id, sulphur_id = [p.id for p in products]
    target, _ = _spray(session, farm, parcels[:2], VOICE, T0)
    target.quantities[0].product_id = copper_id
    session.flush()
    target_quantity_id = target.quantities[0].id
    pending = logs.create_pending(
        session,
        farm_id=farm.id,
        type="spray",
        parcel_ids=[p.id for p in reported],
        ts_start=T0 + timedelta(hours=1),
        ts_end=None,
        quantities=[
            QuantityInput("rate", Decimal("9"), "kg/ha", copper_id, "copper"),
            QuantityInput("rate", Decimal("5"), "kg/ha", sulphur_id, "sulphur"),
        ],
        source=replace(VOICE, external_key="tg:second"),
        actor="owner",
        occurred_at=T0,
    )
    pending_id = pending.id
    sulphur_quantity_id = pending.quantities[1].id

    saved, merged = logs.confirm(session, pending_id, actor="owner", occurred_at=T0)

    session.expire_all()
    assert merged is False
    assert saved.id == pending_id
    assert saved.status == "done"
    assert saved.ts_start == T0 + timedelta(hours=1)
    assert {p.id for p in saved.parcels} == {p.id for p in reported}
    assert [
        (q.id, q.product_id, q.label, q.value, q.unit, q.measure) for q in saved.quantities
    ] == [
        (sulphur_quantity_id, sulphur_id, "sulphur", Decimal("5"), "kg/ha", "rate"),
    ]
    assert [s.external_key for s in saved.sources] == ["tg:second"]
    assert target.status == "done"
    assert target.ts_start == T0
    assert {p.id for p in target.parcels} == {p.id for p in [*parcels[:2], *reported]}
    assert [(q.id, q.product_id, q.label, q.value) for q in target.quantities] == [
        (target_quantity_id, copper_id, "copper", Decimal("2")),
    ]
    assert [s.external_key for s in target.sources] == [VOICE.external_key]
    assert _count(session, Log) == 2
    assert _count(session, Quantity) == 2
    expanded = pending_indices == (1, 2)
    merged_events = _events(session, "log.merged")
    assert {e.res_id for e in merged_events} == ({p.id for p in parcels} if expanded else set())
    for event in merged_events:
        assert event.payload["log_id"] == target.id
        assert event.payload["parcels_added"] == [parcels[2].id]
        assert event.payload["from_confirmed_log_id"] == pending_id
        assert event.payload["source_types"] == ["voice"]
    confirmed_events = _events(session, "log.confirmed")
    assert {e.res_id for e in confirmed_events} == {p.id for p in reported}
    for confirmed in confirmed_events:
        assert confirmed.payload["duplicate_of"] == target.id
        assert confirmed.payload["dropped_products"] == [copper_id if product_ids else "copper"]
    coverage = sorted(
        (p.alias, q.label, q.value)
        for log in (target, saved)
        for p in log.parcels
        for q in log.quantities
    )
    assert coverage == (
        [
            ("P1", "copper", Decimal("2")),
            ("P2", "copper", Decimal("2")),
            ("P2", "sulphur", Decimal("5")),
            ("P3", "copper", Decimal("2")),
            ("P3", "sulphur", Decimal("5")),
        ]
        if expanded
        else [
            ("P1", "copper", Decimal("2")),
            ("P1", "sulphur", Decimal("5")),
            ("P2", "copper", Decimal("2")),
        ]
    )
