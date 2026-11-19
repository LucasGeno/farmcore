"""The portal's station list feeds the farm's source choice: stations near the pin are ranked
from the catalogue's coordinates and altitude, and a far station is never offered."""

from __future__ import annotations

from decimal import Decimal

from farmcore.apps.weather_meteofrance.provider import provider
from farmcore.core.weather.contract import Point
from farmcore.core.weather.selection import select


def test_a_pin_near_saint_emilion_ranks_the_portal_catalogue_stations(credentials, portal) -> None:
    portal()

    chosen = select([provider(credentials)], Point(44.9, -0.2), "FR", Decimal(30))

    assert [(c.station.provider_station_id, c.station.elevation_m) for c in chosen.stations] == [
        ("33394002", Decimal(38)),  # St-Émilion, about 2 km
        ("33281001", Decimal(47)),  # Bordeaux-Mérignac, about 40 km; Paris is not offered
    ]


def test_a_slow_portal_never_holds_up_saving_the_pin(
    credentials, portal, session, session_factory, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from farmcore.apps.onboarding import service
    from farmcore.core import clock
    from farmcore.core.models.canonical import Farm
    from farmcore.core.seed import ensure_default_farm
    from farmcore.main import create_app

    portal(stall=0.3)
    monkeypatch.setattr(service, "DISCOVERY_TIMEOUT_SECONDS", 0.2)
    farm = ensure_default_farm(session, credentials)
    clock.set_live(session, credentials)
    session.commit()

    app = create_app(credentials, session_factory=session_factory)
    token = credentials.admin_token.get_secret_value()
    with TestClient(app, headers={"X-Admin-Token": token}) as client:
        saved = client.post(
            "/onboarding/location",
            data={"latitude": "44.9", "longitude": "-0.2"},
            follow_redirects=False,
        )

    assert saved.status_code == 303
    session.expire_all()
    props = session.get(Farm, farm.id).props
    assert props["forecast_location"] == {"latitude": 44.9, "longitude": -0.2}
    assert (
        "meteofrance-portal: station list unavailable (PortalError)"
        in (props["weather_sources"]["notes"])
    )
