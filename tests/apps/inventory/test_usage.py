"""Season usage is computed from logged sprays (never entered twice); on hand = purchased − used."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from farmcore.apps.inventory import service
from farmcore.core import logs
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
