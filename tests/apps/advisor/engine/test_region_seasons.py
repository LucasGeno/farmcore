"""Per-region season defaults: Bologna ITH55 and Valladolid ES418 get their own dated
season for every baseline year; FRI12 and the generic default are unchanged.

Region dates: ITH55 gate 15 Apr, rapid growth 20 Apr – 30 Jun,
bloom 25 May, bunch closure 5 Jul; ES418 gate 2 May, rapid growth 5 May – 10 Jul, bloom 10 Jun,
bunch closure 20 Jul.
"""

import hashlib
from datetime import date

import pytest

from farmcore.apps.advisor.engine import AdvisorConfig

YEARS = range(2015, 2025)
PENDING = "awaiting agronomist"
SEASON_FIELDS = ("season_gate", "rapid_growth_start", "rapid_growth_end", "powdery_start")

# (gate, rapid growth start, rapid growth end, bloom, bunch closure) as (month, day).
REGION_DATES = {
    "ITH55": ((4, 15), (4, 20), (6, 30), (5, 25), (7, 5)),
    "ES418": ((5, 2), (5, 5), (7, 10), (6, 10), (7, 20)),
}
REGION_NAMES = {"ITH55": "Bologna", "ES418": "Valladolid"}

# sha256 of `for_region_year(...).model_dump_json()`, sources included. A deliberate FRI12 /
# default change updates these with its own review. Last: citation text only (model contract
# review), every value unchanged.
BEFORE = {
    ("FRI12", 2024): "9d4ea73ca4f61e6136572bf25864d3d61182904a0521fee1e09e2c8a23dd2e72",
    ("FRI12", 2025): "0fcbb2c52d7f2819db1ed681479395a0f6205d97211bbc238adf983a5a0f3a62",
    ("FRI12", 2019): "38e294b71f82de0868615e473ee65b70ce5f2164d2b70be717d56fb0d87ada2a",
    ("ZZ999", 2024): "f29a86b52d4b042cc99379dc4913833600d031f35828bacb762bf0a9fec41d75",
}


def test_bologna_2024_gate_is_15_april_and_assumed():
    gate = AdvisorConfig.for_region_year("ITH55", 2024).season_gate

    assert gate.value == date(2024, 4, 15)
    assert gate.assumed
    assert "Bologna" in gate.source and "ARPAE" in gate.source


@pytest.mark.parametrize("region", sorted(REGION_DATES))
@pytest.mark.parametrize("year", YEARS)
def test_region_dates_for_every_baseline_year_are_assumed_and_name_the_region(region, year):
    cfg = AdvisorConfig.for_region_year(region, year)
    gate, rg_start, rg_end, bloom, closure = (date(year, m, d) for m, d in REGION_DATES[region])

    assert cfg.region == region
    assert cfg.season_gate.value == gate
    assert cfg.rapid_growth_start.value == rg_start
    assert cfg.rapid_growth_end.value == rg_end
    assert cfg.powdery_start.value == bloom
    assert cfg.calendar.bunch_closure.value == closure
    for t in [cfg.threshold(f) for f in SEASON_FIELDS] + [cfg.calendar.bunch_closure]:
        assert t.assumed
        assert region in t.source and REGION_NAMES[region] in t.source
        assert PENDING in t.source


def test_valladolid_gate_cites_the_ribera_budbreak():
    gate = AdvisorConfig.for_region_year("ES418", 2024).season_gate

    assert gate.value == date(2024, 5, 2)
    assert "Ribera" in gate.source and "19 Apr" in gate.source


def test_region_is_matched_case_insensitively():
    assert AdvisorConfig.for_region_year("ith55", 2020).season_gate.value == date(2020, 4, 15)


def test_unknown_region_still_gets_the_generic_default():
    cfg = AdvisorConfig.for_region_year("ITF63", 2024)

    assert cfg.season_gate.value == date(2024, 4, 15)
    assert "baseline default 15 Apr" in cfg.season_gate.source


@pytest.mark.parametrize(("region", "year"), sorted(BEFORE))
def test_fri12_and_the_generic_default_are_unchanged(region, year):
    dump = AdvisorConfig.for_region_year(region, year).model_dump_json()

    assert hashlib.sha256(dump.encode()).hexdigest() == BEFORE[(region, year)]
