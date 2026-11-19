from datetime import date

import pytest

from farmcore.apps.irrigation import kc
from farmcore.apps.irrigation.kc import KcValue

# Quoted from FAO-56 Table 12 (Kc ini, mid, end), not derived from the module.
PUBLISHED = {
    "maize": (0.30, 1.20, 0.60),
    "potato": (0.50, 1.15, 0.75),
    "olive": (0.65, 0.70, 0.70),
    "vine": (0.30, 0.70, 0.45),
    "walnut": (0.50, 1.10, 0.65),
}


@pytest.mark.parametrize("crop", PUBLISHED)
def test_table_matches_fao56_table_12_and_cites_it(crop):
    ini, mid, end = PUBLISHED[crop]
    assert kc.kc_for(crop, "ini") == KcValue(ini, "FAO-56 Table 12")
    assert kc.kc_for(crop, "mid") == KcValue(mid, "FAO-56 Table 12")
    assert kc.kc_for(crop, "late").kc == end


def test_unknown_crop_is_unknown_and_stage_is_found_by_bbch_or_date():
    assert kc.kc_for("hazelnut", "mid") == "unknown"
    assert kc.kc_for("vegetables", "mid") == "unknown"
    assert [kc.stage_from_bbch(b) for b in (10, 30, 65, 89)] == ["ini", "dev", "mid", "late"]
    start, end = date(2026, 3, 1), date(2026, 8, 28)  # 181 days
    assert kc.stage_on(start, start, end) == "ini"
    assert kc.stage_on(date(2026, 6, 1), start, end) == "mid"
    assert kc.stage_on(end, start, end) == "late"
    assert kc.kc_for("maize", "dev").source.startswith("assumed")


def test_ground_cover_adjustment_scales_olive_mid_kc():
    # Hand-worked: fc 0.3, h 2 m: Kd = min(1, 1.5*0.3, 0.3^(1/3)) = 0.45;
    # Kc = 0.15 + 0.45 * (0.70 - 0.15) = 0.3975.
    full = kc.kc_for("olive", "mid")
    adjusted = kc.ground_cover_kc(full, fc=0.3, height=2.0)
    assert adjusted.kc == pytest.approx(0.3975, abs=0.001)
    assert "Allen & Pereira 2009" in adjusted.source
    assert kc.ground_cover_kc(full, fc=1.0, height=2.0).kc == 0.70


def test_farmer_override_is_labelled_farmer():
    assert kc.kc_for("olive", "mid", override=0.55) == KcValue(0.55, "farmer")
    assert kc.kc_for("hazelnut", "mid", override=0.8) == KcValue(0.8, "farmer")
    with pytest.raises(ValueError):
        kc.kc_for("olive", "mid", override=0)
