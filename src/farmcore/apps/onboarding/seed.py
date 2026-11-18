"""Seed the demo farm's point so a seeded Home never opens on the first-run prompt.

The coordinates are the demo's Saint-Émilion area (synthetic farm data, not a real address).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from farmcore.core.seed import ensure_default_farm

DEMO_LOCATION = {
    "forecast_location": {"latitude": 44.9, "longitude": -0.2},
    "time_zone": "Europe/Paris",
    "country": "FR",
    "country_name": "France",
}


def seed(session: Session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    farm = ensure_default_farm(session)
    props = dict(farm.props or {})
    if props.get("forecast_location") is None:
        farm.props = {**props, **DEMO_LOCATION}
        session.flush()
        return {"onboarding": 1}
    return {"onboarding": 0}
