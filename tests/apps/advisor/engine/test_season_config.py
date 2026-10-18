"""Season config: sulphur wash-off, powdery period end and the calendar span,
through `AdvisorConfig.for_region_year`."""

import re
from datetime import date, timedelta

from farmcore.apps.advisor.engine import AdvisorConfig
from farmcore.apps.advisor.engine.config import Threshold

PENDING = "assumed: awaiting agronomist"


def test_sulphur_washoff_is_30_mm_assumed_with_its_sources():
    washoff = AdvisorConfig.for_region_year("FRI12", 2024).washoff_sulphur_mm

    assert washoff.value == 30.0
    assert washoff.assumed
    assert "Ticino" in washoff.source and "30–60" in washoff.source
    assert "30–40" in washoff.source
    assert PENDING in washoff.source
    # The French sources (lower) are cited next to the kept value pending agronomist review.
    assert "BIVB Mémo BIO 2025" in washoff.source and "lessivage dès 20-25 mm" in washoff.source
    assert "Chambre d'agriculture AURA" in washoff.source and "Oct 2024" in washoff.source


def test_powdery_period_ends_four_weeks_after_bloom_bunch_closure_kept_for_the_calendar():
    # FRI12 bloom 28 May 2024 / 26 May 2025; + 28 d → 25 Jun / 23 Jun (Gadoury 2003).
    # Bunch closure (BBCH 77) stays 8 Jul / 5 Jul, now the calendar programme's end only.
    y24 = AdvisorConfig.for_region_year("FRI12", 2024)
    y25 = AdvisorConfig.for_region_year("FRI12", 2025)

    assert y24.powdery_end.value == date(2024, 6, 25)
    assert y25.powdery_end.value == date(2025, 6, 23)
    assert "Gadoury 2003" in y24.powdery_end.source
    assert y24.powdery_end.assumed  # bloom itself is an assumed date
    assert y24.calendar.bunch_closure.value == date(2024, 7, 8)
    assert y25.calendar.bunch_closure.value == date(2025, 7, 5)
    assert y24.calendar.bunch_closure.assumed


def test_powdery_end_follows_an_overridden_bloom_unless_set_itself():
    bloom = Threshold(value=date(2024, 6, 1), source="test bloom", assumed=True)
    moved = AdvisorConfig.for_region_year("FRI12", 2024, powdery_start=bloom)
    end = Threshold(value=date(2024, 7, 1), source="test end", assumed=True)
    pinned = AdvisorConfig.for_region_year("FRI12", 2024, powdery_start=bloom, powdery_end=end)

    assert moved.powdery_end.value == date(2024, 6, 1) + timedelta(days=28)
    assert pinned.powdery_end.value == date(2024, 7, 1)


def test_unknown_year_default_season_has_both_ends():
    cfg = AdvisorConfig.for_region_year("FRI12", 2019)

    assert cfg.powdery_end.value == cfg.powdery_start.value + timedelta(days=28)
    assert cfg.calendar.bunch_closure.value == date(2019, 7, 10)


def test_rapid_growth_dates_driving_the_calendar_interval_say_awaiting_agronomist():
    cfg = AdvisorConfig.for_region_year("FRI12", 2024)

    assert PENDING in cfg.rapid_growth_start.source
    assert PENDING in cfg.rapid_growth_end.source


def test_no_threshold_source_cites_an_internal_ticket():
    cfg = AdvisorConfig.for_region_year("FRI12", 2024)
    names = AdvisorConfig.threshold_names() + AdvisorConfig.nested_threshold_names()

    bare = [n for n in names if re.search(r"\bP\d\b|ticket", cfg.threshold(n).source)]

    assert bare == []
