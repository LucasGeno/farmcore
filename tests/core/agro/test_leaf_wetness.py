"""Leaf wetness per hour: a sensor's minutes when there is one, otherwise estimated from relative
humidity against a labelled threshold. An estimate is never reported as measured."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from farmcore.core.agro.leaf_wetness import RH_WET_THRESHOLD, leaf_wetness, wet_duration
from farmcore.core.model_spec import Parameter

T0 = datetime(2025, 5, 1, tzinfo=UTC)


def _hours(*rh):
    return [(T0 + timedelta(hours=n), v) for n, v in enumerate(rh)]


def test_measured_minutes_win_and_estimates_carry_their_threshold() -> None:
    hours = leaf_wetness(_hours(95, 95, 50, None), measured={T0 + timedelta(hours=1): 20})

    assert [(h.wet_minutes, h.origin) for h in hours] == [
        (60, "estimated"),
        (20, "measured"),
        (0, "estimated"),
        (None, "missing"),
    ]
    assert hours[0].basis is RH_WET_THRESHOLD and hours[1].basis is None
    assert RH_WET_THRESHOLD.status == "assumed" and RH_WET_THRESHOLD.unit == "%"


def test_a_series_with_any_estimated_hour_is_labelled_estimated() -> None:
    all_measured = leaf_wetness(_hours(None, None), measured={T0: 60, T0 + timedelta(hours=1): 30})
    mixed = leaf_wetness(_hours(None, 92), measured={T0: 60})
    gap = leaf_wetness(_hours(None, 92), measured={})

    assert wet_duration(all_measured) == (1.5, "measured", (), 0)
    assert wet_duration(mixed) == (2.0, "estimated", (RH_WET_THRESHOLD,), 0)
    assert wet_duration(gap) == (1.0, "estimated", (RH_WET_THRESHOLD,), 1)  # missing, not dry


def test_threshold_is_a_labelled_parameter_and_bad_inputs_are_refused() -> None:
    strict = Parameter("rh_wet_threshold", 97.0, "%", source="assumption: test", status="assumed")
    assert [h.wet_minutes for h in leaf_wetness(_hours(95, 97), threshold=strict)] == [0, 60]
    assert leaf_wetness(_hours(97), threshold=strict)[0].basis is strict

    with pytest.raises(ValueError, match="threshold"):
        leaf_wetness(_hours(95), threshold=Parameter("x", 90.0, "%", status="unsourced"))
    with pytest.raises(ValueError, match="minutes"):
        leaf_wetness(_hours(None), measured={T0: 61})
    with pytest.raises(ValueError, match="not in the series"):
        leaf_wetness(_hours(95), measured={T0 + timedelta(hours=5): 10})
    with pytest.raises(ValueError, match="relative humidity"):
        leaf_wetness(_hours(101))
    with pytest.raises(ValueError, match="timezone"):
        leaf_wetness([(datetime(2025, 5, 1), 95)])


@given(st.lists(st.floats(0, 100), min_size=1, max_size=48), st.floats(0, 5))
def test_estimated_wetness_never_shrinks_when_air_gets_more_humid(rh, bump) -> None:
    before = wet_duration(leaf_wetness(_hours(*rh)))
    after = wet_duration(leaf_wetness(_hours(*(min(100.0, v + bump) for v in rh))))
    assert after.hours >= before.hours
