"""One forecast-edge rule for the card and the Today/home summary (QA F2): the engine scans
spray windows from `floor_hour(now)` for `window_horizon_h` hours, so a window ending there is
open to the forecast end, not a deadline, even when the advice was evaluated off the hour.
A window that closes earlier (real weather) stays a deadline. No engine change; no DB."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from farmcore.apps.advisor import service
from farmcore.apps.advisor.routes import _window_phrase

TZ = "Europe/Paris"
EVALUATED = datetime(2024, 4, 10, 3, 30, tzinfo=UTC)  # 05:30 Paris, off the hour
START = datetime(2024, 4, 10, 3, 0, tzinfo=UTC)
EDGE = datetime(2024, 4, 11, 15, 0, tzinfo=UTC)  # floor(03:30) + 36 h
EARLIER = datetime(2024, 4, 11, 10, 0, tzinfo=UTC)


def test_the_forecast_edge_counts_from_the_evaluation_hour() -> None:
    assert service.forecast_edge(EVALUATED, 36) == EDGE
    assert service.forecast_edge(EVALUATED, None) is None


@pytest.mark.parametrize(("end", "edge"), [(EDGE, True), (EARLIER, False)])
def test_runs_to_forecast_edge(end, edge) -> None:
    assert service.runs_to_forecast_edge(end, EVALUATED, 36) is edge
    assert service.runs_to_forecast_edge(end, EVALUATED, None) is False
    assert service.runs_to_forecast_edge(None, EVALUATED, 36) is False


def test_the_card_calls_a_fractional_evaluations_edge_the_forecast_end() -> None:
    window = [START.isoformat(), EDGE.isoformat()]

    _, plan = _window_phrase(window, EVALUATED, 36, TZ)
    _, lead = _window_phrase(window, EVALUATED, 36, TZ, lead=True)

    assert plan == "before tomorrow 17:00 (end of forecast)"
    assert lead == "open to the forecast end, tomorrow 17:00"


def test_an_earlier_real_cutoff_stays_a_deadline_on_the_card() -> None:
    _, plan = _window_phrase([START.isoformat(), EARLIER.isoformat()], EVALUATED, 36, TZ)

    assert plan == "before tomorrow 12:00"
