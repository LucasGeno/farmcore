"""The 'Season 2024 complete' demo preset. Its logged sprays are the advisor's own simulated
passes (the run behind the KPI advisor column), so Register, Activity, History, Inventory and KPI
tell one story. French demo farm only; idempotent; labelled demo data."""

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.simulate import season_span
from farmcore.core import seed as core_seed
from farmcore.core.demo import collect_presets
from farmcore.core.manifest import import_string
from farmcore.core.models.canonical import (
    ExtId,
    Farm,
    Log,
    LogSource,
    Parcel,
    Product,
    Quantity,
)
from farmcore.main import create_app
from tests.apps.advisor import scenes

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step
pytestmark = pytest.mark.data

LABEL = "Demo data: advisor-recommended pass"


def _preset(manifests):
    return next(p for p in collect_presets(manifests) if p.key == "season-2024")


def _simulated(settings):
    """The KPI's own run for 2024, from the same station file."""
    from farmcore.apps.kpi import season

    return season.season_results(season.station_file(settings.data_dir), (2024,))[2024].model


def test_preset_targets_the_2024_season_end_and_is_listed_after_season_end(manifests):
    presets = collect_presets(manifests)
    keys = [p.key for p in presets]
    assert keys.index("season-2024") > keys.index("season-end")
    preset = _preset(manifests)
    cfg = AdvisorConfig.for_region_year("FRI12", 2024)
    end = season_span(cfg)[1]
    assert end == date(2024, 7, 8)  # bunch closure of the existing 2024 FRI12 config
    assert preset.clock_target.date() == end
    assert "Demo data" in preset.description and "advisor" in preset.description.lower()
    assert preset.label.startswith("Season 2024 complete")


def test_logs_are_the_advisors_own_passes_on_every_parcel(
    session, settings, manifests, seeded, monkeypatch
):
    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    farm = core_seed.ensure_default_farm(session, settings)
    session.execute(  # start from the preset's own clean slate (scene B adds a 9 May spray)
        Log.__table__.delete().where(Log.farm_id == farm.id)
    )
    preset = _preset(manifests)
    import_string(preset.scene)(session, farm, preset.clock_target)
    session.commit()

    sim = _simulated(settings)
    parcels = session.scalars(select(Parcel).where(Parcel.farm_id == farm.id)).all()
    assert sim.passes == 11 and len(parcels) == 10
    for parcel in parcels:
        rows = session.scalars(
            select(Log)
            .where(Log.parcels.contains(parcel), Log.type == "spray")
            .order_by(Log.ts_start)
        ).all()
        assert [r.ts_start for r in rows] == [p.ts for p in sim.dates]
        assert all(LABEL in r.name for r in rows)
    total = session.scalar(select(func.count()).select_from(Log).where(Log.farm_id == farm.id))
    assert total == 10 * sim.passes

    one = session.scalars(select(Log).where(Log.farm_id == farm.id)).first()
    source = session.scalar(select(LogSource).where(LogSource.log_id == one.id))
    assert source.source_type == "seed" and source.raw_payload["demo_data"] is True
    assert source.raw_payload["label"] == LABEL
    # products and doses come from the shortlist / existing demo doses
    doses = {
        (q.product.name, float(q.value), q.unit)
        for q in session.scalars(
            select(Quantity)
            .join(Log)
            .where(Log.farm_id == farm.id, Quantity.product_id.is_not(None))
        )
    }
    assert doses == {
        ("BOUILLIE BORDELAISE RSR DISPERSS NC", 2.0, "kg/ha"),
        ("MICROTHIOL SPECIAL DISPERSS", 2.06, "kg/ha"),
        ("PAJO", 0.45, "kg/ha"),  # the label max dose, the only PAJO dose in our data
    }
    # the 19 Jun tank-mixed pass carries both products in one log
    tank = [p for p in sim.dates if len(p.products) == 2]
    assert len(tank) == 1


def test_applying_twice_changes_nothing_and_other_farms_are_untouched(
    session, settings, manifests, seeded, monkeypatch
):
    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    farm = core_seed.ensure_default_farm(session, settings)
    preset = _preset(manifests)
    apply = import_string(preset.scene)
    apply(session, farm, preset.clock_target)
    first = sorted(session.scalars(select(Log.id).where(Log.farm_id == farm.id)))
    apply(session, farm, preset.clock_target)
    assert sorted(session.scalars(select(Log.id).where(Log.farm_id == farm.id))) == first

    other = Farm(name="Other country demo farm")
    session.add(other)
    session.flush()
    apply(session, other, preset.clock_target)
    assert not session.scalar(select(func.count()).select_from(Log).where(Log.farm_id == other.id))


@pytest.fixture
def admin_client(settings, session_factory):
    configured = settings.model_copy(update={"admin_token": SecretStr("season-test")})
    app = create_app(configured, session_factory=session_factory)
    with TestClient(app) as client:
        client.cookies.set("admin_token", "season-test")
        yield client


def _apply_preset(client, session, settings, preset="season-2024", action="preset"):
    farm = core_seed.ensure_default_farm(session, settings)
    farm.props = {**(farm.props or {}), "chat": {"telegram_chat_ids": [4242], "push_cursor": 7}}
    session.commit()
    page = client.get("/demo/")
    generation = int(re.search(r'name="generation" value="(\d+)"', page.text)[1])
    response = client.post(
        "/demo/apply",
        data={
            "action": action,
            "preset": preset,
            "farm_id": farm.id,
            "generation": generation,
            "confirm": "yes",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    session.expire_all()
    return farm


def test_the_preset_keeps_the_telegram_binding_and_every_view_tells_the_same_season(
    admin_client, session, settings, seeded, monkeypatch
):
    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    farm = _apply_preset(admin_client, session, settings)
    assert session.get(Farm, farm.id).props["chat"] == {
        "telegram_chat_ids": [4242],
        "push_cursor": 7,
    }
    demo_page = admin_client.get("/demo/").text
    assert demo_page.index("Season end · 15 May") < demo_page.index("Season 2024 complete")
    sim = _simulated(settings)
    parcels = session.scalars(select(Parcel).where(Parcel.farm_id == farm.id)).all()
    area = sum(float(p.area_ha) for p in parcels)

    # Register: one row per parcel per treatment (a tank mix is two) minus the one copper part
    # its label no longer allows (19 Jun, logged sulphur only); CSV and JSON agree
    rows = admin_client.get("/operations/register.json").json()
    assert len(rows) == 10 * (sim.treatments - 1)
    csv_lines = admin_client.get("/operations/register.csv").text.strip().splitlines()
    assert len(csv_lines) == 10 * (sim.treatments - 1) + 1
    # label caps: copper stops at its 5 applications, PAJO takes the next downy pass, and the
    # pass neither allows leaves its downy product out: no label warning anywhere
    warns = {c["label"] for r in rows for c in r["checks"] if c["status"] == "warn"}
    assert warns == set()

    # KPI advisor passes equal the passes logged on each parcel
    kpi = admin_client.get("/kpi/kpis.json").json()
    year = next(y for y in kpi["years"] if y["year"] == 2024)
    p5 = next(p for p in parcels if p.alias == "P5")
    logged = session.scalars(
        select(Log.ts_start).where(Log.parcels.contains(p5), Log.type == "spray")
    ).all()
    assert year["model"]["passes"] == sim.passes == len(set(logged)) == 11

    # parcel History: the whole season for P5, one dose per logged treatment
    history = admin_client.get(f"/advisor/parcels/{p5.id}/history").text
    assert history.count("kg/ha") == sim.treatments - 1  # the omitted copper part is not logged
    assert "Tue 9 Apr 06:00" in history and "Fri 21 Jun 06:00" in history

    # Activity: one grouped spray per pass, labelled demo data
    activity = admin_client.get("/activity/").text
    assert activity.count("Spray logged on 10 parcels") >= 10
    assert "Source: Demo data" in " ".join(re.sub(r"<[^>]+>", " ", activity).split())

    # Inventory usage: dose x passes x area, from the logged sprays
    inventory = " ".join(re.sub(r"<[^>]+>", " ", admin_client.get("/inventory/").text).split())
    p5_counts = dict(
        session.execute(
            select(Quantity.label, func.count())
            .join(Log, Log.id == Quantity.log_id)
            .where(Log.parcels.contains(p5), Quantity.product_id.is_not(None))
            .group_by(Quantity.label)
        ).all()
    )
    doses = {
        "BOUILLIE BORDELAISE RSR DISPERSS NC": 2,
        "MICROTHIOL SPECIAL DISPERSS": 2.06,
        "PAJO": 0.45,
    }

    def kg(value: float) -> str:
        return f"{value:.4f}".rstrip("0").rstrip(".")

    assert set(p5_counts) == set(doses)
    for name, count in p5_counts.items():
        used = f"Used (from {10 * count} logged sprays) {kg(doses[name] * count * area)} kg"
        assert used in inventory, name


def test_choose_product_respects_label_applications_and_interval():
    from datetime import date

    from farmcore.apps.advisor.demo import choose_product
    from farmcore.apps.operations.seed import SHORTLIST

    downy = [p for p in SHORTLIST if "downy" in p.targets]
    assert [p.key for p in downy] == ["copper-contact", "cymoxanil-penetrant"]
    used: dict[str, list[date]] = {}
    day = date(2024, 4, 1)
    picks = []
    for step_days in (0, 18, 7, 10, 7, 25, 4, 30, 8):
        day = date.fromordinal(day.toordinal() + step_days)
        product = choose_product(downy, day, used)
        key = product.key if product else None
        if key:
            used.setdefault(key, []).append(day)
        picks.append(key)
    # copper 5x, then PAJO (cap 2, min 7 days between); None when nothing allows the pass
    assert picks[:5] == ["copper-contact"] * 5
    assert picks[5] == "cymoxanil-penetrant"
    assert picks[6] is None  # 4 days after PAJO: its interval blocks it and copper is capped
    assert picks[7] == "cymoxanil-penetrant"  # 30 days later PAJO is allowed (2nd of 2)
    assert picks[8] is None  # both products have reached their cap


def test_per_product_counts_and_kpi_passes_stay_equal(
    session, settings, manifests, seeded, monkeypatch
):
    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    farm = core_seed.ensure_default_farm(session, settings)
    session.execute(Log.__table__.delete().where(Log.farm_id == farm.id))
    preset = _preset(manifests)
    import_string(preset.scene)(session, farm, preset.clock_target)
    session.commit()
    p5 = session.scalar(select(Parcel).where(Parcel.alias == "P5"))
    names = [
        q.product.name
        for q in session.scalars(
            select(Quantity)
            .join(Log)
            .where(Log.parcels.contains(p5), Quantity.product_id.is_not(None))
        )
    ]
    from collections import Counter

    assert Counter(names) == {
        "BOUILLIE BORDELAISE RSR DISPERSS NC": 5,
        "PAJO": 1,
        "MICROTHIOL SPECIAL DISPERSS": 5,
    }
    passes = len(set(session.scalars(select(Log.ts_start).where(Log.parcels.contains(p5)))))
    assert passes == _simulated(settings).passes == 11


def test_season_purchases_cover_the_presets_usage_in_whole_packs(
    admin_client, session, settings, seeded, monkeypatch
):
    from decimal import Decimal

    from farmcore.apps.inventory.models import InventoryStock
    from farmcore.apps.inventory.seed import DEMO_STOCK

    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    farm = _apply_preset(admin_client, session, settings)
    page = " ".join(re.sub(r"<[^>]+>", " ", admin_client.get("/inventory/").text).split())
    assert "Low stock" not in page
    assert "Demo data: season purchases" in page
    base = {d.product: Decimal(d.purchased) for d in DEMO_STOCK}
    rows = session.scalars(select(InventoryStock).where(InventoryStock.farm_id == farm.id)).all()
    assert rows
    for row in rows:
        marker = row.props["season_purchases"]
        pack = Decimal(marker["pack"])
        assert row.purchased == Decimal(marker["base_purchased"]) + pack * marker["packs_added"]
        assert marker["label"] == "Demo data: season purchases"
    for amount in base.values():
        assert amount in {Decimal(r.props["season_purchases"]["base_purchased"]) for r in rows}
    before = {r.id: r.purchased for r in rows}
    _apply_preset(admin_client, session, settings)  # idempotent
    after = {
        r.id: r.purchased
        for r in session.scalars(select(InventoryStock).where(InventoryStock.farm_id == farm.id))
    }
    assert after == before


def test_history_says_not_recorded_for_the_demo_season_instead_of_none(
    admin_client, session, settings, seeded, monkeypatch
):
    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    _apply_preset(admin_client, session, settings)
    p5 = session.scalar(select(Parcel).where(Parcel.alias == "P5"))
    text = " ".join(
        re.sub(r"<[^>]+>", " ", admin_client.get(f"/advisor/parcels/{p5.id}/history").text).split()
    )
    note = "Not recorded for this demo season (sprays logged from the advisor's simulated passes)"
    assert text.count(note) == 2
    assert "No protection period this season" not in text
    assert "No advice change this season" not in text


@pytest.mark.parametrize("preset", ["demo-start", "live-loop"])
def test_reset_after_the_season_preset_puts_stock_back_to_the_base_seed(
    admin_client, session, settings, seeded, monkeypatch, preset
):
    from decimal import Decimal

    from farmcore.apps.inventory.models import InventoryStock
    from farmcore.apps.inventory.seed import DEMO_STOCK

    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    farm = _apply_preset(admin_client, session, settings)
    topped = session.scalars(select(InventoryStock).where(InventoryStock.farm_id == farm.id)).all()
    assert any("season_purchases" in r.props for r in topped)
    _apply_preset(admin_client, session, settings, preset=preset, action="reset")
    rows = session.scalars(select(InventoryStock).where(InventoryStock.farm_id == farm.id)).all()
    base = {d.purchased for d in DEMO_STOCK}
    assert {r.purchased for r in rows} == {Decimal(b) for b in base}
    assert all("season_purchases" not in r.props for r in rows)
    page = " ".join(re.sub(r"<[^>]+>", " ", admin_client.get("/inventory/").text).split())
    assert "Demo data: season purchases" not in page
    ext = session.scalars(select(ExtId).where(ExtId.key.startswith("season-2024-purchases:"))).all()
    assert ext == []


def test_no_pass_breaks_a_label_and_the_sixth_copper_is_left_out_with_a_note(
    admin_client, session, settings, seeded, monkeypatch
):

    from farmcore.apps.operations.checks import Label
    from farmcore.apps.operations.seed import SHORTLIST

    monkeypatch.setattr("farmcore.apps.advisor.demo.get_settings", lambda: settings)
    seeded("B")
    farm = _apply_preset(admin_client, session, settings)
    p5 = session.scalar(select(Parcel).where(Parcel.alias == "P5"))
    note = (
        "Demo data: copper omitted, label maximum of 5 applications reached and PAJO interval "
        "not met"
    )
    tank = next(
        lg
        for lg in session.scalars(select(Log).where(Log.parcels.contains(p5)))
        if lg.notes == note
    )
    names = [
        q.product.name for q in session.scalars(select(Quantity).where(Quantity.log_id == tank.id))
    ]
    assert names == ["MICROTHIOL SPECIAL DISPERSS"]  # sulphur part only
    for item in SHORTLIST:
        label = Label.from_props(item.label)
        count = len(
            {
                lg.ts_start
                for lg in session.scalars(
                    select(Log)
                    .join(Quantity)
                    .join(Product, Product.id == Quantity.product_id)
                    .where(
                        Log.farm_id == farm.id, Log.parcels.contains(p5), Product.name == item.name
                    )
                )
            }
        )
        if label.max_applications is not None:
            assert count <= label.max_applications, item.key
    rows = admin_client.get("/operations/register.json").json()
    assert not [c for r in rows for c in r["checks"] if c["status"] == "warn"]
    history = admin_client.get(f"/advisor/parcels/{p5.id}/history").text
    assert "copper omitted, label maximum of 5 applications reached" in history
