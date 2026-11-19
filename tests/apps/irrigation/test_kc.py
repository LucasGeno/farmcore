from datetime import date

import pytest

from farmcore.apps.irrigation import kc
from farmcore.apps.irrigation.kc import KcValue

# Quoted from FAO-56 Table 12 (Kc ini, mid, end), not derived from the module. Group Kc ini for
# cereals, oil crops, roots and legumes; deciduous orchards are the killing-frost variant.
PUBLISHED = {
    "barley": (0.30, 1.15, 0.25),
    "soft-wheat": (0.70, 1.15, 0.25),
    "durum-wheat": (0.70, 1.15, 0.25),
    "maize": (0.30, 1.20, 0.60),
    "sunflower": (0.35, 1.15, 0.35),
    "rapeseed": (0.35, 1.15, 0.35),
    "rice": (1.05, 1.20, 0.90),
    "potato": (0.50, 1.15, 0.75),
    "alfalfa": (0.40, 0.95, 0.90),
    "soybean": (0.40, 1.15, 0.50),
    "grassland": (0.40, 0.85, 0.85),
    "vine": (0.30, 0.70, 0.45),
    "olive": (0.65, 0.70, 0.70),
    "citrus": (0.70, 0.65, 0.70),
    "walnut": (0.50, 1.10, 0.65),
    "peach": (0.45, 0.90, 0.65),
    "plum": (0.45, 0.90, 0.65),
    "cherry": (0.45, 0.95, 0.70),
    "pear": (0.45, 0.95, 0.70),
}


def _cited(source):
    return source.startswith(("FAO-56 Table 12", "assumed: FAO-56 Table 12"))


@pytest.mark.parametrize("crop", PUBLISHED)
def test_table_matches_fao56_table_12_and_cites_it(crop):
    ini, mid, end = PUBLISHED[crop]
    assert set(kc.TABLE) == set(PUBLISHED)
    for stage, expected in (("ini", ini), ("mid", mid)):
        value = kc.kc_for(crop, stage)
        assert value.kc == expected
        assert _cited(value.source)
    assert kc.kc_for(crop, "late", progress=1.0).kc == end


def test_stage_is_found_by_bbch_or_date_and_dev_late_are_assumed_and_interpolated():
    assert [kc.stage_from_bbch(b) for b in (10, 30, 65, 89)] == ["ini", "dev", "mid", "late"]
    start, end = date(2026, 3, 1), date(2026, 8, 28)  # 181 days
    assert kc.stage_on(start, start, end) == ("ini", 0.0)
    assert kc.stage_on(date(2026, 6, 1), start, end)[0] == "mid"
    assert kc.stage_on(end, start, end)[0] == "late"
    # Maize ini 0.30 -> mid 1.20 halfway is 0.75; mid 1.20 -> end 0.60 halfway is 0.90.
    dev, late = kc.kc_for("maize", "dev"), kc.kc_for("maize", "late")
    assert (dev.kc, late.kc) == (0.75, 0.90)
    assert dev.source.startswith("assumed") and late.source.startswith("assumed")


def test_unknown_crop_is_unknown_everywhere():
    assert kc.kc_for("hazelnut", "mid") == "unknown"
    assert kc.kc_for("vegetables", "mid") == "unknown"
    assert kc.ground_cover_kc(kc.kc_for("hazelnut", "mid"), fc=0.5, height=2.0) == "unknown"


def test_ground_cover_adjusts_only_a_full_cover_kc():
    # Hand-worked from Allen & Pereira 2009 on maize mid 1.20 (full cover), fc 0.5, h 2 m:
    # Kd = min(1, 1.5*0.5, 0.5^(1/3)) = 0.75; Kc = 0.15 + 0.75 * (1.20 - 0.15) = 0.9375.
    adjusted = kc.ground_cover_kc(kc.kc_for("maize", "mid"), fc=0.5, height=2.0)
    assert adjusted.kc == pytest.approx(0.9375, abs=0.001)
    assert "Allen & Pereira 2009" in adjusted.source and "assumed" in adjusted.source
    # Olive's 0.70 already includes its 40-60 % cover: adjusting it would count that twice.
    with pytest.raises(ValueError):
        kc.ground_cover_kc(kc.kc_for("olive", "mid"), fc=0.3, height=2.0)


def test_farmer_override_is_labelled_farmer():
    assert kc.kc_for("olive", "mid", override=0.55) == KcValue(0.55, "farmer")
    assert kc.kc_for("hazelnut", "mid", override=0.8) == KcValue(0.8, "farmer")
    with pytest.raises(ValueError):
        kc.kc_for("olive", "mid", override=0)
