"""Crop coefficients (Kc) by FarmCore crop key and growth stage, each with its source.

Sources
- FAO-56: Allen, Pereira, Raes, Smith (1998), FAO Irrigation and Drainage Paper 56, Table 12
  (single Kc ini / mid / end, sub-humid climate, RHmin about 45 %, wind 2 m/s).
- Ground cover: the Allen & Pereira (2009) Kd approach (Irrig. Sci. 28:17-34), not FAO-56
  eq. 98: Kc = Kc_min + Kd (Kc_full - Kc_min), Kd = min(1, M_L fc, fc^(1/(1+h))).
- Rallo et al. (2021), Agric. Water Manag. 250:106645, updates woody-crop values. None of its
  values is tabulated here yet; woody rows keep their FAO-56 values until they are quoted.

Every value carries a `source`: a citation, `assumed`, or `farmer` for an override. A crop or
stage FarmCore has no value for is `UNKNOWN`; nothing is guessed from a neighbouring crop.
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


@dataclass(frozen=True)
class CropKc:
    ini: float
    mid: float
    end: float
    source: str = FAO56


# FarmCore crop key -> FAO-56 Table 12 row. Crops absent here are unknown, including the group
# keys (fruit-trees, nuts, vegetables) that name no single Table 12 row.
TABLE: dict[str, CropKc] = {
    "barley": CropKc(0.30, 1.15, 0.25),
    "soft-wheat": CropKc(0.40, 1.15, 0.25),  # winter wheat, non-frozen soils
    "durum-wheat": CropKc(0.40, 1.15, 0.25),  # winter wheat row, durum not listed apart
    "maize": CropKc(0.30, 1.20, 0.60),  # field maize, harvested at high grain moisture
    "sunflower": CropKc(0.35, 1.00, 0.35),  # mid is 1.00-1.15; the low end
    "rapeseed": CropKc(0.35, 1.00, 0.35),  # rape / canola; mid is 1.00-1.15, the low end
    "rice": CropKc(1.05, 1.20, 0.90),  # end is 0.90-0.60; the high end
    "potato": CropKc(0.50, 1.15, 0.75),
    "alfalfa": CropKc(0.40, 0.95, 0.90),  # hay, averaged over cutting cycles
    "soybean": CropKc(0.40, 1.15, 0.50),
    "grassland": CropKc(0.40, 0.85, 0.85),  # grazing pasture, rotated; mid is 0.85-1.05
    "vine": CropKc(0.30, 0.70, 0.45),  # wine grapes
    "olive": CropKc(0.65, 0.70, 0.70),
    "citrus": CropKc(0.70, 0.65, 0.70),  # 70 % canopy, no weeds
    "walnut": CropKc(0.50, 1.10, 0.65),
    "peach": CropKc(0.55, 0.90, 0.65),  # stone fruit, no ground cover
    "plum": CropKc(0.55, 0.90, 0.65),  # stone fruit, no ground cover
    "cherry": CropKc(0.45, 0.95, 0.70),  # apples, cherries, pears, no ground cover
    "pear": CropKc(0.45, 0.95, 0.70),
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


def stage_on(day: date, start: date, end: date) -> str:
    """The stage of `day` in the season start..end (inclusive), by the `assumed` DATE_SHARES."""
    if not start <= day <= end:
        raise ValueError("the day is outside the season")
    fraction = (day - start).days / max((end - start).days + 1, 1)
    edge = 0.0
    for stage, share in zip(STAGES, DATE_SHARES, strict=True):
        edge += share
        if fraction < edge:
            return stage
    return "late"


def kc_for(crop: str, stage: str, *, override: float | None = None) -> KcValue | Literal["unknown"]:
    """Kc of a crop in a stage. `dev` and `late` are not tabulated: they sit at the ini or mid
    value, the conservative step of FAO-56's own piecewise curve, and are labelled `assumed`.
    A farmer's `override` wins and is labelled `farmer`."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
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
        return KcValue(row.mid, row.source)
    if stage == "dev":
        return KcValue(row.ini, f"{ASSUMED}: ini value, {row.source}")
    return KcValue(row.end, f"{row.source} (end)")


def ground_cover_kc(kc_full: KcValue, fc: float, height: float) -> KcValue:
    """Adjust a full-cover Kc to a sparse canopy: Allen & Pereira (2009) Kd approach.

    `fc` is the fraction of ground covered (0-1), `height` the canopy height in metres."""
    if not 0 < fc <= 1:
        raise ValueError("ground cover is a fraction above 0 up to 1")
    if height <= 0:
        raise ValueError("canopy height must be positive")
    kd = min(1.0, M_L * fc, fc ** (1 / (1 + height)))
    kc = KC_MIN + kd * (max(kc_full.kc, KC_MIN) - KC_MIN)
    return KcValue(round(kc, 3), f"{AP2009} Kd on {kc_full.source}")
