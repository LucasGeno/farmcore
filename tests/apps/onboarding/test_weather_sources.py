"""Placing the pin chooses the farm's weather sources from the apps that are on; in Expert view
the owner overrides them and gives the farm's elevation, which re-ranks the stations.

The installation's own providers answer offline: the replayed Météo-France station
(St-Émilion, 38 m), Open-Meteo for the forecast, and the live portal without credentials.
"""

from __future__ import annotations

from farmcore.core.models.canonical import Farm
from farmcore.core.seed import ensure_default_farm


def _sources(session, farm_id: int) -> dict:
    session.expire_all()
    return session.get(Farm, farm_id).props.get("weather_sources") or {}


def test_the_pin_chooses_sources_and_the_owner_overrides_them_in_expert_view(
    client, session, settings
) -> None:
    farm = ensure_default_farm(session, settings)
    session.commit()

    client.post("/onboarding/location", data={"latitude": "44.9", "longitude": "-0.2"})

    chosen = _sources(session, farm.id)
    assert [(s["provider"], s["id"]) for s in chosen["stations"]] == [("meteofrance", "33394002")]
    assert "farm elevation unknown" in chosen["stations"][0]["reason"]
    assert [f["provider"] for f in chosen["forecast"]] == ["open-meteo"]
    assert chosen["backfill"] == []
    # The live portal has no credentials here: said, and the pin is still saved.
    assert any(n.startswith("meteofrance-portal:") for n in chosen["notes"])

    client.cookies.set("expert", "1")
    assert "Save sources" in client.get("/onboarding/location").text

    refused = client.post("/onboarding/weather-sources", data={"station": "other:1"})
    assert refused.status_code == 422

    saved = client.post(
        "/onboarding/weather-sources",
        data={"station": "meteofrance:33394002", "forecast": "", "elevation": "400"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    chosen = _sources(session, farm.id)
    assert chosen["override"] == {"station": "meteofrance:33394002"}
    assert chosen["stations"][0]["down_ranked"]
    assert "362 m lower" in chosen["stations"][0]["reason"]

    # A new point: the old elevation and override described the old one.
    client.post("/onboarding/location", data={"latitude": "44.95", "longitude": "-0.25"})
    session.expire_all()
    props = session.get(Farm, farm.id).props
    assert "elevation_m" not in props
    assert props["weather_sources"]["override"] == {}
