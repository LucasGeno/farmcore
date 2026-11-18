"""Season usage is computed from logged sprays (never entered twice); on hand = purchased − used."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from farmcore.apps.inventory import service
from farmcore.apps.operations import seed as ops_seed
from farmcore.core import logs
from farmcore.core import seed as core_seed
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import Farm
from tests.apps.inventory.conftest import T_APR, make_parcel, spray, stock


def _repo(session, farm) -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")


def _row(session, farm, now):
    rows = service.stock_rows(_repo(session, farm), now=now, tz="Europe/Paris")
    assert len(rows) == 1
    return rows[0]


def test_rate_per_hectare_times_parcel_area_is_used_and_subtracted(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20", unit="kg")
    # 2 kg/ha on 1.25 ha + 0.75 ha = 4 kg
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="2", unit="kg/ha", key="a")

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert row.used == Decimal("4")
    assert row.on_hand == Decimal("16")
    assert row.sprays == 1
    assert row.not_computable == 0


def test_grams_per_hectare_convert_into_a_kilogram_stock(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="10", unit="kg")
    spray(session, inv.farm, inv.parcels[:1], copper, at=T_APR, value="800", unit="g/ha", key="g")

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert row.used == Decimal("1")
    assert row.on_hand == Decimal("9")


def test_only_done_sprays_of_this_product_this_season_up_to_now_count(session, inv):
    copper, sulphur = inv.products["copper-contact"], inv.products["sulphur-wettable"]
    stock(session, inv.farm, copper, purchased="20")
    one = inv.parcels[:1]  # 1.25 ha
    spray(session, inv.farm, one, copper, at=T_APR, value="2", unit="kg/ha", key="done")
    spray(
        session, inv.farm, one, copper, at=T_APR + timedelta(days=2), value="2",
        unit="kg/ha", key="pending", status="pending",
    )  # fmt: skip
    spray(session, inv.farm, one, sulphur, at=T_APR + timedelta(days=4), value="5", unit="kg/ha",
          key="other")  # fmt: skip
    spray(session, inv.farm, one, copper, at=T_APR + timedelta(days=30), value="2", unit="kg/ha",
          key="later")  # fmt: skip
    spray(session, inv.farm, one, copper, at=datetime(2023, 6, 1, 8, tzinfo=UTC), value="2",
          unit="kg/ha", key="last-season")  # fmt: skip

    row = _row(session, inv.farm, T_APR + timedelta(days=10))

    assert row.used == Decimal("2.5")
    assert row.sprays == 1


def test_a_spray_in_an_incompatible_unit_makes_usage_not_computable(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20", unit="kg")
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="2", unit="kg/ha", key="kg")
    spray(session, inv.farm, inv.parcels, copper, at=T_APR + timedelta(days=3), value="3",
          unit="L/ha", key="litres")  # fmt: skip

    row = _row(session, inv.farm, T_APR + timedelta(days=5))

    assert row.used is None
    assert row.on_hand is None
    assert row.not_computable == 1
    assert row.low is False


def test_low_stock_is_the_farmer_threshold_against_on_hand(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="5", low_stock=Decimal("2"))
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="1.5", unit="kg/ha", key="a")

    assert _row(session, inv.farm, T_APR - timedelta(hours=1)).low is False  # 5 on hand
    assert _row(session, inv.farm, T_APR + timedelta(hours=1)).low is True  # 2 on hand


def test_no_threshold_is_never_low(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="0")

    row = _row(session, inv.farm, T_APR)

    assert row.on_hand == Decimal("0")
    assert row.low is False


def test_another_farms_sprays_and_stock_stay_out(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20")
    other = Farm(name="Other farm")
    session.add(other)
    session.flush()
    stock(session, other, copper, purchased="99")
    spray(session, other, [make_parcel(session, other, 1, "3")], copper, at=T_APR, value="2",
          unit="kg/ha", key="other-farm")  # fmt: skip

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert row.purchased == Decimal("20")
    assert row.used == Decimal("0")


def test_only_the_current_seasons_stock_rows_show(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20", season=2023)

    rows = service.stock_rows(_repo(session, inv.farm), now=T_APR, tz="Europe/Paris")

    assert rows == []


def test_products_off_the_farm_shortlist_are_hidden(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20")
    inv.farm.props = {**(inv.farm.props or {}), "product_ids": []}
    session.flush()

    rows = service.stock_rows(_repo(session, inv.farm), now=T_APR, tz="Europe/Paris")

    assert rows == []


def test_a_two_product_log_counts_each_product_only_once(session, inv):
    copper, sulphur = inv.products["copper-contact"], inv.products["sulphur-wettable"]
    stock(session, inv.farm, copper, purchased="20")
    stock(session, inv.farm, sulphur, purchased="50")
    also_sulphur = logs.QuantityInput("rate", Decimal("5"), "kg/ha", sulphur.id, sulphur.name)
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="2", unit="kg/ha", key="mix",
          extra=(also_sulphur,))  # fmt: skip

    rows = {r.product.id: r for r in service.stock_rows(
        _repo(session, inv.farm), now=T_APR + timedelta(hours=1), tz="Europe/Paris")}  # fmt: skip

    assert (rows[copper.id].used, rows[copper.id].sprays) == (Decimal("4"), 1)
    assert (rows[sulphur.id].used, rows[sulphur.id].sprays) == (Decimal("10"), 1)


def test_a_total_next_to_the_rate_on_one_log_is_not_counted_twice(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20")
    total = logs.QuantityInput("volume", Decimal("4"), "kg", copper.id, copper.name)
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="2", unit="kg/ha", key="both",
          extra=(total,))  # fmt: skip

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert row.used == Decimal("4")
    assert row.not_computable == 0


def test_a_rate_without_per_hectare_unit_is_not_computable(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20")
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="4", unit="kg", key="abs")

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert row.used is None
    assert row.not_computable == 1


def test_a_log_with_only_a_total_of_the_product_is_not_computable(session, inv):
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20")
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="4", unit="kg", key="tot",
          measure="volume")  # fmt: skip

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert row.used is None
    assert row.not_computable == 1


@pytest.mark.parametrize(
    "kind,entered",
    [("irrigate", [logs.EnteredQuantity(Decimal("12.5"), "m3")]), ("observation", [])],
)
def test_a_confirmed_fact_never_consumes_spray_stock(session, inv, kind, entered):
    """Only spray doses count: an irrigation's 12.5 m3 shares the volume measure with a litre
    product stock, yet it is a record, not a product use."""
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20", unit="l")
    logs.record_operation(
        session,
        farm_id=inv.farm.id,
        type=kind,
        parcel_ids=[p.id for p in inv.parcels],
        ts_start=T_APR,
        ts_end=None,
        quantities=entered,
        source=logs.SourceInput("voice", f"{kind}-1", {}, 0.9, logs.RANK_VOICE),
        actor="owner",
        occurred_at=T_APR,
    )

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert (row.used, row.sprays, row.on_hand) == (Decimal("0"), 0, Decimal("20"))


@pytest.mark.parametrize(
    "value,unit",
    [("2", "oz/ha"), ("NaN", "kg/ha"), ("Infinity", "kg/ha")],
    ids=["unknown-unit", "nan-dose", "infinite-dose"],
)
def test_a_dose_without_a_known_factor_never_gives_a_balance(session, inv, value, unit):
    """A dose FarmCore cannot convert exactly makes the season not computable, even beside a
    good dose on the same log and with a low-stock threshold set: no plausible, NaN or infinite
    balance."""
    copper = inv.products["copper-contact"]
    stock(session, inv.farm, copper, purchased="20", low_stock=Decimal("5"))
    bad = logs.QuantityInput("rate", Decimal(value), unit, copper.id, copper.name)
    spray(session, inv.farm, inv.parcels, copper, at=T_APR, value="2", unit="kg/ha", key="mix",
          extra=(bad,))  # fmt: skip

    row = _row(session, inv.farm, T_APR + timedelta(hours=1))

    assert (row.used, row.on_hand, row.low) == (None, None, False)
    assert (row.sprays, row.not_computable) == (1, 1)


def test_two_sessions_confirming_one_pending_spray_move_stock_once(
    settings, committed_session_factory, monkeypatch
):
    """The second confirmation waits on the farm lock, then finds the spray no longer pending."""
    monkeypatch.setattr(ops_seed, "get_settings", lambda: settings)
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    with committed_session_factory() as session:
        farm = core_seed.ensure_default_farm(session, settings)
        parcels = [make_parcel(session, farm, 1, "1.25"), make_parcel(session, farm, 2, "0.75")]
        copper = ops_seed.ensure_products(session)["copper-contact"]
        stock(session, farm, copper, purchased="20")
        pending = logs.create_pending(
            session,
            farm_id=farm.id,
            type="spray",
            parcel_ids=[p.id for p in parcels],
            ts_start=T_APR,
            ts_end=None,
            quantities=[logs.QuantityInput("rate", Decimal("2"), "kg/ha", copper.id)],
            source=logs.SourceInput("voice", "capture-1", {}, 0.9, logs.RANK_VOICE),
            actor="owner",
            occurred_at=T_APR,
        )
        session.commit()
        farm_id, log_id = farm.id, pending.id

    def confirm_in_own_session():
        with committed_session_factory() as other:
            logs.confirm(other, log_id, "owner", T_APR)
            other.commit()

    with committed_session_factory() as first, ThreadPoolExecutor(max_workers=1) as pool:
        logs.confirm(first, log_id, "owner", T_APR)
        second = pool.submit(confirm_in_own_session)
        deadline = time.monotonic() + 8
        while not _waiting_on_advisory_lock(committed_session_factory):
            assert time.monotonic() < deadline, "second confirmation never reached the farm lock"
            time.sleep(0.01)
        first.commit()
        with pytest.raises(ValueError, match="not pending"):
            second.result(timeout=8)

    with committed_session_factory() as session:
        row = _row(session, session.get(Farm, farm_id), T_APR + timedelta(hours=1))
        assert (row.used, row.on_hand, row.sprays) == (Decimal("4"), Decimal("16"), 1)


def _waiting_on_advisory_lock(factory) -> bool:
    with factory() as observer:
        return bool(
            observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event = 'advisory' AND query LIKE '%pg_advisory_xact_lock%'"
                )
            )
        )
