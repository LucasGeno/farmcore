"""Operations seed: the demo product shortlist, and with `--scene B` the 9 May copper spray.

Products upsert through ext_id `farmcore-demo-products`:<key> (stable keys, so re-seeding updates
them in place). Names and AMM numbers are real French products authorised on vigne, each verified
on its E-Phy page (`props.source`), so
`props.placeholder` is false. Each product's vine label limits (`DemoProduct.label`, the E-Phy
"Vigne, Trt Part.Aer." usage) go into its props for the register checks (`checks.Label`);
the upsert replaces props, so a database seeded earlier gains them on the next seed.
`active_substance` is lower-case: the chat regex extractor (06) maps spoken words through it.

Scene B: one copper spray per parcel of the default farm at 2024-05-09 08:00 local, 2 kg/ha,
through `logs.record_operation` with a `seed` source (ext_id per spray, so re-seeding is a no-op).
No 10 Apr spray: the live voice log covers 10 Apr, and a seed spray there would merge with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.core import ext_ids, logs
from farmcore.core.access import DEMO_SOURCE
from farmcore.core.models.canonical import ExtId, Log, Parcel, Product
from farmcore.core.seed import ensure_default_farm
from farmcore.settings import get_settings

PRODUCT_SOURCE = "farmcore-demo-products"
SEED_ACTOR = "seed"
SOURCE_APP = "operations"


@dataclass(frozen=True)
class DemoProduct:
    key: str
    name: str
    authorisation_no: str
    active_substance: str
    product_type: str
    targets: tuple[str, ...]
    source: str  # the product's E-Phy page; "" while the number is a placeholder
    # Vine label limits from the same E-Phy page (keys read by `checks.Label.from_props`); a limit
    # the label does not set is left out, never guessed.
    label: dict[str, Any] = field(default_factory=dict)
    # Organic status of the active substance, only with its source (N8, `production.py`); None:
    # no source, so organic mode does not offer the product.
    organic: dict[str, str] | None = None

    @property
    def placeholder(self) -> bool:
        return not self.source


EPHY = "https://ephy.anses.fr/ppp/"

# E-Phy shortlist (each entry re-checked on its E-Phy page 16 Oct 2026); names
# exactly as on E-Phy. Vine labels: copper downy 3.75 kg/ha max 5 applications; sulphur powdery
# 12.5 kg/ha max 8; PAJO downy 0.45 kg/ha max 2, 28-day PHI. PAJO also carries zoxamide.
# The label limits below are the "Vigne, Trt Part.Aer." usages on each page (ephy-products.md):
# - copper: no DAR on the label ("-"), no minimum interval; "copper compounds, 20 % copper" in the
#   composition, so 1 kg of product carries 0.2 kg copper metal (`cu_fraction`);
# - sulphur: DAR 3 days, no minimum interval;
# - PAJO: DAR 28 days, 7 days minimum between applications.
LABEL_USAGE = "Vigne, Trt Part.Aer."
# Reg. (EU) 2021/1165, Article 1: only the active substances in its Annex I may be contained in
# plant protection products used in organic production. Annex I lists "Bordeaux mixture" among
# the copper compounds (28 kg Cu/ha over 7 years) and "Sulphur". OJ L 253, 16.7.2021, p. 13,
# read 17 Oct 2026 (Publications Office copy of the OJ text). Cymoxanil and zoxamide (PAJO) are
# not in Annex I: no organic source, so PAJO carries none.
REG_2021_1165 = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32021R1165"
ORGANIC_COPPER = {
    "source": "Reg. (EU) 2021/1165 Annex I: Bordeaux mixture (copper compounds), max 28 kg "
    "Cu/ha over 7 years; Article 1",
    "url": REG_2021_1165,
}
ORGANIC_SULPHUR = {
    "source": "Reg. (EU) 2021/1165 Annex I: Sulphur; Article 1",
    "url": REG_2021_1165,
}
SHORTLIST: tuple[DemoProduct, ...] = (
    DemoProduct(
        "copper-contact",
        "BOUILLIE BORDELAISE RSR DISPERSS NC",
        "9800474",
        "copper",
        "contact",
        ("downy",),
        EPHY + "bouillie-bordelaise-rsr-disperss-nc",
        {"label_max_dose_kg_ha": 3.75, "label_max_applications": 5, "cu_fraction": 0.2},
        ORGANIC_COPPER,
    ),
    DemoProduct(
        "sulphur-wettable",
        "MICROTHIOL SPECIAL DISPERSS",
        "9800245",
        "sulphur",
        "contact",
        ("powdery",),
        EPHY + "microthiol-special-disperss",
        {"label_max_dose_kg_ha": 12.5, "label_max_applications": 8, "phi_days": 3},
        ORGANIC_SULPHUR,
    ),
    DemoProduct(
        "cymoxanil-penetrant",
        "PAJO",
        "2140127",
        "cymoxanil",
        "penetrant",
        ("downy",),
        EPHY + "pajo",
        {
            "label_max_dose_kg_ha": 0.45,
            "label_max_applications": 2,
            "phi_days": 28,
            "min_interval_days": 7,
        },
    ),
)

# Scene B: 9 May 2024 08:00 local copper spray, 2 kg/ha.
SCENE_B = "b"
SCENE_B_LOCAL = datetime(2024, 5, 9, 8, 0)
SCENE_B_PRODUCT = "copper-contact"
SCENE_B_DOSE = Decimal("2")
SCENE_B_UNIT = "kg/ha"


def ensure_products(session: Session) -> dict[str, Product]:
    """Upsert the shortlist; returns key -> product."""
    ext_ids.ensure_source(
        session, PRODUCT_SOURCE, "farmcore demo product shortlist", "seed", "Apache-2.0"
    )
    out = {}
    for p in SHORTLIST:
        out[p.key], _ = ext_ids.upsert(
            session,
            source=PRODUCT_SOURCE,
            key=p.key,
            model=Product,
            values={
                "name": p.name,
                "authorisation_no": p.authorisation_no,
                "active_substance": p.active_substance,
                "product_type": p.product_type,
                "targets": list(p.targets),
                "props": {
                    "placeholder": p.placeholder,
                    "source": p.source or None,
                    **({"label_usage": LABEL_USAGE, **p.label} if p.label else {}),
                    **({"organic": dict(p.organic)} if p.organic else {}),
                },
            },
        )
    return out


def _map_log(session: Session, key: str, log_id: int) -> None:
    """Point ext_id `farmcore-demo`:`key` at the log (re-pointed if its old log was deleted)."""
    ext = session.scalar(select(ExtId).where(ExtId.source == DEMO_SOURCE, ExtId.key == key))
    if ext is None:
        session.add(ExtId(source=DEMO_SOURCE, key=key, model="log", res_id=log_id))
    else:
        ext.model, ext.res_id = "log", log_id
    session.flush()


def _scene_b(session: Session, farm_id: int, copper: Product, tz: str) -> int:
    ts = SCENE_B_LOCAL.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))
    # Materialised: record_operation flushes and queries on this session inside the loop.
    parcels = list(
        session.scalars(select(Parcel).where(Parcel.farm_id == farm_id).order_by(Parcel.id))
    )
    count = 0
    for parcel in parcels:
        key = f"scene-b:{SCENE_B_LOCAL:%Y-%m-%d}:parcel:{parcel.id}"
        count += 1
        if isinstance(ext_ids.lookup(session, DEMO_SOURCE, key), Log):
            continue
        log, _ = logs.record_operation(
            session,
            farm_id=farm_id,
            type="spray",
            parcel_ids=[parcel.id],
            ts_start=ts,
            ts_end=None,
            quantities=[
                logs.QuantityInput(
                    measure="rate",
                    value=SCENE_B_DOSE,
                    unit=SCENE_B_UNIT,
                    product_id=copper.id,
                    label=copper.name,
                )
            ],
            source=logs.SourceInput("seed", key, {"scene": "B"}, None, logs.RANK_SEED),
            actor=SEED_ACTOR,
            occurred_at=ts,
            name=f"Copper spray {SCENE_B_LOCAL:%d %b %Y} (scene B)",
            source_app=SOURCE_APP,
        )
        _map_log(session, key, log.id)
    return count


def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    settings = get_settings()
    farm = ensure_default_farm(session, settings)
    if "production" not in (farm.props or {}):  # the demo farm is conventional (N8)
        farm.props = {**(farm.props or {}), "production": "conventional"}
    products = ensure_products(session)
    counts = {"products": len(products)}
    if scene is not None and scene.strip().lower() == SCENE_B:
        counts["scene_b_sprays"] = _scene_b(
            session, farm.id, products[SCENE_B_PRODUCT], settings.display_tz
        )
    return counts
