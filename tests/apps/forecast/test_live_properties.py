"""Generated receipt invariants through the real live-job and stored-run boundary."""

from datetime import UTC, datetime, timedelta

import httpx
from hypothesis import HealthCheck, given
from hypothesis import settings as property_settings
from hypothesis import strategies as st

from farmcore.apps.forecast.manifest import MANIFEST
from farmcore.apps.forecast.store import latest_run
from farmcore.core import clock
from farmcore.core.access import repo_for_job
from farmcore.core.jobs import run_now
from farmcore.core.models.canonical import Farm
from tests.apps.forecast.conftest import provider_client

JOB = "forecast.fetch_live"
# Exact binary fractions keep the oracle independent of float rounding/storage details.
CELLS = st.lists(
    st.tuples(
        st.one_of(st.none(), st.sampled_from((0.0, 0.25, 1.5, 8.0))),
        st.one_of(st.none(), st.sampled_from((0.0, 4.5, 12.0, 25.0))),
    ),
    min_size=2,
    max_size=24,
)


@property_settings(
    max_examples=30,
    deadline=None,
    # Pytest fixtures span all examples. Every example has its own outer SQL savepoint
    # (rolled back below), and a scoped external-provider patch, so neither cached farm
    # state nor old duplicate receipts can affect the next example or shrink attempt.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cells=CELLS)
def test_generated_weather_nulls_remain_unknown_in_stored_receipts(
    connection, session_factory, settings, monkeypatch, cells
):
    example = connection.begin_nested()
    try:
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        rain = [cell[0] for cell in cells]
        wind = [cell[1] for cell in cells]
        payload = {
            "latitude": 44.91,
            "longitude": -0.21,
            "utc_offset_seconds": 0,
            "hourly_units": {
                "precipitation": "mm",
                "wind_speed_10m": "km/h",
                "temperature_2m": "°C",
            },
            "hourly": {
                "time": [(start + timedelta(hours=i)).isoformat() for i in range(len(cells))],
                "precipitation": rain,
                "wind_speed_10m": wind,
                # Retain each hourly row even when both generated inputs are absent.
                "temperature_2m": [12.0] * len(cells),
            },
        }
        with session_factory.begin() as session:
            farm = Farm(
                name="Generated receipt invariant",
                props={"forecast_location": {"latitude": 44.9, "longitude": -0.2}},
            )
            session.add(farm)
            session.flush()
            farm_id = farm.id
            clock.set_live(session, settings)
        with monkeypatch.context() as external:
            external.setattr(
                httpx,
                "AsyncClient",
                provider_client(
                    lambda url, **kw: httpx.Response(
                        200, request=httpx.Request("GET", url), json=payload
                    )
                ),
            )
            result = run_now(session_factory, [MANIFEST], JOB, settings)
        assert result is not None and result.ok
        with session_factory() as session:
            run = latest_run(repo_for_job(session, farm_id, JOB), datetime.now(UTC))
            assert run is not None
            assert [row.rr1_mm for row in run.series] == rain
            assert [row.ff_kmh for row in run.series] == wind
            assert [row.t_c for row in run.series] == [12.0] * len(cells)
            assert run.props["coverage"]["rr1_mm"] == {
                "known": sum(value is not None for value in rain),
                "expected": len(cells),
            }
            assert run.props["coverage"]["ff_kmh"] == {
                "known": sum(value is not None for value in wind),
                "expected": len(cells),
            }
    finally:
        example.rollback()
