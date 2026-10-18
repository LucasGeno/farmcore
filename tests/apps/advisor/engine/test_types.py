"""Engine input shapes: timestamps must be timezone-aware and are held in UTC."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from farmcore.apps.advisor.engine import HourlyObs, ProductRef, Spray

COPPER = ProductRef(name="copper", product_type="contact", targets=["downy"])


def test_naive_observation_timestamp_is_a_clear_error():
    with pytest.raises(ValueError, match="HourlyObs.ts must be timezone-aware"):
        HourlyObs(ts=datetime(2024, 4, 9, 12))


def test_naive_spray_timestamp_is_a_clear_error():
    with pytest.raises(ValueError, match="Spray.ts must be timezone-aware"):
        Spray(ts=datetime(2024, 4, 9, 8), products=[COPPER])


def test_local_timestamps_are_held_in_utc():
    local = datetime(2024, 5, 9, 8, tzinfo=ZoneInfo("Europe/Paris"))

    spray = Spray(ts=local, products=[COPPER])
    obs = HourlyObs(ts=local)

    assert spray.ts == obs.ts == datetime(2024, 5, 9, 6, tzinfo=UTC)
    assert spray.ts.tzinfo is UTC and obs.ts.tzinfo is UTC


def test_lists_are_accepted_and_held_as_tuples():
    spray = Spray(ts=datetime(2024, 5, 9, 6, tzinfo=UTC), products=[COPPER])

    assert spray.products == (COPPER,)
    assert COPPER.targets == ("downy",)
    assert hash(spray)
