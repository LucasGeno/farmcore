"""Repository policy checks (not HTTP/DB lifecycle evidence)."""

import pytest

from farmcore.core.access import ScopeDenied, ScopedRepository
from farmcore.core.models.canonical import DataGrant


@pytest.mark.parametrize(
    "resource", ["parameter_version", "parameter_review", "parameter_activation"]
)
def test_advice_or_weather_consent_cannot_read_private_parameter_history(resource):
    grant = DataGrant(id=1, farm_id=1, grantee="recipient", scopes=["advice:read", "stations:read"])
    repo = ScopedRepository(None, 1, grant, "recipient", "consent-policy")
    with pytest.raises(ScopeDenied):
        repo.check("advice:read", resource)
    with pytest.raises(ScopeDenied):
        repo.check("parameters:read", resource)
    grant.scopes = ["parameters:read"]
    repo.check("parameters:read", resource)


def test_weather_only_consent_preserves_forecast_without_parameter_or_observation_scope(
    session, settings
):
    from datetime import UTC, datetime

    from farmcore.apps.forecast import sources, store
    from farmcore.apps.forecast.planning import plan_days
    from farmcore.apps.notifications.service import items
    from farmcore.core.models.canonical import Farm
    from tests.apps.forecast.conftest import FIXTURE

    farm = Farm(name="Weather-consent farm", props={"region": "FRI12"})
    session.add(farm)
    session.flush()
    grant = DataGrant(
        farm_id=farm.id,
        grantee="Weather reader",
        purpose="Weather-only consent",
        scopes=[
            "fields:read",
            "advice:read",
            "stations:read",
        ],
    )
    session.add(grant)
    session.flush()
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    repo = ScopedRepository(session, farm.id, grant, "recipient", "consent-bound weather")
    now = datetime(2024, 4, 9, 4, tzinfo=UTC)
    plan = plan_days(repo, now, settings.display_tz)
    notifications = items(repo, now, settings.display_tz, ["forecast"])
    assert len(plan["days"]) == 14
    assert any(day["grade"] == "best" for day in plan["days"])
    assert "not authorized" in plan["unavailable"]
    assert all(not day["parcels"] for day in plan["days"])
    assert notifications
