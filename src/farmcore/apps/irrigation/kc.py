"""Crop coefficients (Kc) by FarmCore crop key and growth stage, each with its source.

Sources
- FAO-56: Allen, Pereira, Raes, Smith (1998), FAO Irrigation and Drainage Paper 56, Table 12
  (single Kc ini / mid / end). Table 12 values are for a sub-humid climate (RHmin about 45 %,
  u2 2 m/s). The Eq. 62 adjustment of Kc mid / end for other climates (Mediterranean summers,
  RHmin < 45 %) is NOT applied here.
- Ground cover: the Allen & Pereira (2009) Kd approach (Irrig. Sci. 28:17-34), not FAO-56
  eq. 98: Kc = Kc_min + Kd (Kc_full - Kc_min), Kd = min(1, M_L fc_eff, fc_eff^(1/(1+h))). It
  takes a full-cover Kc only: Table 12 woody rows already include their own cover.
- Rallo et al. (2021), Agric. Water Manag. 250:106645, updates woody-crop values. It is NOT
  applied: every woody row here (vine, olive, fruit trees, citrus, walnut) is FAO-56 Table 12.

Every value carries a `source`: a citation, `assumed`, or `farmer` for an override. A crop
FarmCore has no value for is `UNKNOWN`; nothing is guessed from a neighbouring crop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

UNKNOWN: Literal["unknown"] = "unknown"
ASSUMED = "assumed"
FARMER = "farmer"
FAO56 = "FAO-56 Table 12"
AP2009 = "Allen & Pereira 2009"

STAGES = ("ini", "dev", "mid", "late")

# Kc_min of bare soil between rows (Allen & Pereira 2009 use 0.15) and M_L, the canopy-density
# multiplier (their default 1.5; range 1.5-2.0).
KC_MIN = 0.15
M_L = 1.5

# Share of a season spent in ini / dev / mid / late. FAO-56 Table 11 lengths are per crop and
# climate and are not tabulated here, so a date splits the season by these `assumed` shares.
DATE_SHARES = (0.15, 0.25, 0.35, 0.25)


@dataclass(frozen=True)
class KcValue:
    kc: float
    source: str
    full_cover: bool = False  # a full-canopy Kc, the only input of `ground_cover_kc`


@dataclass(frozen=True)
class CropKc:
    ini: float
    mid: float
    end: float
    source: str
    full_cover: bool = False  # mid is a full-cover value (annual crops)


# FarmCore crop key -> FAO-56 Table 12 row. Crops absent here are unknown, including the group
# keys (fruit-trees, nuts, vegetables) that name no single Table 12 row. Deciduous orchards
# use the "no ground cover, killing frost" variant.
TABLE: dict[str, CropKc] = {
    # Cereals group Kc ini 0.3 (row blank); end 0.25-0.4, low end = machine harvest (fn 10)
    "barley": CropKc(
        0.30, 1.15, 0.25, f"{FAO56}, Barley; Kc ini from Cereals group", full_cover=True
    ),
    "soft-wheat": CropKc(
        0.70,
        1.15,
        0.25,
        f"{FAO56}, Winter Wheat, non-frozen soils; end low end (fn 10)",
        full_cover=True,
    ),
    "durum-wheat": CropKc(
        0.70,
        1.15,
        0.25,
        f"{FAO56}, Winter Wheat, non-frozen soils; end low end (fn 10)",
        full_cover=True,
    ),
    "maize": CropKc(
        0.30,
        1.20,
        0.60,
        f"{FAO56}, Maize, Field (grain); Kc ini from Cereals group; "
        "end at high grain moisture (fn 11)",
        full_cover=True,
    ),
    "sunflower": CropKc(
        0.35,
        1.15,
        0.35,
        f"{FAO56}, Sunflower; Kc ini from Oil Crops group; mid high end, irrigated (fn 9)",
        full_cover=True,
    ),
    "rapeseed": CropKc(
        0.35,
        1.15,
        0.35,
        f"{FAO56}, Rapeseed, Canola; Kc ini from Oil Crops group; mid high end, irrigated (fn 9)",
        full_cover=True,
    ),
    "rice": CropKc(1.05, 1.20, 0.90, f"{FAO56}, Rice; end 0.90-0.60, high end", full_cover=True),
    "potato": CropKc(
        0.50, 1.15, 0.75, f"{FAO56}, Potato; Kc ini from Roots and Tubers group", full_cover=True
    ),
    "alfalfa": CropKc(
        0.40, 0.95, 0.90, f"{FAO56}, Alfalfa Hay, averaged cutting effects (fn 13)", full_cover=True
    ),
    "soybean": CropKc(
        0.40, 1.15, 0.50, f"{FAO56}, Soybeans; Kc ini from Legumes group", full_cover=True
    ),
    "grassland": CropKc(
        0.40,
        0.85,
        0.85,
        f"{ASSUMED}: {FAO56}, Grazing Pasture, Rotated Grazing; mid 0.85-1.05, low end",
    ),
    "vine": CropKc(0.30, 0.70, 0.45, f"{FAO56}, Grapes, Wine"),
    "olive": CropKc(0.65, 0.70, 0.70, f"{FAO56}, Olives, 40-60% ground cover (fn 24)"),
    "citrus": CropKc(
        0.70,
        0.65,
        0.70,
        f"{ASSUMED}: {FAO56}, Citrus, no ground cover, 70% canopy (fn 21)",
    ),
    "walnut": CropKc(0.50, 1.10, 0.65, f"{FAO56}, Walnut Orchard (fn 18, 19)"),
    "peach": CropKc(
        0.45,
        0.90,
        0.65,
        f"{FAO56}, Apricots, Peaches, Stone Fruit, no ground cover, killing frost",
    ),
    "plum": CropKc(
        0.45,
        0.90,
        0.65,
        f"{FAO56}, Apricots, Peaches, Stone Fruit, no ground cover, killing frost (fn 20)",
    ),
    "cherry": CropKc(
        0.45,
        0.95,
        0.70,
        f"{FAO56}, Apples, Cherries, Pears, no ground cover, killing frost",
    ),
    "pear": CropKc(
        0.45,
        0.95,
        0.70,
        f"{FAO56}, Apples, Cherries, Pears, no ground cover, killing frost",
    ),
}


def stage_from_bbch(bbch: int) -> str:
    """The stage of a BBCH code (0-99). The split is `assumed`: BBCH is not tied to Kc stages."""
    if not 0 <= bbch <= 99:
        raise ValueError("BBCH is 0-99")
    if bbch < 20:
        return "ini"
    if bbch < 50:
        return "dev"
    if bbch < 80:
        return "mid"
    return "late"


def stage_on(day: date, start: date, end: date) -> tuple[str, float]:
    """The stage of `day` in the season start..end (inclusive) and how far through that stage it
    is (0-1), by the `assumed` DATE_SHARES."""
    if not start <= day <= end:
        raise ValueError("the day is outside the season")
    fraction = (day - start).days / max((end - start).days + 1, 1)
    edge = 0.0
    for stage, share in zip(STAGES, DATE_SHARES, strict=True):
        if fraction < edge + share:
            return stage, (fraction - edge) / share
        edge += share
    return "late", 1.0


def kc_for(
    crop: str, stage: str, *, progress: float = 0.5, override: float | None = None
) -> KcValue | Literal["unknown"]:
    """Kc of a crop in a stage. `ini` and `mid` are the Table 12 values. `dev` and `late` are
    not tabulated: they are linear within the stage, ini to mid and mid to end (the shape of
    FAO-56 Fig. 25 / Eq. 66), at `progress` (0-1, from `stage_on`), and labelled `assumed`.
    A farmer's `override` wins and is labelled `farmer`."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if not 0 <= progress <= 1:
        raise ValueError("progress is 0-1")
    if override is not None:
        if override <= 0:
            raise ValueError("a Kc override must be positive")
        return KcValue(override, FARMER)
    row = TABLE.get(crop)
    if row is None:
        return UNKNOWN
    if stage == "ini":
        return KcValue(row.ini, row.source)
    if stage == "mid":
        return KcValue(row.mid, row.source, full_cover=row.full_cover)
    start, stop = (row.ini, row.mid) if stage == "dev" else (row.mid, row.end)
    kc = round(start + (stop - start) * progress, 3)
    return KcValue(kc, f"{ASSUMED}: linear within {stage}; {row.source}")


def ground_cover_kc(
    kc_full: KcValue | Literal["unknown"], fc: float, height: float
) -> KcValue | Literal["unknown"]:
    """Adjust a full-cover Kc to a sparse canopy: Allen & Pereira (2009) Kd approach.

    `fc` is the fraction of ground covered (0-1), `height` the canopy height in metres. Only a
    full-cover Kc is accepted; Table 12 woody rows already include their cover and are refused,
    since adjusting them would count the sparseness twice. fc_eff = fc is `assumed` (A&P divide
    by the sine of the solar elevation, which understates Kd slightly)."""
    if kc_full == UNKNOWN:
        return UNKNOWN
    if not kc_full.full_cover:
        raise ValueError("ground-cover adjustment needs a full-cover Kc")
    if not 0 < fc <= 1:
        raise ValueError("ground cover is a fraction above 0 up to 1")
    if height <= 0:
        raise ValueError("canopy height must be positive")
    kd = min(1.0, M_L * fc, fc ** (1 / (1 + height)))
    kc = KC_MIN + kd * (max(kc_full.kc, KC_MIN) - KC_MIN)
    return KcValue(round(kc, 3), f"{AP2009} Kd, {ASSUMED} fc_eff = fc; on {kc_full.source}")
