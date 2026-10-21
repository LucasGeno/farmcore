"""Saved spray weather stays out of grant exports: the grantee register keeps its keys.

The owner register opts in to `saved_weather`; the share app reads the default projection, so a
grant sees exactly the rows it saw before, even when a source carries saved evidence. Synthetic
values only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from farmcore.apps.operations import register, spray_weather
from farmcore.apps.share import service
from farmcore.core.models.canonical import LogSource
from tests.apps.share.synth import NOW, T_SPRAY

STATION = "Synthetic withheld station"


@pytest.fixture
def grant(session, owner_repo, grantee_identity):
    g = service.create_grant(
        owner_repo,
        recipient_identity_id=grantee_identity.id,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()
    return g


@pytest.fixture
def evidence(session, demo, spray):
    """A done spray whose voice source carries a captured v1 snapshot."""
    log = spray()
    [source] = session.scalars(select(LogSource).where(LogSource.log_id == log.id))
    slot = T_SPRAY.replace(minute=0)
    source.raw_payload = {
        **(source.raw_payload or {}),
        spray_weather.NAMESPACE: {
            "v": spray_weather.VERSION,
            "policy": spray_weather.POLICY,
            "status": spray_weather.CAPTURED,
            "reason": None,
            "source_time": T_SPRAY.isoformat(),
            "parcel_ids": [p.id for p in demo.parcels],
            "slot": slot.isoformat(),
            "station": {"id": 3, "name": STATION},
            "observed_at": slot.isoformat(),
            "rain_interval": [(slot - timedelta(hours=1)).isoformat(), slot.isoformat()],
            "values": {"t_c": 13.0, "ff_kmh": 6.0, "rr1_mm": 0.2},
            "units": dict(spray_weather.UNITS),
            "captured_at": NOW.isoformat(),
            "data_clock": NOW.isoformat(),
        },
    }
    session.commit()
    return log


def test_grantee_register_json_keeps_its_keys_with_saved_weather_present(
    grantee_client, admin, evidence, grant
) -> None:
    rows = grantee_client.get(f"/share/as/{grant.id}/register.json").json()

    assert {r["log_id"] for r in rows} == {evidence.id}
    # the EU fields, then the portable parcel ids; never saved weather
    assert all(list(r) == [*register.FIELDS, register.PARCEL_ALIASES] for r in rows)
    # The owner's own export of the same rows does carry it: the grant withholds it.
    owner = admin.get("/operations/register.json").json()
    assert {e["station"]["name"] for r in owner for e in r["saved_weather"]} == {STATION}


def test_grantee_page_and_ngsi_export_never_show_saved_weather(
    grantee_client, evidence, grant
) -> None:
    for path in ("", "register.json", "ngsi-ld.json"):
        resp = grantee_client.get(f"/share/as/{grant.id}/{path}")
        assert resp.status_code == 200
        assert STATION not in resp.text
        assert "saved_weather" not in resp.text
        assert spray_weather.NAMESPACE not in resp.text
