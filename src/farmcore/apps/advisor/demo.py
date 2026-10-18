"""Advisor demo presets: the live loop's earlier copper sprays, and a complete 2024 season
whose logged sprays are the advisor's own simulated passes. Rules and weather are unchanged."""

from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.advisor.engine.config import AdvisorConfig
from farmcore.apps.advisor.engine.simulate import season_span, simulate
from farmcore.apps.advisor.engine.types import HourlyObs
from farmcore.apps.operations.checks import Label
from farmcore.apps.operations.isoxml_demo import SULPHUR_MG_M2
from farmcore.apps.operations.seed import (
    PRODUCT_SOURCE,
    SCENE_B_DOSE,
    SCENE_B_PRODUCT,
    SCENE_B_UNIT,
    SHORTLIST,
    DemoProduct,
    _map_log,
)
from farmcore.apps.stations.connector import FILE as STATION_FILE
from farmcore.apps.stations.connector import read_hours
from farmcore.core import ext_ids, logs
from farmcore.core.access import DEMO_SOURCE
from farmcore.core.demo import Preset
from farmcore.core.models.canonical import Farm, Log, Parcel, Product
from farmcore.settings import get_settings

UNTREATED = ("P5", "P6", "P7")
SEED_TIME = datetime(2024, 4, 9, 8, tzinfo=ZoneInfo("Europe/Paris"))
LIVE_LOOP = Preset(
    key="live-loop",
    label="Live loop: 3 parcels need treatment",
    description=(
        "French demo farm only: seven demo data copper sprays on 9 Apr at 08:00. "
        "At 10 Apr 06:00, P5 Les Granges, P6 Peyrot and P7 La Fontaine need treatment."
    ),
    clock_target=datetime(2024, 4, 10, 6, tzinfo=ZoneInfo("Europe/Paris")),
    scene="farmcore.apps.advisor.demo:apply_live_loop",
)


def apply_live_loop(session: Session, farm: Farm, now: datetime) -> None:
    """Idempotently add seven labelled demo sprays before the 10 Apr infection-risk scene."""
    demo_farm = ext_ids.lookup(session, DEMO_SOURCE, "chateau")
    if not isinstance(demo_farm, Farm) or farm.id != demo_farm.id:
        return
    copper = ext_ids.lookup(session, PRODUCT_SOURCE, SCENE_B_PRODUCT)
    if not isinstance(copper, Product):
        raise ValueError("Seed the demo product shortlist before applying the live-loop scene")
    parcels = session.scalars(
        select(Parcel)
        .where(Parcel.farm_id == farm.id, Parcel.alias.not_in(UNTREATED))
        .order_by(Parcel.id)
    ).all()
    for parcel in parcels:
        key = f"live-loop:{farm.id}:2024-04-09:{parcel.alias}"
        logs.record_operation(
            session,
            farm_id=farm.id,
            type="spray",
            parcel_ids=[parcel.id],
            ts_start=SEED_TIME,
            ts_end=None,
            quantities=[
                logs.QuantityInput("rate", SCENE_B_DOSE, SCENE_B_UNIT, copper.id, copper.name)
            ],
            source=logs.SourceInput(
                "seed", key, {"scene": "live-loop", "demo_data": True}, None, logs.RANK_SEED
            ),
            actor="seed",
            occurred_at=SEED_TIME,
            name="Copper spray 09 Apr 2024 (live-loop demo data)",
            source_app="advisor",
        )


SEASON_REGION, SEASON_YEAR = "FRI12", 2024
PASS_LABEL = "Demo data: advisor-recommended pass"
# Doses already in our demo data: copper is the voice-note / scene B dose, sulphur the demo
# sprayer file's setpoint (206 mg/m² = 2.06 kg/ha). A product with no demo dose (PAJO) is applied
# at its E-Phy label maximum, the only dose for it in our data. Product keys: operations SHORTLIST.
DEMO_DOSES = {
    SCENE_B_PRODUCT: SCENE_B_DOSE,
    "sulphur-wettable": Decimal(SULPHUR_MG_M2) / 100,
}
_SEASON_END = season_span(AdvisorConfig.for_region_year(SEASON_REGION, SEASON_YEAR))[1]
SEASON_2024 = Preset(
    key="season-2024",
    label=f"Season 2024 complete · {_SEASON_END.day} {_SEASON_END:%b} 06:00",
    description=(
        "Demo data, French demo farm only: the whole 2024 season replayed to its end "
        f"({_SEASON_END.day} {_SEASON_END:%B}, the season config's bunch closure), with every "
        "spray the advisor itself recommended over the season logged on all ten parcels as "
        "demo data (the same simulation as the KPI advisor column). A product its label no "
        "longer allows is left out of that pass, and a pass left with no product is not "
        "logged, so no label limit is broken. For history, export, "
        "Inventory and Season & impact; not for the live loop."
    ),
    clock_target=datetime.combine(_SEASON_END, time(6), tzinfo=ZoneInfo("Europe/Paris")),
    scene="farmcore.apps.advisor.demo:apply_season_2024",
)


def _season_series() -> list[HourlyObs]:
    """The KPI's own input: the open station file, hourly."""
    path: Path = get_settings().data_dir / STATION_FILE
    return [
        HourlyObs(
            ts=h.ts,
            rr1_mm=h.values["rr1_mm"],
            t_c=h.values["t_c"],
            rh_pct=h.values["rh_pct"],
            ff_kmh=h.values["ff_kmh"],
        )
        for h in read_hours(path)
    ]


def _dose(product: DemoProduct) -> Decimal:
    if product.key in DEMO_DOSES:
        return DEMO_DOSES[product.key]
    return Decimal(str(product.label["label_max_dose_kg_ha"]))


SHORT_NAMES = {"copper-contact": "copper", "cymoxanil-penetrant": "PAJO"}


def blocked_by(product: DemoProduct, day: date, used: dict[str, list[date]]) -> str | None:
    """Why the product's stored label limits (E-Phy values from the seed) forbid a pass on `day`:
    maximum applications a season, minimum days between applications or maximum dose."""
    label = Label.from_props(product.label)
    before = used.get(product.key, [])
    if label.max_applications is not None and len(before) >= label.max_applications:
        return f"label maximum of {label.max_applications} applications reached"
    gap = label.min_interval_days
    if gap is not None and before and (day - before[-1]).days < gap:
        return f"{SHORT_NAMES.get(product.key, product.key)} interval not met"
    if label.max_dose_kg_ha is not None and _dose(product) > label.max_dose_kg_ha:
        return "label maximum dose exceeded"
    return None


def choose_product(
    candidates: Sequence[DemoProduct], day: date, used: dict[str, list[date]]
) -> DemoProduct | None:
    """The first shortlist product whose label limits allow a pass on `day`; None when none does
    (the pass is then logged without that product, never against its label)."""
    return next((p for p in candidates if blocked_by(p, day, used) is None), None)


def apply_season_2024(session: Session, farm: Farm, now: datetime) -> None:
    """Idempotently log the advisor's simulated 2024 passes on every parcel of the demo farm.

    The passes are `simulate()` over the season span with the 2024 FRI12 config, exactly the
    KPI advisor run; each is one spray log per parcel at the pass's own window start, keyed
    `season-2024:<farm>:<day>:<alias>`. The product of each pass is the first shortlist product
    for its disease that its label limits allow (`choose_product`). Its purchases are topped up
    in whole packs to cover its own usage (`inventory.demo_purchases`).
    """
    demo_farm = ext_ids.lookup(session, DEMO_SOURCE, "chateau")
    if not isinstance(demo_farm, Farm) or farm.id != demo_farm.id:
        return
    products = {}
    for item in SHORTLIST:
        product = ext_ids.lookup(session, PRODUCT_SOURCE, item.key)
        if not isinstance(product, Product):
            raise ValueError("Seed the demo product shortlist before applying the season preset")
        products[item.key] = product
    cfg = AdvisorConfig.for_region_year(SEASON_REGION, SEASON_YEAR)
    result = simulate(_season_series(), cfg, *season_span(cfg))
    disease = {
        cfg.default_downy_product.name: cfg.default_downy_product.targets[0],
        cfg.default_powdery_product.name: cfg.default_powdery_product.targets[0],
    }
    parcels = session.scalars(
        select(Parcel).where(Parcel.farm_id == farm.id).order_by(Parcel.id)
    ).all()
    area = sum((p.area_ha for p in parcels), Decimal(0))
    used: dict[str, list[date]] = {}
    usage: dict[str, Decimal] = {}
    for sim_pass in result.dates:
        chosen = []
        omitted: list[str] = []
        for name in sim_pass.products:
            candidates = [p for p in SHORTLIST if disease[name] in p.targets]
            product = choose_product(candidates, sim_pass.day, used)
            if product is None:
                omitted.append(SHORT_NAMES.get(candidates[0].key, candidates[0].key))
                omitted.extend(
                    r for c in candidates if (r := blocked_by(c, sim_pass.day, used)) is not None
                )
                continue
            used.setdefault(product.key, []).append(sim_pass.day)
            usage[product.key] = usage.get(product.key, Decimal(0)) + _dose(product) * area
            chosen.append(product)
        if not chosen:
            continue  # nothing left that its label allows: the pass is not logged
        note = f"Demo data: {omitted[0]} omitted, {' and '.join(omitted[1:])}" if omitted else ""
        quantities = [
            logs.QuantityInput("rate", _dose(p), "kg/ha", products[p.key].id, products[p.key].name)
            for p in chosen
        ]
        for parcel in parcels:
            key = f"season-2024:{farm.id}:{sim_pass.day:%Y-%m-%d}:{parcel.alias}"
            if isinstance(ext_ids.lookup(session, DEMO_SOURCE, key), Log):
                continue
            log, _ = logs.record_operation(
                session,
                farm_id=farm.id,
                type="spray",
                parcel_ids=[parcel.id],
                ts_start=sim_pass.ts,
                ts_end=None,
                quantities=quantities,
                source=logs.SourceInput(
                    "seed",
                    key,
                    {
                        "scene": "season-2024",
                        "demo_data": True,
                        "label": PASS_LABEL,
                        "decision": sim_pass.decision,
                        "advised_at": sim_pass.advised_at.isoformat(),
                        "omitted": note,
                    },
                    None,
                    logs.RANK_SEED,
                ),
                actor="seed",
                occurred_at=sim_pass.ts,
                name=f"{PASS_LABEL} · {' + '.join(p.key.split('-')[0] for p in chosen)} "
                f"{sim_pass.day:%d %b %Y}",
                source_app="advisor",
            )
            if note:
                log.notes = note
            _map_log(session, key, log.id)
    from farmcore.apps.inventory import demo_purchases  # optional app: its own demo rows only

    demo_purchases.apply(session, farm, usage)
