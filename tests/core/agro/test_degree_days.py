"""Degree-days (Zalom et al. 1983): single and double sine and triangle, horizontal and vertical
cutoffs. The golden numbers are degday 0.4.0's, run out of process (tools/oracles); degday has no
vertical cutoff, so those cases are derived from its horizontal outputs by removing the time the
day spends above the upper threshold, which is closed-form for a sine or a straight line."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from farmcore.core.agro.degree_days import daily_degree_days, degree_days

FIXTURES = Path(__file__).parents[2] / "fixtures" / "oracles" / "degday"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_daily_degree_days_match_the_degday_oracle(path: Path) -> None:
    fx = json.loads(path.read_text())
    p, i = fx["parameters"], fx["inputs"]
    assert p["cutoff"] == "horizontal" and p["no_neg"] and not p["cumulative"]

    got = daily_degree_days(
        list(zip(i["daily_min"], i["daily_max"], strict=True)),
        lower=p["thresh_low"],
        upper=p["thresh_up"],
        method=p["method"],
        cutoff="horizontal",
    )

    # degday's own last-day rule: the next-day minimum of the last row is its own minimum.
    assert i["nextday_min"] == i["daily_min"][1:] + i["daily_min"][-1:]
    assert got == pytest.approx(fx["outputs"]["daily_degree_days"], abs=5e-7)


def _fraction_above(tmin: float, tmax: float, upper: float, shape: str) -> float:
    """Share of a half-day (tmin to tmax) above `upper`: the line or sine crosses it once."""
    if tmax <= upper:
        return 0.0
    if tmin >= upper:
        return 1.0
    if shape == "tri":
        return (tmax - upper) / (tmax - tmin)
    mean, amp = (tmax + tmin) / 2, (tmax - tmin) / 2
    return math.acos((upper - mean) / amp) / math.pi


@pytest.mark.parametrize("method", ["sng_sine", "dbl_sine", "sng_tri", "dbl_tri"])
def test_vertical_cutoff_drops_the_time_above_the_upper_threshold(method: str) -> None:
    fx = _load(f"{method}-horizontal")
    i, lo, up = fx["inputs"], fx["parameters"]["thresh_low"], fx["parameters"]["thresh_up"]
    shape = method.split("_")[1]
    expected = []
    for dd, tmin, tmax, nxt in zip(
        fx["outputs"]["daily_degree_days"],
        i["daily_min"],
        i["daily_max"],
        i["nextday_min"],
        strict=True,
    ):
        if method.startswith("sng"):
            above = _fraction_above(tmin, tmax, up, shape)
        else:
            above = (
                _fraction_above(tmin, tmax, up, shape) + _fraction_above(nxt, tmax, up, shape)
            ) / 2
        expected.append(dd - (up - lo) * above)

    got = daily_degree_days(
        list(zip(i["daily_min"], i["daily_max"], strict=True)),
        lower=lo,
        upper=up,
        method=method,
        cutoff="vertical",
    )

    assert got == pytest.approx(expected, abs=5e-6)
    if method.startswith("sng"):  # a double day falls to the next minimum (10) afterwards
        assert got[5] == 0  # entirely above the upper threshold: no development at all
    assert got[0] == 0  # entirely below the lower threshold


temps = st.floats(min_value=-30, max_value=50, allow_nan=False)
methods = st.sampled_from(["sng_sine", "dbl_sine", "sng_tri", "dbl_tri"])


@example(0.0, 5e-324, 0.0, 0.0, "sng_sine", 1.0)  # a spread too small to divide by
@given(temps, temps, temps, st.floats(min_value=0, max_value=20), methods, st.floats(0.5, 25))
def test_degree_days_stay_inside_the_threshold_window_and_grow_with_warmth(
    a, b, nxt, lower, method, width
) -> None:
    tmin, tmax = min(a, b), max(a, b)
    upper = lower + width
    kw = {"lower": lower, "upper": upper, "method": method, "next_tmin": nxt}
    horizontal = degree_days(tmin, tmax, cutoff="horizontal", **kw)
    vertical = degree_days(tmin, tmax, cutoff="vertical", **kw)

    assert -1e-9 <= vertical <= horizontal + 1e-9
    assert horizontal <= width + 1e-9
    assert degree_days(tmin, tmax + 1, cutoff="horizontal", **kw) >= horizontal - 1e-9
    # No upper threshold is the same as one the day never reaches.
    assert degree_days(tmin, tmax, lower=lower, method=method, next_tmin=nxt) == pytest.approx(
        degree_days(tmin, tmax, lower=lower, upper=1e6, method=method, next_tmin=nxt), abs=1e-9
    )


def test_degree_days_refuse_inverted_or_unknown_inputs() -> None:
    with pytest.raises(ValueError, match="tmin"):
        degree_days(20, 10, lower=10)
    with pytest.raises(ValueError, match="upper"):
        degree_days(5, 20, lower=10, upper=10)
    with pytest.raises(ValueError, match="method"):
        degree_days(5, 20, lower=10, method="avg")
    with pytest.raises(ValueError, match="cutoff"):
        degree_days(5, 20, lower=10, upper=30, cutoff="intermediate")
