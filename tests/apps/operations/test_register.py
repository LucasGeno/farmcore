"""Reg. 2023/564 spray register rows: one per done spray log × parcel × product, clock-bounded."""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from farmcore.apps.operations import register
from farmcore.apps.operations import seed as ops_seed
from farmcore.core import logs
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.models.canonical import AccessLog, Quantity

# 2024-04-10 08:00 Europe/Paris (CEST) = 06:00 UTC. Synthetic values.
T_SPRAY = datetime(2024, 4, 10, 6, 0, tzinfo=UTC)
NOW = datetime(2024, 4, 12, 6, 0, tzinfo=UTC)
VOICE = logs.SourceInput("voice", "tg:1", {"transcript": "synthetic"}, 0.9, logs.RANK_VOICE)


def _repo(session, farm) -> ScopedRepository:
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "owner", "test")


def _spray(session, demo, products, *, parcels=None, status="done", ts=T_SPRAY, **kw):
    quantities = [
        logs.QuantityInput("rate", Decimal(dose), "kg/ha", product_id=products[key].id)
        for key, dose in kw.pop("doses", [("copper-contact", "2"), ("sulphur-wettable", "3")])
    ]
    write = logs.create_pending if status == "pending" else logs.record_operation
    result = write(
        session,
        farm_id=demo.farm.id,
        type="spray",
        parcel_ids=[p.id for p in (parcels or demo.parcels)],
        ts_start=ts,
        ts_end=None,
        quantities=quantities,
        source=kw.pop("source", VOICE),
        actor="owner",
        occurred_at=ts,
    )
    log = result if status == "pending" else result[0]
    for k, v in kw.items():
        setattr(log, k, v)
    session.flush()
    return log


def test_done_log_on_two_parcels_with_two_products_gives_four_full_rows(session, demo, tmp_path):
    products = ops_seed.ensure_products(session)
    log = _spray(session, demo, products)

    rows = register.register_rows(_repo(session, demo.farm), NOW)

    assert len(rows) == 4
    assert list(rows[0]) == list(register.FIELDS)
    first = rows[0]
    assert first == {
        "product_name": "BOUILLIE BORDELAISE RSR DISPERSS NC",
        "authorisation_no": "9800474",
        "authorisation_placeholder": False,
        "active_substance": "copper",
        "date": "2024-04-10",
        "start_time": "08:00",
        "dose": "2",
        "dose_unit": "kg/ha",
        "iacs_parcel_id": "FR-TEST-1",
        "parcel_alias": "P1",
        "area_treated_ha": "1.25",
        "crop_name": "Vigne",
        "crop_eppo": "VITVI",
        "bbch": 13,
        "bbch_source": "config",
        "log_id": log.id,
    }
    assert [(r["parcel_alias"], r["active_substance"], r["dose"]) for r in rows] == [
        ("P1", "copper", "2"),
        ("P1", "sulphur", "3"),
        ("P2", "copper", "2"),
        ("P2", "sulphur", "3"),
    ]
    # Parcel 2 has no crop props: the crop defaults to grapevine (VITVI).
    assert (rows[2]["crop_name"], rows[2]["crop_eppo"], rows[2]["area_treated_ha"]) == (
        "",
        "VITVI",
        "0.80",
    )


def test_pending_log_is_not_in_the_register_and_a_done_one_is(session, demo):
    products = ops_seed.ensure_products(session)
    pending = _spray(session, demo, products, status="pending", parcels=demo.parcels[:1])
    repo = _repo(session, demo.farm)

    assert register.register_rows(repo, NOW) == []

    logs.confirm(session, pending.id, "owner", T_SPRAY)
    assert {r["log_id"] for r in register.register_rows(repo, NOW)} == {pending.id}


def test_register_is_bounded_by_the_clock_and_only_lists_sprays(session, demo):
    products = ops_seed.ensure_products(session)
    _spray(session, demo, products, ts=datetime(2024, 4, 20, 6, 0, tzinfo=UTC))

    assert register.register_rows(_repo(session, demo.farm), NOW) == []


def test_bbch_from_the_log_wins_and_log_props_override_the_treated_area(session, demo):
    products = ops_seed.ensure_products(session)
    _spray(
        session,
        demo,
        products,
        parcels=demo.parcels[:1],
        doses=[("copper-contact", "2")],
        bbch=15,
        props={"area_treated_ha": {str(demo.parcels[0].id): "0.5"}},
    )

    (row,) = register.register_rows(_repo(session, demo.farm), NOW)

    assert (row["bbch"], row["bbch_source"], row["area_treated_ha"]) == (15, "log", "0.5")


def test_bbch_outside_the_configured_years_is_blank(session, demo):
    products = ops_seed.ensure_products(session)
    ts = datetime(2019, 5, 9, 6, 0, tzinfo=UTC)
    _spray(
        session, demo, products, parcels=demo.parcels[:1], doses=[("copper-contact", "2")], ts=ts
    )

    (row,) = register.register_rows(_repo(session, demo.farm), NOW)

    assert (row["bbch"], row["bbch_source"]) == (None, "none")


def test_register_reads_go_through_the_repo_under_register_read(session, demo):
    products = ops_seed.ensure_products(session)
    _spray(session, demo, products)

    register.register_rows(_repo(session, demo.farm), NOW)

    scopes = {(a.resource, a.scope) for a in session.scalars(select(AccessLog))}
    assert ("log", "register:read") in scopes
    assert ("parcel", "register:read") in scopes
    assert {s for _, s in scopes} == {"register:read"}


def test_csv_has_the_stable_header_and_the_e_phy_number(session, demo):
    products = ops_seed.ensure_products(session)
    _spray(session, demo, products, parcels=demo.parcels[:1], doses=[("copper-contact", "2")])

    text = register.to_csv(register.register_rows(_repo(session, demo.farm), NOW))

    header, row, *rest = text.splitlines()
    assert rest == []
    # The EU columns in their order, then one `checks` column.
    assert header == (
        "product_name,authorisation_no,authorisation_placeholder,active_substance,date,"
        "start_time,dose,dose_unit,iacs_parcel_id,parcel_alias,area_treated_ha,crop_name,"
        "crop_eppo,bbch,bbch_source,log_id,checks"
    )
    assert (*register.FIELDS, "checks") == register.CSV_FIELDS
    assert row.startswith("BOUILLIE BORDELAISE RSR DISPERSS NC,9800474,,copper,2024-04-10,08:00,2,")
    assert row.endswith(",")  # rows read without checks leave the cell blank


def test_rows_with_checks_carry_the_spray_checks_and_the_csv_says_them_in_words(session, demo):
    products = ops_seed.ensure_products(session)
    log = _spray(session, demo, products)

    rows = register.register_rows(_repo(session, demo.farm), NOW, checks=True)

    assert all(list(r) == list(register.CSV_FIELDS) for r in rows)
    assert all(r["checks"] == rows[0]["checks"] for r in rows)  # one list per spray
    got = {c["key"]: (c["status"], c["label"]) for c in rows[0]["checks"]}
    copper, sulphur = products["copper-contact"].id, products["sulphur-wettable"].id
    assert got == {
        "copper": ("ok", "Copper 0.4 of 4 kg Cu/ha this year"),
        f"applications:{copper}": ("ok", "1 of 5 copper applications this season"),
        f"applications:{sulphur}": ("ok", "1 of 8 sulphur applications this season"),
        f"dose:{copper}": ("ok", "Copper dose 2 kg/ha, label max 3.75"),
        f"dose:{sulphur}": ("ok", "Sulphur dose 3 kg/ha, label max 12.5"),
        "phi": ("info", "Harvest not before 13 Apr"),
    }
    first = next(csv.DictReader(io.StringIO(register.to_csv(rows))))
    assert first["log_id"] == str(log.id)
    assert first["checks"] == (
        "OK: Copper 0.4 of 4 kg Cu/ha this year; "
        "OK: 1 of 5 copper applications this season; "
        "OK: 1 of 8 sulphur applications this season; "
        "OK: Copper dose 2 kg/ha, label max 3.75; "
        "OK: Sulphur dose 3 kg/ha, label max 12.5; "
        "Note: Harvest not before 13 Apr"
    )


def test_checks_count_earlier_confirmed_sprays_only(session, demo):
    products = ops_seed.ensure_products(session)
    p1 = demo.parcels[:1]
    copper_only = [("copper-contact", "10")]  # 2 kg Cu/ha each
    _spray(
        session,
        demo,
        products,
        parcels=p1,
        doses=copper_only,
        ts=datetime(2024, 4, 1, 6, tzinfo=UTC),
    )
    _spray(
        session,
        demo,
        products,
        parcels=p1,
        doses=copper_only,
        ts=datetime(2024, 4, 2, 6, tzinfo=UTC),
        source=replace(VOICE, external_key="tg:2"),
        status="pending",
    )
    _spray(
        session,
        demo,
        products,
        parcels=p1,
        doses=copper_only,
        ts=datetime(2024, 4, 3, 6, tzinfo=UTC),
        source=replace(VOICE, external_key="tg:3"),
    )
    third = _spray(
        session,
        demo,
        products,
        parcels=p1,
        doses=[("copper-contact", "2")],
        source=replace(VOICE, external_key="tg:4"),
    )
    _spray(
        session,
        demo,
        products,
        parcels=p1,
        doses=copper_only,
        ts=datetime(2024, 4, 11, 6, tzinfo=UTC),
        source=replace(VOICE, external_key="tg:5"),
    )

    rows = register.register_rows(_repo(session, demo.farm), NOW, checks=True)

    row = next(r for r in rows if r["log_id"] == third.id)
    got = {c["key"]: c for c in row["checks"]}
    # 2 + 2 + 0.4: the pending spray and the 11 Apr one (after this spray) do not count.
    assert (got["copper"]["status"], got["copper"]["label"]) == (
        "warn",
        "Copper 4.4 of 4 kg Cu/ha this year",
    )
    assert got[f"applications:{products['copper-contact'].id}"]["label"] == (
        "3 of 5 copper applications this season"
    )
    over = next(r for r in rows if r["log_id"] != third.id and r["date"] == "2024-04-01")
    assert {c["key"]: c["status"] for c in over["checks"]}[
        f"dose:{products['copper-contact'].id}"
    ] == "warn"  # 10 kg/ha is above 3.75


def test_parcels_sort_by_alias_number_so_p10_follows_p9(session, demo, add_parcel):
    p10 = add_parcel(10)
    products = ops_seed.ensure_products(session)
    _spray(session, demo, products, parcels=[p10, *demo.parcels], doses=[("copper-contact", "2")])

    rows = register.register_rows(_repo(session, demo.farm), NOW)

    assert [r["parcel_alias"] for r in rows] == ["P1", "P2", "P10"]


def test_alias_key_orders_numbered_aliases_numerically_and_others_after():
    aliases = ["P10", "Home block", "P2", "P1"]
    assert sorted(aliases, key=register.alias_key) == ["P1", "P2", "P10", "Home block"]


def test_unconvertible_copper_rate_warns_and_keeps_later_known_sum(session, demo):
    products = ops_seed.ensure_products(session)
    earlier = _spray(
        session,
        demo,
        products,
        parcels=demo.parcels[:1],
        doses=[("copper-contact", "2")],
        ts=datetime(2024, 4, 1, 6, tzinfo=UTC),
    )
    q = session.scalar(select(Quantity).where(Quantity.log_id == earlier.id))
    q.unit = "l/ha"  # no density: cannot convert the actual recorded 2 L/ha to kg/ha
    later = _spray(
        session,
        demo,
        products,
        parcels=demo.parcels[:1],
        doses=[("copper-contact", "2")],
        source=replace(VOICE, external_key="tg:2"),
    )
    session.flush()
    rows = register.register_rows(_repo(session, demo.farm), NOW, checks=True)
    cu = {r["log_id"]: next(c for c in r["checks"] if c["key"] == "copper") for r in rows}
    assert cu[earlier.id]["status"] == "warn"
    assert cu[later.id]["status"] == "warn"
    assert cu[later.id]["label"] == "Copper at least 0.4 kg Cu/ha this year (incomplete)"
    assert "Warning: Copper at least 0.4" in register.to_csv(rows)


@pytest.mark.parametrize("second_unit", ["kg/ha", "g/ha", "l/ha"])
def test_register_checks_combine_same_product_rates_only(session, demo, second_unit):
    products = ops_seed.ensure_products(session)
    log = _spray(
        session,
        demo,
        products,
        parcels=demo.parcels[:1],
        doses=[("copper-contact", "2"), ("copper-contact", "2")],
    )
    quantities = session.scalars(
        select(Quantity).where(Quantity.log_id == log.id).order_by(Quantity.id)
    ).all()
    quantities[1].unit = second_unit
    quantities[1].value = Decimal("2000") if second_unit == "g/ha" else Decimal("2")
    session.add(
        Quantity(
            log_id=log.id,
            measure="volume",
            value=Decimal("1000"),
            unit="kg/ha",
            product_id=products["copper-contact"].id,
            label="Tank volume",
        )
    )
    session.flush()
    rows = register.register_rows(_repo(session, demo.farm), NOW, checks=True)
    found = {c["key"]: c for c in rows[0]["checks"]}
    dose = found[f"dose:{products['copper-contact'].id}"]
    assert dose["status"] == "warn"
    if second_unit == "l/ha":
        assert "incomplete" in dose["label"] and "at least 2" in dose["label"]
    else:
        assert dose["label"] == "Copper dose 4 kg/ha, above the label max 3.75"
        assert found["copper"]["label"] == "Copper 0.8 of 4 kg Cu/ha this year"
    assert found[f"applications:{products['copper-contact'].id}"]["label"].startswith("1 of 5")


def test_only_labelled_rates_are_products_without_catalogue_or_props(session, demo):
    products = ops_seed.ensure_products(session)
    log = _spray(session, demo, products, parcels=demo.parcels[:1], doses=[])
    for measure, label in [
        ("rate", "Mystery"),
        ("volume", "Water"),
        ("area", "Area"),
        ("rate", ""),
        ("rate", "  "),
    ]:
        session.add(
            Quantity(log_id=log.id, measure=measure, label=label, value=Decimal("2"), unit="kg/ha")
        )
    session.flush()
    rows = register.register_rows(_repo(session, demo.farm), NOW)
    assert [(r["product_name"], r["dose"], r["dose_unit"]) for r in rows] == [
        ("Mystery", "2", "kg/ha")
    ]


def test_register_interval_reaches_december_without_counting_it_in_january(session, demo):
    products = ops_seed.ensure_products(session)
    pajo = next(p for p in products.values() if p.active_substance == "cymoxanil")
    for at in [datetime(2023, 12, 29, 6, tzinfo=UTC), datetime(2024, 1, 2, 6, tzinfo=UTC)]:
        logs.record_operation(
            session,
            farm_id=demo.farm.id,
            type="spray",
            parcel_ids=[demo.parcels[0].id],
            ts_start=at,
            ts_end=None,
            quantities=[logs.QuantityInput("rate", Decimal("0.4"), "kg/ha", pajo.id)],
            source=logs.SourceInput("voice", str(at), {}, 0.9, logs.RANK_VOICE),
            actor="test",
            occurred_at=at,
        )
    rows = register.register_rows(_repo(session, demo.farm), NOW, checks=True)
    january = next(r for r in rows if r["date"] == "2024-01-02")
    found = {c["key"]: c for c in january["checks"]}
    assert (
        found[f"interval:{pajo.id}"]["label"]
        == "4 days since the last cymoxanil spray, label min 7"
    )
    assert found[f"interval:{pajo.id}"]["status"] == "warn"
    assert found[f"applications:{pajo.id}"]["label"] == "1 of 2 cymoxanil applications this season"


def test_register_warns_for_known_copper_identity_without_composition(session, demo):
    products = ops_seed.ensure_products(session)
    copper = products["copper-contact"]
    copper.props = {k: v for k, v in copper.props.items() if k != "cu_fraction"}
    _spray(session, demo, products, parcels=demo.parcels[:1], doses=[("copper-contact", "2")])
    rows = register.register_rows(_repo(session, demo.farm), NOW, checks=True)
    check = next(c for c in rows[0]["checks"] if c["key"] == "copper")
    assert check["status"] == "warn"
    assert check["label"] == "Copper at least 0 kg Cu/ha this year (incomplete)"
    assert "composition" in check["detail"]
