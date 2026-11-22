"""Daily degree-days by the methods in Zalom et al. (1983).

Zalom, F.G., Goodell, P.B., Wilson, L.T., Barnett, W.W. & Bentley, W.J. (1983). Degree-days: the
calculation and use of heat units in pest management. University of California DANR Leaflet
21373. Implemented from the leaflet's description, not from any implementation's code.

A day's temperature runs from its minimum to its maximum and back. The single methods mirror the
first half (min to max) for the whole day; the double methods average that half with the second
(max to the next day's minimum). Each half follows a sine (`*_sine`) or a straight line
(`*_tri`). The degree-days of a half are the mean, over the half, of the temperature above the
lower threshold, limited at the upper threshold:

- `horizontal` cutoff: time above the upper threshold counts as `upper - lower`;
- `vertical` cutoff: time above the upper threshold counts as 0 (no development).

The thresholds are the caller's (from its model's `ModelSpec`); this module holds none. With no
upper threshold the cutoff does not apply. Golden tests: `tests/core/agro/test_degree_days.py`
against degday 0.4.0 (GPL), run out of process by the oracle harness.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

METHODS = ("sng_sine", "dbl_sine", "sng_tri", "dbl_tri")
CUTOFFS = ("horizontal", "vertical")


def degree_days(
    tmin: float,
    tmax: float,
    *,
    lower: float,
    upper: float | None = None,
    method: str = "sng_sine",
    cutoff: str = "horizontal",
    next_tmin: float | None = None,
) -> float:
    """Degree-days (degC day) of one day with minimum `tmin` and maximum `tmax` (degC).

    `next_tmin` is the next day's minimum, used by the double methods; None means this day's
    own minimum (the usual rule for the last day of a series)."""
    if method not in METHODS:
        raise ValueError(f"method must be one of {', '.join(METHODS)}, got {method!r}")
    if cutoff not in CUTOFFS:
        raise ValueError(f"cutoff must be one of {', '.join(CUTOFFS)}, got {cutoff!r}")
    nxt = tmin if next_tmin is None else next_tmin
    for name, value in (("tmin", tmin), ("tmax", tmax), ("next_tmin", nxt), ("lower", lower)):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite temperature, got {value!r}")
    if tmin > tmax:
        raise ValueError(f"tmin {tmin} is above tmax {tmax}")
    if upper is not None and not math.isfinite(upper):
        raise ValueError(f"upper threshold must be finite or None, got {upper!r}")
    if upper is not None and not upper > lower:
        raise ValueError(f"upper threshold {upper} must be above the lower threshold {lower}")

    shape = method.split("_")[1]
    first = _half(tmin, tmax, lower, upper, shape, cutoff)
    if method.startswith("sng"):
        return first
    # The second half falls from today's maximum to the next minimum; a next minimum above
    # today's maximum (a warm front) is a rise, which has the same mean either way round.
    second = _half(min(nxt, tmax), max(nxt, tmax), lower, upper, shape, cutoff)
    return (first + second) / 2


def daily_degree_days(
    days: Sequence[tuple[float, float]],
    *,
    lower: float,
    upper: float | None = None,
    method: str = "sng_sine",
    cutoff: str = "horizontal",
) -> list[float]:
    """Degree-days of consecutive days given as (tmin, tmax); the next day's minimum feeds the
    double methods and the last day uses its own."""
    mins = [d[0] for d in days]
    return [
        degree_days(
            tmin,
            tmax,
            lower=lower,
            upper=upper,
            method=method,
            cutoff=cutoff,
            next_tmin=mins[n + 1] if n + 1 < len(mins) else tmin,
        )
        for n, (tmin, tmax) in enumerate(days)
    ]


def _half(
    lo: float, hi: float, lower: float, upper: float | None, shape: str, cutoff: str
) -> float:
    """Mean degree-days over a half-day rising from `lo` to `hi`."""
    capped = upper is not None and cutoff == "horizontal"
    mean, amp = (hi + lo) / 2, (hi - lo) / 2
    if amp == 0:  # a flat half, or one too narrow to divide by (subnormal spread)
        if lo <= lower:
            return 0.0
        if upper is not None and lo > upper:
            return upper - lower if capped else 0.0
        return lo - lower
    if shape == "tri":
        span = hi - lo
        start = min(max(lower, lo), hi)
        end = hi if upper is None else min(max(upper, lo), hi)
        area = ((end - lower) ** 2 - (start - lower) ** 2) / (2 * span)
        if capped:
            area += (upper - lower) * (hi - end) / span
        return area
    # T(t) = mean + amp * sin(t), t from -pi/2 (lo) to pi/2 (hi).
    t1 = _crossing(lower, mean, amp)
    t2 = math.pi / 2 if upper is None else _crossing(upper, mean, amp)
    area = (mean - lower) * (t2 - t1) + amp * (math.cos(t1) - math.cos(t2))
    if capped:
        area += (upper - lower) * (math.pi / 2 - t2)
    return area / math.pi


def _crossing(threshold: float, mean: float, amp: float) -> float:
    """The phase where the sine reaches `threshold`, held to [-pi/2, pi/2]."""
    return math.asin(min(1.0, max(-1.0, (threshold - mean) / amp)))
